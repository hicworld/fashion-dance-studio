"""Offline checks for one billable POST, durable IDs and query-only recovery."""
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from PIL import Image

import providers
import providers_fal as fal


class Response:
    def __init__(self, payload=None, *, status=200, content=b"offline valid video" * 8, chunks=None):
        self.payload, self.status_code, self.content, self.chunks = payload, status, content, chunks

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("offline failure", response=self)

    def iter_content(self, chunk_size):
        return iter(self.chunks if self.chunks is not None else [self.content])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FalRecoveryTests(unittest.TestCase):
    option = "fal-kling-v3-pro"
    identifier = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.image, self.clip, self.output = (self.folder / name for name in ("model.png", "reference.mp4", "fashion_dance_source.mp4"))
        Image.new("RGB", (512, 768), "white").save(self.image)
        self.clip.write_bytes(b"offline reference video" * 8)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"FAL_KEY": "offline-current-key"}))
        self.probe = self.stack.enter_context(patch.object(fal, "_probe", return_value={"width": 720, "height": 1280, "duration": 5.0}))
        self.upload_client = Mock(upload_file=Mock(side_effect=lambda path: "https://upload.test/" + path.name))
        self.new_client_original = fal._new_upload_client
        self.upload = self.stack.enter_context(patch.object(fal, "_new_upload_client", return_value=self.upload_client))
        self.post = self.stack.enter_context(patch.object(fal.requests, "post", return_value=Response({"request_id": self.identifier})))
        self.get = self.stack.enter_context(patch.object(fal.requests, "get", side_effect=self.success_get))
        self.wait = self.stack.enter_context(patch.object(fal, "_wait"))
        self.events = []

    def success_get(self, url, **kwargs):
        if url.endswith("/status"):
            return Response({"status": "COMPLETED", "request_id": self.identifier})
        if url.startswith(fal.QUEUE_REQUESTS):
            return Response({"video": {"url": "https://download.test/video.mp4?signature=private"}})
        return Response()

    def task(self, **changes):
        value = {"provider": "fal", "task_id": self.identifier, "request_id": self.identifier,
                 "endpoint": fal.ENDPOINTS[self.option], "option": self.option, **changes}
        fal._paths(self.output)[0].write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_one_queue_post_records_id_before_query_and_downloads_valid_source(self):
        statuses = ["IN_QUEUE", "IN_PROGRESS", "COMPLETED"]

        def get(url, **kwargs):
            if url.endswith("/status"):
                task = json.loads(fal._paths(self.output)[0].read_text(encoding="utf-8"))
                self.assertEqual(task["request_id"], self.identifier)
                return Response({"status": statuses.pop(0), "request_id": self.identifier})
            return self.success_get(url, **kwargs)

        self.get.side_effect = get
        result = providers.motion(self.image, self.clip, self.output, self.option, progress=self.events.append)
        self.assertEqual(result["task_id"], self.identifier)
        self.assertEqual(result["model"], fal.ENDPOINTS[self.option])
        self.assertTrue(self.output.is_file())
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], fal.QUEUE_BASE + fal.ENDPOINTS[self.option])
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["character_orientation"], "video")
        self.assertTrue(payload["keep_original_sound"])
        self.assertNotIn("resolution", payload)
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.upload_client.upload_file.call_count, 2)
        self.assertTrue(any("排队" in event["stage"] for event in self.events))
        self.assertTrue(any("正在生成" in event["stage"] for event in self.events))
        self.assertEqual(self.events[-1]["status"], "downloaded")
        self.assertFalse(any("percent" in event for event in self.events))
        for path in fal._paths(self.output)[:2]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("https://", text)
            self.assertNotIn("offline-current-key", text)

    def test_accepted_query_failure_then_restart_only_queries_same_id(self):
        self.get.side_effect = requests.Timeout("signed URL and key must not be exposed")
        with self.assertRaises(fal.MotionTransientError):
            fal.motion(self.image, self.clip, self.output, self.option)
        self.assertTrue(fal._paths(self.output)[0].is_file())
        self.assertFalse(self.output.exists())
        self.post.assert_called_once()
        with self.assertRaises(fal.MotionSubmissionUncertainError):
            fal.motion(self.image, self.clip, self.output, self.option)
        self.get.side_effect = self.success_get
        self.upload.reset_mock()
        self.upload_client.upload_file.reset_mock()
        result = providers.resume_motion(self.output, self.option)
        self.assertEqual(result["request_id"], self.identifier)
        self.assertTrue(self.output.is_file())
        self.post.assert_called_once()
        self.upload.assert_not_called()
        self.upload_client.upload_file.assert_not_called()

    def test_submit_timeout_is_not_retried_and_unknown_submission_cannot_resume(self):
        self.post.side_effect = requests.Timeout("unknown response")
        with self.assertRaises(fal.MotionSubmissionUncertainError):
            fal.motion(self.image, self.clip, self.output, self.option)
        self.post.assert_called_once()
        self.assertTrue(fal._paths(self.output)[1].is_file())
        self.assertFalse(fal._paths(self.output)[0].exists())
        self.get.assert_not_called()
        self.upload.reset_mock()
        with self.assertRaises(fal.MotionSubmissionUncertainError):
            fal.resume_motion(self.output, self.option)
        with self.assertRaises(fal.MotionSubmissionUncertainError):
            fal.motion(self.image, self.clip, self.output, self.option)
        self.upload.assert_not_called()
        self.post.assert_called_once()
        self.get.assert_not_called()

    def test_acceptance_record_write_failure_keeps_marker_and_never_resubmits(self):
        with patch.object(fal, "_atomic_record", side_effect=fal.MotionTransientError("offline write failure")):
            with self.assertRaises(fal.MotionSubmissionUncertainError) as error:
                fal.motion(self.image, self.clip, self.output, self.option)
        self.assertIn(self.identifier, str(error.exception))
        self.assertTrue(fal._paths(self.output)[1].is_file())
        self.post.assert_called_once()
        self.get.assert_not_called()

    def test_explicit_submission_rejection_has_safe_advice_and_exactly_one_post(self):
        for code, advice in ((401, "FAL_KEY"), (402, "余额"), (403, "权限"), (422, "素材")):
            with self.subTest(code=code):
                self.post.reset_mock()
                self.post.return_value = Response({"error": "offline-current-key https://private.test/file?signature=hidden"}, status=code)
                destination = self.folder / f"rejected_{code}.mp4"
                with self.assertRaises(fal.MotionSubmissionUncertainError) as error:
                    fal.motion(self.image, self.clip, destination, self.option)
                text = str(error.exception)
                self.assertIn(f"HTTP {code}", text)
                self.assertIn(advice, text)
                self.assertNotIn("offline-current-key", text)
                self.assertNotIn("https://", text)
                self.assertTrue(fal._paths(destination)[1].exists())
                self.assertFalse(fal._paths(destination)[0].exists())
                self.post.assert_called_once()
        self.get.assert_not_called()

    def test_missing_fal_adapter_is_configuration_status_instead_of_import_crash(self):
        with patch.object(providers.importlib, "import_module", side_effect=ModuleNotFoundError("offline missing adapter")):
            missing = providers.missing_for("motion", self.option)
        self.assertTrue(missing)
        self.assertIn("适配器", missing[0])
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_complete_error_is_terminal_and_cached_without_redownloading_or_posting(self):
        self.task()
        self.get.return_value = Response({"status": "COMPLETED", "error": "private provider error", "error_type": "validation_error"})
        self.get.side_effect = None
        for attempt in range(2):
            with self.assertRaises(fal.MotionTerminalError) as error:
                fal.resume_motion(self.output, self.option)
            self.assertEqual(error.exception.task_status, "failed")
        self.assertEqual(self.get.call_count, 1)
        self.assertFalse(self.output.exists())
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_not_found_auth_and_server_errors_have_distinct_recovery_outcomes(self):
        for status, kind in ((404, fal.MotionTerminalError), (410, fal.MotionTerminalError),
                             (401, fal.MotionTransientError), (403, fal.MotionTransientError),
                             (429, fal.MotionTransientError), (503, fal.MotionTransientError)):
            with self.subTest(status=status):
                for path in fal._paths(self.output):
                    path.unlink(missing_ok=True)
                self.task()
                self.get.side_effect = None
                self.get.return_value = Response({}, status=status)
                with self.assertRaises(kind):
                    fal.resume_motion(self.output, self.option)
                self.assertFalse(self.output.exists())
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_invalid_download_never_becomes_final_and_does_not_submit_again(self):
        def probe(path):
            if path.name.endswith(".download"):
                raise ValueError("not a video")
            return {"width": 720, "height": 1280, "duration": 5}

        self.probe.side_effect = probe
        with self.assertRaises(fal.MotionTransientError):
            fal.motion(self.image, self.clip, self.output, self.option)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_name(self.output.name + ".download").exists())
        self.assertTrue(fal._paths(self.output)[0].is_file())
        self.assertTrue(fal._paths(self.output)[2].is_file())
        self.post.assert_called_once()

    def test_local_valid_source_resumes_without_key_sdk_upload_or_queries(self):
        self.output.write_bytes(b"offline source" * 8)
        with patch.dict(os.environ, {"FAL_KEY": ""}):
            result = fal.resume_motion(self.output, self.option)
        self.assertTrue(result["reused_local_video"])
        self.assertEqual(result["task_id"], "")
        self.post.assert_not_called()
        self.get.assert_not_called()
        self.upload.assert_not_called()

    def test_cached_download_can_resume_without_key_and_without_queue_query(self):
        task = self.task()
        fal._paths(self.output)[2].write_text(json.dumps({**task, "status": "done", "video_url": "https://download.test/cached.mp4"}), encoding="utf-8")
        with patch.dict(os.environ, {"FAL_KEY": ""}):
            result = fal.resume_motion(self.output, self.option)
        self.assertEqual(result["request_id"], self.identifier)
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(self.get.call_args.args[0], "https://download.test/cached.mp4")
        self.assertNotIn("headers", self.get.call_args.kwargs)
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_task_endpoint_or_identifier_mismatch_is_never_queried(self):
        for changes in ({"endpoint": fal.ENDPOINTS["fal-kling-v3-standard"]}, {"task_id": "other-id"}, {"request_id": "../bad"}, {"provider": "volcengine"}):
            with self.subTest(changes=changes):
                self.task(**changes)
                with self.assertRaises(fal.MotionSubmissionUncertainError):
                    fal.resume_motion(self.output, self.option)
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_stop_before_start_and_during_query_never_cancels_or_resubmits_cloud_task(self):
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(fal.MotionCancelledError):
            fal.motion(self.image, self.clip, self.output, self.option, stop_event=stopped)
        self.post.assert_not_called()
        self.upload.assert_not_called()
        self.task()
        stopped.clear()

        def get(*args, **kwargs):
            stopped.set()
            return Response({"status": "IN_QUEUE"})

        self.get.side_effect = get
        self.wait.side_effect = lambda seconds, event: fal._cancelled(event)
        with self.assertRaises(fal.MotionCancelledError):
            fal.resume_motion(self.output, self.option, stop_event=stopped)
        self.assertTrue(fal._paths(self.output)[0].exists())
        self.post.assert_not_called()

    def test_stop_during_stream_preserves_previous_file_and_removes_partial(self):
        self.output.write_bytes(b"previous file must remain")
        stop = threading.Event()

        def chunks():
            yield b"first chunk"
            stop.set()
            yield b"second chunk"

        self.get.side_effect = None
        self.get.return_value = Response(chunks=chunks())
        with self.assertRaises(fal.MotionCancelledError):
            fal._download("https://download.test/file.mp4", self.output, stop_event=stop)
        self.assertEqual(self.output.read_bytes(), b"previous file must remain")
        self.assertFalse(self.output.with_name(self.output.name + ".download").exists())
        self.post.assert_not_called()

    def test_validation_rejects_limits_before_upload_or_submit(self):
        for size in ((339, 768), (4000, 768), (340, 1000), (1000, 340)):
            with self.subTest(size=size):
                Image.new("RGB", size).save(self.image)
                with self.assertRaises(ValueError):
                    fal.motion(self.image, self.clip, self.output, self.option)
        Image.new("RGB", (512, 768)).save(self.image)
        for info in ({"width": 339, "height": 768, "duration": 5}, {"width": 512, "height": 4000, "duration": 5},
                     {"width": 512, "height": 768, "duration": 2.99}, {"width": 512, "height": 768, "duration": 30.06}):
            self.probe.return_value = info
            with self.subTest(info=info), self.assertRaises(ValueError):
                fal.motion(self.image, self.clip, self.output, self.option)
        self.probe.return_value = {"width": 720, "height": 1280, "duration": 30.05}
        self.assertEqual(fal.validate_motion_inputs(self.image, self.clip, self.option)["reference_duration_seconds"], 30.05)
        with self.image.open("r+b") as output:
            output.truncate(fal.MAX_IMAGE_BYTES + 1)
        with self.assertRaises(ValueError):
            fal.motion(self.image, self.clip, self.output, self.option)
        Image.new("RGB", (512, 768)).save(self.image)
        with self.clip.open("r+b") as output:
            output.truncate(fal.MAX_VIDEO_BYTES + 1)
        with self.assertRaises(ValueError):
            fal.motion(self.image, self.clip, self.output, self.option)
        self.upload.assert_not_called()
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_new_sdk_instance_reads_latest_saved_key_instead_of_module_singleton(self):
        keys = []

        def client(key, **kwargs):
            keys.append(key)
            return SimpleNamespace(upload_file=lambda path: "https://upload.test/" + path.name)

        module = SimpleNamespace(SyncClient=client, upload_file=Mock(side_effect=AssertionError("global SDK forbidden")))
        with patch.object(fal.importlib, "import_module", return_value=module):
            for key in ("first-offline-key", "second-offline-key"):
                with patch.dict(os.environ, {"FAL_KEY": key}):
                    # The patched runtime constructor stays isolated from the
                    # network while its original function receives a fake SDK.
                    result = self.new_client_original()
                    self.assertEqual(result.upload_file(self.image), "https://upload.test/model.png")
        self.assertEqual(keys, ["first-offline-key", "second-offline-key"])
        module.upload_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()

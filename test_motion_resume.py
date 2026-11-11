"""Query-only motion recovery tests: isolated files and no cloud requests."""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import providers
import providers_jimeng as provider


class MotionResumeTests(unittest.TestCase):
    VIDEO = b"OFFLINE_VALID_VIDEO" * 16
    URL = "https://example.invalid/output.mp4?signature=private-test-url"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "source.mp4"
        self.task_path, self.marker_path, self.result_path = provider._motion_paths(self.destination)
        self.task_path.write_text(json.dumps({"task_id": "offline-task", "req_key": provider.DREAMACTOR_MODEL,
                                             "request_id": "offline-request"}), encoding="utf-8")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(os, "environ", {}))
        self.stack.enter_context(patch.object(provider, "available", side_effect=AssertionError("resume must not preflight uploads")))
        self.stack.enter_context(patch.object(provider, "prepare_motion_clip", side_effect=AssertionError("no clip preparation")))
        self.stack.enter_context(patch.object(provider, "_motion_image_base64", side_effect=AssertionError("no input preparation")))
        self.upload = self.stack.enter_context(patch.object(provider.storage_tos, "upload_file", side_effect=AssertionError("no upload")))
        self.stack.enter_context(patch.object(provider.requests, "get", side_effect=AssertionError("no real HTTP")))
        self.stack.enter_context(patch.object(provider.requests, "post", side_effect=AssertionError("no real HTTP")))
        self.service = self.stack.enter_context(patch.object(provider, "_new_motion_service", return_value=object()))
        self.query = self.stack.enter_context(patch.object(provider, "_visual_call", side_effect=AssertionError("unconfigured query")))
        self.download = self.stack.enter_context(patch.object(provider, "download_file", side_effect=self.download_video))
        self.sleep = self.stack.enter_context(patch.object(provider.time, "sleep"))
        self.stack.enter_context(patch.object(provider.subprocess, "run", side_effect=self.probe))

    def probe(self, arguments, **kwargs):
        path = Path(arguments[-1])
        if path.read_bytes() != self.VIDEO:
            raise subprocess.CalledProcessError(1, "offline ffprobe")
        return Mock(stdout=json.dumps({"streams": [{"width": 720, "height": 1280}], "format": {"duration": "5.0"}}))

    def download_video(self, url, destination):
        self.assertTrue(url.startswith("https://example.invalid/"))
        Path(destination).write_bytes(self.VIDEO)

    @staticmethod
    def response(status, url=None, *, code=10000):
        return {"code": code, "data": {"status": status, **({"video_url": url} if url else {})}}

    def query_responses(self, *responses):
        iterator = iter(responses)

        def query(service, method, payload):
            self.assertEqual(method, "cv_sync2async_get_result", "recovery must never submit")
            self.assertEqual(payload, {"req_key": provider.DREAMACTOR_MODEL, "task_id": "offline-task"})
            value = next(iterator)
            if isinstance(value, BaseException):
                raise value
            return value
        self.query.side_effect = query

    def cached(self, **extra):
        self.result_path.write_text(json.dumps({"task_id": "offline-task", "req_key": provider.DREAMACTOR_MODEL,
                                               "status": "done", "video_url": self.URL, **extra}), encoding="utf-8")

    def test_query_only_recovers_processing_task_and_keeps_url_private(self):
        self.query_responses(self.response("in_queue"), self.response("generating"), self.response("done", self.URL))
        progress = []
        metadata = providers.resume_motion(self.destination, progress=progress.append)
        self.assertEqual(self.destination.read_bytes(), self.VIDEO)
        self.assertEqual(self.query.call_count, 3)
        self.upload.assert_not_called()
        self.assertEqual(metadata["task_id"], "offline-task")
        self.assertNotIn(self.URL, json.dumps(metadata))
        self.assertNotIn("private-test-url", json.dumps(progress))
        self.assertEqual(json.loads(self.result_path.read_text())["video_url"], self.URL)
        self.assertEqual(progress[-1]["status"], "downloaded")
        self.assertTrue(any("排队" in item["stage"] for item in progress))
        self.assertFalse(self.destination.with_name("source.mp4.download").exists())

    def test_valid_local_video_reuses_without_credentials_sdk_or_task(self):
        self.destination.write_bytes(self.VIDEO)
        self.task_path.unlink()
        metadata = provider.resume_motion(self.destination)
        self.assertTrue(metadata["reused_local_video"])
        self.service.assert_not_called()
        self.query.assert_not_called()
        self.download.assert_not_called()
        self.upload.assert_not_called()

    def test_missing_corrupt_or_wrong_model_checkpoint_never_submits(self):
        variants = [None, "broken-json", {"task_id": "bad id"},
                    {"task_id": "offline-task", "req_key": "wrong-model"}]
        for record in variants:
            with self.subTest(record_type=type(record).__name__):
                if record is None:
                    self.task_path.unlink(missing_ok=True)
                else:
                    self.task_path.write_text(record if isinstance(record, str) else json.dumps(record))
                with self.assertRaises(provider.MotionSubmissionUncertainError):
                    provider.resume_motion(self.destination)
        self.query.assert_not_called()
        self.download.assert_not_called()
        self.upload.assert_not_called()

    def test_legacy_checkpoint_without_req_key_can_query(self):
        self.task_path.write_text(json.dumps({"task_id": "offline-task"}))
        self.query_responses(self.response("done", self.URL))
        provider.resume_motion(self.destination)
        self.assertEqual(self.destination.read_bytes(), self.VIDEO)

    def test_transient_query_failures_retry_same_id_with_backoff(self):
        self.query_responses(requests.ConnectionError(self.URL), provider.MotionTransientError("offline timeout"),
                             self.response("done", self.URL))
        provider.resume_motion(self.destination)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [5, 10])
        self.assertEqual(self.query.call_count, 3)

    def test_unknown_or_unsuccessful_query_stays_retryable_not_remote_failed(self):
        self.query.return_value = self.response("done", self.URL, code=50000)
        self.query.side_effect = None
        with self.assertRaises(provider.MotionTransientError) as error:
            provider.resume_motion(self.destination)
        self.assertEqual(self.query.call_count, 6)
        self.assertNotIn(self.URL, str(error.exception))
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.result_path.exists())

    def test_done_without_url_remains_retryable_and_never_generates(self):
        self.query.return_value = self.response("done")
        self.query.side_effect = None
        with self.assertRaises(provider.MotionTransientError):
            provider.resume_motion(self.destination)
        self.assertEqual(self.query.call_count, 6)
        for call in self.query.call_args_list:
            self.assertEqual(call.args[1], "cv_sync2async_get_result")
        self.download.assert_not_called()
        self.upload.assert_not_called()
        self.assertTrue(self.task_path.is_file())
        self.assertFalse(self.destination.exists())

    def test_remote_terminal_states_are_saved_and_not_requeried(self):
        for status in ("failed", "expired", "not_found"):
            with self.subTest(status=status):
                self.result_path.unlink(missing_ok=True)
                self.query.reset_mock()
                self.query_responses(self.response(status))
                with self.assertRaises(provider.MotionTerminalError) as error:
                    provider.resume_motion(self.destination)
                self.assertEqual(error.exception.task_status, status)
                with self.assertRaises(provider.MotionTerminalError):
                    provider.resume_motion(self.destination)
                self.assertEqual(self.query.call_count, 1)
        self.download.assert_not_called()

    def test_cached_done_url_downloads_without_sdk_and_replaces_only_valid_file(self):
        self.cached()
        self.destination.write_bytes(b"partial old file")

        def download(url, temporary):
            self.assertEqual(self.destination.read_bytes(), b"partial old file")
            self.assertEqual(Path(temporary).name, "source.mp4.download")
            self.download_video(url, temporary)
        self.download.side_effect = download
        provider.resume_motion(self.destination)
        self.assertEqual(self.destination.read_bytes(), self.VIDEO)
        self.service.assert_not_called()
        self.query.assert_not_called()

    def test_bad_download_never_replaces_existing_file_and_can_resume_later(self):
        self.cached()
        self.destination.write_bytes(b"partial old file")
        self.download.side_effect = lambda url, target: Path(target).write_bytes(b"not a video" * 10)
        with self.assertRaises(provider.MotionTransientError):
            provider.resume_motion(self.destination)
        self.assertEqual(self.destination.read_bytes(), b"partial old file")
        self.assertFalse((self.root / "source.mp4.download").exists())
        self.assertEqual(self.download.call_count, 3)
        self.download.side_effect = self.download_video
        provider.resume_motion(self.destination)
        self.assertEqual(self.destination.read_bytes(), self.VIDEO)
        self.query.assert_not_called()

    def test_unavailable_cached_url_only_requeries_existing_task(self):
        self.cached()
        error = requests.HTTPError(self.URL, response=Mock(status_code=403))
        self.download.side_effect = [error, None]

        def download(url, target):
            if url == self.URL:
                raise error
            self.download_video(url, target)
        self.download.side_effect = download
        self.query_responses(self.response("done", "https://example.invalid/fresh.mp4"))
        provider.resume_motion(self.destination)
        self.assertEqual(self.query.call_count, 1)
        self.assertEqual(self.destination.read_bytes(), self.VIDEO)

    def test_shutdown_interrupts_query_and_download_without_losing_checkpoint(self):
        event = threading.Event()
        self.query_responses(self.response("processing"))
        def progress(state):
            if state["status"] == "processing":
                event.set()
        with self.assertRaises(provider.MotionCancelledError):
            provider.resume_motion(self.destination, progress=progress, stop_event=event)
        self.assertTrue(self.task_path.exists())
        self.assertFalse(self.destination.exists())
        event.clear()
        self.cached()
        def interrupted_download(url, target):
            self.download_video(url, target)
            event.set()
        self.download.side_effect = interrupted_download
        with self.assertRaises(provider.MotionCancelledError):
            provider.resume_motion(self.destination, stop_event=event)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "source.mp4.download").exists())
        self.assertTrue(self.result_path.exists())

    def test_uncertain_submission_marker_blocks_second_submit_and_resume_without_id(self):
        self.task_path.unlink()
        with patch.object(provider, "available", return_value=[]), \
             patch.object(provider, "_motion_image_base64", return_value="offline-image"), \
             patch.object(provider, "prepare_motion_clip", return_value=self.root / "clip.mp4"), \
             patch.object(provider.storage_tos, "upload_file", return_value="https://example.invalid/clip.mp4") as upload, \
             patch.object(provider, "_verify_clip_url"), \
             patch.object(provider, "_visual_call", side_effect=TimeoutError("offline submit response lost")) as submit:
            with self.assertRaises(provider.MotionSubmissionUncertainError):
                provider.motion(self.root / "image.png", self.root / "clip.mp4", self.destination, "volc-dreamactor-2")
            self.assertTrue(self.marker_path.is_file())
            self.assertFalse(self.task_path.exists())
            with self.assertRaisesRegex(RuntimeError, "已阻止重复提交"):
                provider.motion(self.root / "image.png", self.root / "clip.mp4", self.destination, "volc-dreamactor-2")
            with self.assertRaises(provider.MotionSubmissionUncertainError):
                provider.resume_motion(self.destination)
            submit.assert_called_once()
            upload.assert_called_once()

    def test_facade_forwards_callbacks_and_exposes_errors(self):
        stop = threading.Event()
        progress = Mock()
        with patch.object(provider, "motion", return_value={}) as motion:
            providers.motion(self.root / "image.png", self.root / "clip.mp4", self.destination,
                             "volc-dreamactor-2", progress=progress, stop_event=stop)
        self.assertEqual(motion.call_args.kwargs, {"progress": progress, "stop_event": stop})
        self.assertIs(providers.MotionTransientError, provider.MotionTransientError)
        self.assertIs(providers.MotionTerminalError, provider.MotionTerminalError)

    def test_new_motion_submits_once_then_uses_the_query_only_poll(self):
        self.task_path.unlink()
        methods = []

        def call(service, method, payload):
            methods.append(method)
            self.assertTrue(self.marker_path.is_file())
            if method == "cv_sync2async_submit_task":
                return {"code": 10000, "request_id": "offline-request", "data": {"task_id": "offline-task"}}
            self.assertEqual(method, "cv_sync2async_get_result")
            self.assertEqual(json.loads(self.task_path.read_text()),
                             {"task_id": "offline-task", "req_key": provider.DREAMACTOR_MODEL,
                              "request_id": "offline-request"})
            return self.response("done", self.URL)
        with patch.object(provider, "available", return_value=[]), \
             patch.object(provider, "_motion_image_base64", return_value="offline-image"), \
             patch.object(provider, "prepare_motion_clip", return_value=self.root / "clip.mp4"), \
             patch.object(provider.storage_tos, "upload_file", return_value="https://example.invalid/clip.mp4"), \
             patch.object(provider, "_verify_clip_url"), \
             patch.object(provider, "_visual_call", side_effect=call):
            metadata = provider.motion(self.root / "image.png", self.root / "clip.mp4", self.destination,
                                       "volc-dreamactor-2")
        self.assertEqual(methods, ["cv_sync2async_submit_task", "cv_sync2async_get_result"])
        self.assertEqual(metadata["task_id"], "offline-task")
        self.assertEqual(self.destination.read_bytes(), self.VIDEO)


if __name__ == "__main__":
    unittest.main()

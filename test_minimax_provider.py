"""Offline H3 API schema, local bounds and single-submission recovery tests."""
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

import providers_minimax as h3
from providers_jimeng import (MotionCancelledError, MotionSubmissionUncertainError,
                              MotionTerminalError, MotionTransientError)


class MiniMaxProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.images = [self.folder / "role_1.png", self.folder / "role_2.png"]
        for image in self.images:
            Image.new("RGB", (720, 960), "blue").save(image)
        self.clip = self.folder / "input.mp4"
        self.clip.write_bytes(b"fake-source")
        self.destination = self.folder / "fashion_dance_source.mp4"
        self.video = {"width": 720, "height": 960, "duration": 5.0, "fps": 30.0,
                      "video_codec": "h264", "audio_codecs": ["aac"]}
        self.descriptions = ["开场左侧人物", "开场右侧人物"]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"MINIMAX_API_KEY": "test-secret-only"}))
        self.probe = self.stack.enter_context(patch.object(h3, "_probe", return_value=self.video))
        self.wait = self.stack.enter_context(patch.object(h3, "_wait"))

    def validate(self, **kwargs):
        return h3.validate_edit_inputs(self.images, self.clip, role_descriptions=self.descriptions, **kwargs)

    def task(self, **kwargs):
        return {"provider": "minimax", "model": h3.MODEL, "option": h3.OPTION,
                "endpoint": h3.CREATE_URL, "task_id": "test-id-1", "resolution": "768P", "duration": 5, **kwargs}

    def save_task(self, **kwargs):
        record = self.task(**kwargs)
        h3._atomic_record(h3._paths(self.destination)[0], record)
        return record

    def response(self, value, status=200):
        response = Mock(status_code=status)
        response.json.return_value = value
        return response

    def rejected(self, status=402, **overrides):
        labels = {400: "bad_request_error", 401: "authorized_error", 402: "insufficient_balance_error",
                  422: "unprocessable_entity_error", 429: "rate_limit_error"}
        codes = {400: "2013", 401: "1004", 402: "1008", 422: "1026", 429: "1002"}
        return {"type": "error", "error": {"type": labels[status], "http_code": str(status),
                "message": f"private-url-and-secret-key ({codes[status]})"},
                "request_id": "021785229015510a2c883cf675b9804d", **overrides}

    def completed(self, **kwargs):
        return {"task": {"id": "test-id-1", "model": h3.MODEL, "status": "succeeded",
                         "content": {"url": "https://cdn.example.com/result.mp4?private-token=value"},
                         "usage": {"input_seconds": 5, "output_seconds": 5, "input_image_count": 2}, **kwargs}}

    def test_local_validation_needs_no_key_or_http_and_maps_two_roles(self):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": ""}), patch.object(h3.requests, "post") as post, patch.object(h3.requests, "get") as get:
            value = self.validate()
        self.assertEqual(value["generation_duration_seconds"], 5)
        self.assertEqual(value["resolution"], "768P")
        self.assertIn("参考图片1", value["request_prompt"])
        self.assertIn("参考图片2", value["request_prompt"])
        self.assertIn("开场左侧人物", value["request_prompt"])
        post.assert_not_called()
        get.assert_not_called()

    def test_resolution_assets_and_role_mapping_validation(self):
        for kwargs in ({"resolution": "1080P"}, {"video_asset_id": "asset-1"}, {"role_asset_ids": ["asset-2"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.validate(**kwargs)
        for references, descriptions in (([], []), (self.images * 2, self.descriptions * 2),
                                         (self.images, ["one"]), (self.images, ["one", ""])):
            with self.subTest(descriptions=descriptions), self.assertRaises(ValueError):
                h3.validate_edit_inputs(references, self.clip, role_descriptions=descriptions)
        self.assertEqual(self.validate(resolution="2K")["resolution"], "2K")

    def test_image_dimensions_formats_and_size_rejected_locally(self):
        Image.new("RGB", (255, 720)).save(self.images[0])
        with self.assertRaisesRegex(ValueError, "256"):
            self.validate()
        Image.new("RGB", (300, 900)).save(self.images[0])
        with self.assertRaisesRegex(ValueError, "0.4"):
            self.validate()
        Image.new("RGB", (720, 960)).save(self.images[0], format="BMP")
        with self.assertRaisesRegex(ValueError, "JPG"):
            self.validate()
        with patch.object(h3, "MAX_IMAGE_BYTES", 1), self.assertRaisesRegex(ValueError, "30MB"):
            self.validate()

    def test_duration_fps_codec_video_size_and_shape_bounds(self):
        bad = [{"duration": 3}, {"duration": 15.1}, {"duration": 5.25}, {"fps": 16}, {"fps": 61},
               {"width": 255}, {"height": 5761}, {"video_codec": "vp9"}, {"audio_codecs": ["opus"]}]
        for change in bad:
            with self.subTest(change=change):
                self.probe.return_value = {**self.video, **change}
                with self.assertRaises(ValueError):
                    self.validate()
        self.probe.return_value = {**self.video, "fps": 24000 / 1001, "duration": 4.0, "audio_codecs": []}
        self.assertEqual(self.validate()["generation_duration_seconds"], 4)
        with patch.object(h3, "MAX_VIDEO_BYTES", 1), self.assertRaisesRegex(ValueError, "50MB"):
            self.validate()

    def test_request_budget_and_prompt_are_checked_before_post(self):
        with patch.object(h3, "MAX_REQUEST_BYTES", 100), self.assertRaisesRegex(ValueError, "64MB"):
            self.validate()
        with self.assertRaisesRegex(ValueError, "7000"):
            self.validate(prompt="x" * 7000)

    def test_post_exactly_once_schema_and_saved_metadata(self):
        response = self.response({"task_id": "test-id-1"})
        with patch.object(h3.requests, "post", return_value=response) as post, patch.object(h3, "_poll", return_value={"ok": True}):
            result = h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
        self.assertEqual(result, {"ok": True})
        post.assert_called_once()
        self.assertEqual(post.call_args.args, (h3.CREATE_URL,))
        kwargs = post.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        payload = json.loads(kwargs["data"])
        self.assertEqual(set(payload), {"model", "content", "resolution", "duration", "ratio"})
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["ratio"], "adaptive")
        self.assertEqual([entry["type"] for entry in payload["content"]], ["text", "image_url", "image_url", "video_url"])
        self.assertEqual([entry.get("role") for entry in payload["content"][1:]], ["reference_image", "reference_image", "reference_video"])
        self.assertTrue(payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(payload["content"][-1]["video_url"]["url"].startswith("data:video/mp4;base64,"))
        self.assertEqual(len(kwargs["data"]), self.validate()["request_bytes"])
        task = h3._read_task(self.destination, h3.OPTION)
        self.assertEqual(task["task_id"], "test-id-1")
        checkpoint_text = h3._paths(self.destination)[0].read_text()
        self.assertNotIn("test-secret-only", checkpoint_text)
        self.assertNotIn("data:", checkpoint_text)
        self.assertTrue(h3._paths(self.destination)[1].is_file())

    def test_network_timeout_keeps_marker_and_blocks_repeat(self):
        with patch.object(h3.requests, "post", side_effect=requests.Timeout("secret-signed-url")) as post:
            with self.assertRaises(MotionSubmissionUncertainError) as caught:
                h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
            self.assertNotIn("secret-signed-url", str(caught.exception))
            with self.assertRaises(MotionSubmissionUncertainError):
                h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
            post.assert_called_once()
        self.assertTrue(h3._paths(self.destination)[1].is_file())
        self.assertFalse(h3._paths(self.destination)[0].exists())

    def test_official_402_refusal_is_terminal_and_saves_only_safe_proof(self):
        response = self.response(self.rejected(), status=402)
        with patch.object(h3.requests, "post", return_value=response) as post, patch.object(h3, "_poll") as poll:
            with self.assertRaises(MotionTerminalError) as caught:
                h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
        self.assertEqual(caught.exception.task_status, "rejected")
        self.assertEqual(caught.exception.http_status, 402)
        self.assertIn("按量账户余额", str(caught.exception))
        self.assertNotIn("private-url", str(caught.exception))
        post.assert_called_once()
        poll.assert_not_called()
        self.assertFalse(h3._paths(self.destination)[0].exists())
        marker = h3._read(h3._paths(self.destination)[1])
        self.assertEqual(marker["status"], "rejected")
        self.assertEqual(marker["provider_code"], "1008")
        self.assertEqual(marker["error_type"], "insufficient_balance_error")
        self.assertTrue(marker["rejection_verified"])
        self.assertNotIn("private-url", json.dumps(marker))
        self.assertNotIn("secret-key", json.dumps(marker))
        proof = h3.read_submission_rejection(self.destination)
        self.assertEqual(proof["http_status"], 402)
        self.assertEqual(set(proof), {"http_status", "error_type", "provider_code", "request_id", "rejection_verified"})

    def test_verified_refusal_neither_resumes_queries_nor_reposts(self):
        with patch.object(h3.requests, "post", return_value=self.response(self.rejected(), status=402)) as post:
            with self.assertRaises(MotionTerminalError):
                h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
            with patch.object(h3.requests, "get") as get:
                with self.assertRaises(MotionTerminalError) as caught:
                    h3.resume_motion(self.destination)
                self.assertEqual(caught.exception.task_status, "rejected")
                with self.assertRaises(MotionTerminalError):
                    h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
            get.assert_not_called()
        post.assert_called_once()

    def test_only_documented_matching_4xx_errors_are_definitive_refusals(self):
        for status in (400, 401, 402, 422, 429):
            with self.subTest(status=status):
                destination = self.folder / f"refused-{status}.mp4"
                with patch.object(h3.requests, "post", return_value=self.response(self.rejected(status), status=status)) as post:
                    with self.assertRaises(MotionTerminalError) as caught:
                        h3.video_edit(self.images, self.clip, destination, role_descriptions=self.descriptions)
                self.assertEqual(caught.exception.task_status, "rejected")
                self.assertEqual(h3.read_submission_rejection(destination)["http_status"], status)
                post.assert_called_once()

    def test_bare_status_server_error_bad_envelopes_or_task_evidence_stay_uncertain(self):
        cases = [
            (402, {}), (402, []), (402, {"error": "insufficient balance"}),
            (402, {**self.rejected(), "type": "other"}),
            (402, {**self.rejected(), "error": {"type": "insufficient_balance_error", "http_code": "401", "message": "rejected"}}),
            (402, {**self.rejected(), "error": {"type": "authorized_error", "http_code": "402", "message": "rejected"}}),
            (402, {**self.rejected(), "error": {"type": "insufficient_balance_error", "http_code": "402", "message": "conflicting (1004)"}}),
            (402, self.rejected(task_id="accepted-task")),
            (402, self.rejected(task_id="")),
            (402, self.rejected(task={"id": "accepted-task"})),
            (402, self.rejected(data={"task_id": "accepted-task"})),
            (500, self.rejected()), (503, self.rejected()), (403, self.rejected()),
        ]
        for index, (status, value) in enumerate(cases):
            with self.subTest(index=index):
                destination = self.folder / f"uncertain-{index}.mp4"
                with patch.object(h3.requests, "post", return_value=self.response(value, status=status)) as post:
                    with self.assertRaises(MotionSubmissionUncertainError):
                        h3.video_edit(self.images, self.clip, destination, role_descriptions=self.descriptions)
                self.assertEqual(h3._read(h3._paths(destination)[1])["status"], "submitting")
                self.assertIsNone(h3.read_submission_rejection(destination))
                post.assert_called_once()

    def test_unreadable_402_response_stays_uncertain(self):
        response = self.response(None, status=402)
        response.json.side_effect = ValueError("private response HTML")
        with patch.object(h3.requests, "post", return_value=response), self.assertRaises(MotionSubmissionUncertainError) as caught:
            h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
        self.assertNotIn("private response HTML", str(caught.exception))
        self.assertIsNone(h3.read_submission_rejection(self.destination))

    def test_refusal_checkpoint_save_failure_keeps_uncertainty(self):
        with patch.object(h3.requests, "post", return_value=self.response(self.rejected(), status=402)), \
             patch.object(h3, "_atomic_record", side_effect=MotionTransientError("private error")):
            with self.assertRaises(MotionSubmissionUncertainError) as caught:
                h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
        self.assertNotIn("private error", str(caught.exception))
        self.assertEqual(h3._read(h3._paths(self.destination)[1])["status"], "submitting")

    def test_rejection_reader_rejects_legacy_forged_and_conflicting_records(self):
        marker = {key: value for key, value in self.task().items() if key != "task_id"}
        valid = {**marker, "status": "rejected", "rejection_verified": True,
                 "http_status": 402, "error_type": "insufficient_balance_error"}
        for change in ({"status": "submitting"}, {"rejection_verified": False}, {"provider": "fal"},
                       {"option": "other"}, {"model": "other"}, {"endpoint": "https://evil.invalid"},
                       {"http_status": 500}, {"http_status": True}, {"http_status": "402"},
                       {"error_type": "authorized_error"}, {"task_id": "accepted-id"},
                       {"nested": {"id": "accepted-id"}}, {"resolution": []}, {"duration": 5.5}):
            with self.subTest(change=change):
                h3._paths(self.destination)[1].write_text(json.dumps({**valid, **change}))
                self.assertIsNone(h3.read_submission_rejection(self.destination))
        h3._paths(self.destination)[1].write_text(json.dumps(valid))
        self.assertEqual(h3.read_submission_rejection(self.destination)["http_status"], 402)
        for path in (self.destination, h3._paths(self.destination)[0], h3._paths(self.destination)[2]):
            path.write_text("{}")
            self.assertIsNone(h3.read_submission_rejection(self.destination))
            path.unlink()

    def test_request_id_is_allowlisted_and_raw_error_text_not_saved(self):
        value = self.rejected(request_id="https://private.invalid?secret=KEY")
        with patch.object(h3.requests, "post", return_value=self.response(value, status=402)), self.assertRaises(MotionTerminalError):
            h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions)
        record = h3._read(h3._paths(self.destination)[1])
        self.assertNotIn("request_id", record)
        self.assertNotIn("private.invalid", json.dumps(record))
        self.assertNotIn("private-url", json.dumps(record))

    def test_missing_or_corrupt_task_never_uploads_or_posts(self):
        for record in (None, {}, self.task(provider="fal"), self.task(model="other"),
                       self.task(endpoint="https://evil.invalid"), self.task(task_id="../bad"),
                       self.task(option="other"), self.task(resolution="1080P"), self.task(duration=5.2)):
            with self.subTest(record=record):
                path = h3._paths(self.destination)[0]
                path.unlink(missing_ok=True)
                if record is not None:
                    path.write_text(json.dumps(record))
                with patch.object(h3.requests, "post") as post, patch.object(h3.requests, "get") as get:
                    with self.assertRaises(MotionSubmissionUncertainError):
                        h3.resume_motion(self.destination)
                post.assert_not_called()
                get.assert_not_called()

    def test_resume_get_only_uses_exact_task_and_usage_whitelist(self):
        self.save_task()
        result = self.completed()
        result["task"]["usage"].update({"cost": 99, "secret": "private-url", "total_seconds": 10, "total_tokens": True})
        with patch.object(h3.requests, "get", return_value=self.response(result)) as get, patch.object(h3.requests, "post") as post, patch.object(h3, "_download") as download:
            info = h3.resume_motion(self.destination)
        post.assert_not_called()
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], h3.QUERY_BASE + "test-id-1")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        download.assert_called_once()
        self.assertEqual(info["usage"], {"input_seconds": 5, "output_seconds": 5, "input_image_count": 2, "total_seconds": 10})
        self.assertNotIn("actual_price", info)
        self.assertEqual(info["model"], "MiniMax-H3")

    def test_wrong_remote_identity_is_not_downloaded(self):
        self.save_task()
        with patch.object(h3.requests, "get", return_value=self.response(self.completed(id="wrong"))) as get, patch.object(h3.requests, "post") as post, patch.object(h3, "_download") as download:
            with self.assertRaises(MotionTransientError):
                h3.resume_motion(self.destination)
        self.assertEqual(get.call_count, 3)
        post.assert_not_called()
        download.assert_not_called()

    def test_terminal_failure_persisted_and_does_not_retry_or_leak(self):
        self.save_task()
        result = self.completed(status="failed", error={"message": "secret URL/key"})
        with patch.object(h3.requests, "get", return_value=self.response(result)) as get, patch.object(h3.requests, "post") as post:
            with self.assertRaises(MotionTerminalError) as caught:
                h3.resume_motion(self.destination)
            self.assertEqual(caught.exception.task_status, "failed")
            self.assertNotIn("secret", str(caught.exception))
            with self.assertRaises(MotionTerminalError):
                h3.resume_motion(self.destination)
        get.assert_called_once()
        post.assert_not_called()
        self.assertEqual(h3._read(h3._paths(self.destination)[2])["status"], "failed")

    def test_valid_local_source_resumes_without_key_or_http(self):
        task = self.save_task()
        self.destination.write_bytes(b"valid-tested-source")
        h3._atomic_record(h3._paths(self.destination)[2], {**task, "status": "done", "usage": {"input_seconds": 5}})
        with patch.dict(os.environ, {"MINIMAX_API_KEY": ""}), patch.object(h3.requests, "post") as post, patch.object(h3.requests, "get") as get:
            info = h3.resume_motion(self.destination)
        self.assertTrue(info["reused_local_video"])
        self.assertEqual(info["usage"]["input_seconds"], 5)
        get.assert_not_called()
        post.assert_not_called()

    def test_expired_cached_download_refreshes_get_only(self):
        task = self.save_task()
        h3._atomic_record(h3._paths(self.destination)[2], {**task, "status": "done", "video_url": "https://cdn.example.com/old"})
        with patch.object(h3, "_download", side_effect=[h3._URLUnavailable("expired"), None]) as download, patch.object(h3.requests, "get", return_value=self.response(self.completed())) as get, patch.object(h3.requests, "post") as post:
            h3.resume_motion(self.destination)
        self.assertEqual(download.call_count, 2)
        get.assert_called_once()
        post.assert_not_called()

    def test_shutdown_stops_before_submission(self):
        stop = threading.Event()
        stop.set()
        with patch.object(h3.requests, "post") as post, self.assertRaises(MotionCancelledError):
            h3.video_edit(self.images, self.clip, self.destination, role_descriptions=self.descriptions, stop_event=stop)
        post.assert_not_called()
        self.assertFalse(h3._paths(self.destination)[1].exists())

    def test_download_is_atomic_and_rejects_invalid_video(self):
        response = self.response({})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b"some-video"]
        with patch.object(h3.requests, "get", return_value=response), patch.object(h3, "_valid_source", return_value=False):
            with self.assertRaises(MotionTransientError):
                h3._download("https://cdn.example.com/result.mp4", self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_name(self.destination.name + ".download").exists())
        response.iter_content.return_value = [b"some-video"]
        with patch.object(h3.requests, "get", return_value=response), patch.object(h3, "_valid_source", return_value=True):
            h3._download("https://cdn.example.com/result.mp4", self.destination)
        self.assertEqual(self.destination.read_bytes(), b"some-video")


if __name__ == "__main__":
    unittest.main()

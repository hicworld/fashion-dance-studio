"""Offline checks for Jimeng preflight and accepted-task persistence only."""

import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import providers_jimeng as provider


class JimengSubmissionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clip = self.root / "reference.mp4"
        self.clip.write_bytes(bytes(range(64)))
        self.destination = self.root / "result.mp4"
        self.task_path = self.root / "result_task.json"
        self.signed_url = "https://example.invalid/reference.mp4?signature=do-not-log"
        self.sdk_module = types.ModuleType("volcengine.visual.VisualService")
        self.sdk_module.VisualService = Mock()

    def _patch_inputs(self, stack):
        stack.enter_context(patch.dict(sys.modules, {
            "volcengine.visual.VisualService": self.sdk_module,
        }))
        stack.enter_context(patch.object(provider, "available", return_value=[]))
        stack.enter_context(patch.object(provider, "_motion_image_base64", return_value="PRIVATE_IMAGE_BASE64"))
        stack.enter_context(patch.object(provider, "prepare_motion_clip", return_value=self.clip))
        upload = stack.enter_context(patch.object(provider.storage_tos, "upload_file", return_value=self.signed_url))
        stack.enter_context(patch.object(provider.requests, "head", side_effect=AssertionError("HEAD must not be used")))
        return upload

    @staticmethod
    def _response(status, chunks):
        response = Mock()
        response.status_code = status
        response.iter_content.return_value = iter(chunks)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_get_preflight_failure_never_submits(self):
        cases = [
            {"return_value": self._response(403, [])},
            {"return_value": self._response(200, [b"wrong-content"])},
            {"side_effect": requests.ConnectionError(self.signed_url)},
        ]
        for get_configuration in cases:
            with self.subTest(get_configuration=get_configuration), ExitStack() as stack:
                self._patch_inputs(stack)
                get = stack.enter_context(patch.object(provider.requests, "get", **get_configuration))
                visual_call = stack.enter_context(patch.object(provider, "_visual_call"))
                with self.assertRaisesRegex(RuntimeError, "已阻止提交") as error:
                    provider.motion(self.root / "model.png", self.clip, self.destination, "volc-dreamactor-2")
                self.assertNotIn(self.signed_url, str(error.exception))
                self.assertNotIn("do-not-log", str(error.exception))
                self.assertTrue(get.call_args.kwargs["stream"])
                self.assertEqual(get.call_args.kwargs["headers"], {"Range": "bytes=0-31"})
                visual_call.assert_not_called()
                self.assertFalse(self.task_path.exists())
                self.assertFalse(self.destination.exists())

    def test_accepted_task_is_saved_before_query_failure_and_not_resubmitted(self):
        expected = {
            "task_id": "task-123", "req_key": provider.DREAMACTOR_MODEL,
            "request_id": "request-456",
        }
        calls = []

        def visual_call(service, method, payload):
            calls.append(method)
            if method == "cv_sync2async_submit_task":
                return {"code": 10000, "request_id": "request-456", "data": {"task_id": "task-123"}}
            self.assertEqual(method, "cv_sync2async_get_result")
            self.assertEqual(json.loads(self.task_path.read_text(encoding="utf-8")), expected)
            raise RuntimeError("离线模拟查询失败")

        response = self._response(206, [bytes(range(8)), bytes(range(8, 32))])
        with ExitStack() as stack:
            upload = self._patch_inputs(stack)
            get = stack.enter_context(patch.object(provider.requests, "get", return_value=response))
            stack.enter_context(patch.object(provider, "_visual_call", side_effect=visual_call))
            stack.enter_context(patch.object(provider.time, "sleep"))
            download = stack.enter_context(patch.object(provider, "download_file"))
            with self.assertRaisesRegex(RuntimeError, "离线模拟查询失败"):
                provider.motion(self.root / "model.png", self.clip, self.destination, "volc-dreamactor-2")
            self.assertEqual(json.loads(self.task_path.read_text(encoding="utf-8")), expected)
            self.assertEqual(calls.count("cv_sync2async_submit_task"), 1)
            self.assertFalse(self.destination.exists())
            download.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "已阻止重复提交"):
                provider.motion(self.root / "model.png", self.clip, self.destination, "volc-dreamactor-2")
            upload.assert_called_once()
            get.assert_called_once()
            self.assertEqual(calls.count("cv_sync2async_submit_task"), 1)

            # An existing result also blocks before upload, even without a checkpoint.
            self.task_path.unlink()
            self.destination.write_bytes(b"existing result")
            with self.assertRaisesRegex(RuntimeError, "已阻止重复提交"):
                provider.motion(self.root / "model.png", self.clip, self.destination, "volc-dreamactor-2")
            upload.assert_called_once()
            self.assertEqual(calls.count("cv_sync2async_submit_task"), 1)


if __name__ == "__main__":
    unittest.main()

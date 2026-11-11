"""Offline two-image background editing and paid-request reuse safety."""

import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

import providers
import providers_jimeng as provider


class BackgroundProviderTests(unittest.TestCase):
    URL = "https://example.invalid/background.png?signature=private-background-url"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = self.root / "model.png"
        self.background = self.root / "background.png"
        self.garment = self.root / "garment.png"
        self.destination = self.root / "model_wearing_garment.png"
        self.checkpoint = self.root / "seedream_request.private.json"
        for path, color in ((self.model, (230, 20, 20)), (self.background, (20, 230, 20)),
                            (self.garment, (20, 20, 230))):
            Image.new("RGB", (90, 160), color).save(path)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {"ARK_API_KEY": "offline-background-key", "SEEDREAM_MODEL": provider.SEEDREAM_DEFAULT}
        self.stack.enter_context(patch.object(os, "environ", self.environment))
        response = Mock(ok=True, status_code=200, headers={"x-request-id": "offline-background-request"})
        response.json.return_value = {"data": [{"url": self.URL}], "usage": {"generated_images": 1}}
        self.submit = self.stack.enter_context(patch.object(provider.requests, "post", return_value=response))
        self.stack.enter_context(patch.object(provider.requests, "get", side_effect=AssertionError("no real HTTP")))
        self.download = self.stack.enter_context(patch.object(provider, "download_file", side_effect=self.write_image))
        self.motion = self.stack.enter_context(patch.object(provider, "motion", side_effect=AssertionError("image previews never run video")))

    @staticmethod
    def write_image(url, destination):
        Image.new("RGB", (144, 256), "white").save(destination, format="PNG")

    def edit(self, **kwargs):
        return providers.compose_background(self.model, self.background, self.destination, **kwargs)

    def test_fixed_two_image_order_and_preservation_prompt(self):
        metadata = self.edit(prompt="室内柔光")
        self.submit.assert_called_once()
        payload = self.submit.call_args.kwargs["json"]
        self.assertEqual(payload["size"], "1440x2560")
        self.assertEqual(len(payload["image"]), 2)
        for encoded, original in zip(payload["image"], (self.model, self.background)):
            with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as sent, Image.open(original) as source:
                self.assertEqual(sent.getpixel((0, 0)), source.getpixel((0, 0)))
        for constraint in ("已经完成穿搭", "不换衣", "不增减衣物", "配饰", "姿态", "全身构图",
                           "图2只作为场景背景参考", "不要提取图2中的人物", "不额外生成多人", "室内柔光"):
            self.assertIn(constraint, payload["prompt"])
        self.assertEqual(metadata["reference_roles"], ["dressed_model", "background"])
        self.assertEqual(metadata["operation"], "background_replace")
        self.assertEqual(metadata["usage"], {"generated_images": 1})
        self.assertNotIn(self.URL, json.dumps(metadata))
        self.assertNotIn(self.environment["ARK_API_KEY"], json.dumps(metadata))
        self.assertTrue(self.destination.is_file())
        record = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(record["operation"], "background_replace")
        self.assertEqual(len(record["operation_sha256"]), 64)
        self.assertEqual(len(record["input_sha256"]), 2)
        self.motion.assert_not_called()

    def test_only_safe_numeric_usage_is_exposed_without_raw_response(self):
        self.submit.return_value.json.return_value["usage"].update(
            input_images=2, output_tokens=100, total_tokens=110,
            secret_url=self.URL, api_key=self.environment["ARK_API_KEY"],
            tool_usage={"web_search": 0, "raw_payload": self.URL},
        )
        result = self.edit()
        self.assertEqual(result["usage"], {"generated_images": 1, "input_images": 2,
            "output_tokens": 100, "total_tokens": 110, "tool_usage": {"web_search": 0}})
        self.assertNotIn(self.URL, json.dumps(result))
        self.assertNotIn(self.environment["ARK_API_KEY"], json.dumps(result))
        self.submit.assert_called_once()

    def test_download_failure_recovers_same_url_without_key_or_resubmission(self):
        self.download.side_effect = requests.ConnectionError(self.URL)
        with self.assertRaisesRegex(RuntimeError, "已生成但下载") as error:
            self.edit()
        self.assertNotIn(self.URL, str(error.exception))
        self.assertFalse(self.destination.exists())
        self.assertEqual(json.loads(self.checkpoint.read_text(encoding="utf-8"))["image_url"], self.URL)
        self.environment.pop("ARK_API_KEY")
        self.download.side_effect = self.write_image
        self.submit.side_effect = AssertionError("cannot submit twice")
        info = self.edit()
        self.assertEqual(info["request_id"], "offline-background-request")
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.download.call_args.args[0], self.URL)
        self.assertFalse(list(self.root.glob("*.download")))
        self.assertFalse(list(self.root.glob("*.normalized")))

    def test_completed_preview_reuses_without_submit_or_download(self):
        first = self.edit()
        self.submit.reset_mock()
        self.download.reset_mock()
        second = self.edit()
        self.assertEqual(second, first)
        self.submit.assert_not_called()
        self.download.assert_not_called()

    def test_pending_submit_blocks_repeat_when_response_was_lost(self):
        self.submit.side_effect = requests.Timeout("offline response lost")
        with self.assertRaisesRegex(RuntimeError, "提交结果不确定"):
            self.edit()
        self.assertEqual(json.loads(self.checkpoint.read_text(encoding="utf-8"))["status"], "submitting")
        with self.assertRaisesRegex(RuntimeError, "已有提交记录"):
            self.edit()
        self.assertEqual(self.submit.call_count, 1)

    def test_changed_input_role_or_requirement_never_reuses_existing_request(self):
        self.edit()
        original = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        mutations = [
            {**original, "operation": "fashion_compose"},
            {**original, "reference_roles": ["background", "dressed_model"]},
            {**original, "input_sha256": ["wrong", "hash"]},
        ]
        for record in mutations:
            with self.subTest(field=next(key for key in record if record[key] != original.get(key))):
                self.checkpoint.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "其他操作"):
                    self.edit()
        self.checkpoint.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "其他操作"):
            self.edit(prompt="different request")
        self.assertEqual(self.submit.call_count, 1)

    def test_fashion_compose_and_background_edit_reject_each_others_checkpoint(self):
        self.edit()
        with self.assertRaisesRegex(RuntimeError, "其他编辑操作"):
            provider.compose(self.model, self.garment, self.background, self.destination)
        self.checkpoint.unlink()
        self.destination.unlink()
        provider.compose(self.model, self.garment, self.background, self.destination)
        with self.assertRaisesRegex(RuntimeError, "其他操作"):
            self.edit()
        self.assertEqual(self.submit.call_count, 2)

    def test_invalid_image_or_existing_untracked_output_blocks_paid_request(self):
        self.destination.write_bytes(b"existing untracked output")
        with self.assertRaisesRegex(RuntimeError, "已阻止重复提交"):
            self.edit()
        self.destination.unlink()
        Image.new("RGB", (10, 10), "white").save(self.background)
        with self.assertRaises(ValueError):
            self.edit()
        self.submit.assert_not_called()

    def test_invalid_download_does_not_overwrite_preview_and_can_be_retrieved(self):
        self.download.side_effect = lambda url, path: Path(path).write_bytes(b"not an image")
        with self.assertRaisesRegex(RuntimeError, "下载或校验失败"):
            self.edit()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.submit.call_count, 1)
        self.download.side_effect = self.write_image
        self.edit()
        with Image.open(self.destination) as opened:
            opened.verify()
        self.assertEqual(self.submit.call_count, 1)


if __name__ == "__main__":
    unittest.main()

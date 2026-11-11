"""Offline source-video edit validation, role mapping and durable recovery."""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from PIL import Image

import providers
import providers_fal as fal
from test_fal_recovery import Response


class VideoEditProviderTests(unittest.TestCase):
    option = "fal-kling-o3-pro-edit"
    identifier = "edit-request-123"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.references = [self.folder / f"role_{index}.png" for index in range(1, 4)]
        for image in self.references:
            Image.new("RGB", (512, 768), "white").save(image)
        self.clip = self.folder / "reference.mp4"
        self.clip.write_bytes(b"offline reference" * 8)
        self.output = self.folder / "fashion_dance_source.mp4"
        self.roles = ["左侧穿红衣的人", "右侧穿蓝衣的人", "后方戴帽子的人"]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"FAL_KEY": "offline-edit-key"}))
        self.original_probe = fal._probe
        self.probe = self.stack.enter_context(patch.object(fal, "_probe", return_value={"width": 720, "height": 960, "duration": 5, "fps": 30}))
        self.client = SimpleNamespace(upload_file=Mock(side_effect=lambda path: "https://upload.test/" + path.name))
        self.new_client = self.stack.enter_context(patch.object(fal, "_new_upload_client", return_value=self.client))
        self.post = self.stack.enter_context(patch.object(fal.requests, "post", return_value=Response({"request_id": self.identifier})))
        self.get = self.stack.enter_context(patch.object(fal.requests, "get", side_effect=self.success_get))
        self.wait = self.stack.enter_context(patch.object(fal, "_wait"))

    def success_get(self, url, **kwargs):
        if url.endswith("/status"):
            task = json.loads(fal._paths(self.output)[0].read_text(encoding="utf-8"))
            self.assertEqual(task["request_id"], self.identifier)
            self.assertEqual(task["endpoint"], fal.EDIT_ENDPOINTS[self.option])
            return Response({"request_id": self.identifier, "status": "COMPLETED"})
        if url.startswith(fal.QUEUE_REQUESTS):
            return Response({"video": {"url": "https://download.test/edit.mp4?signature=private"}})
        return Response()

    def task(self):
        value = {"provider": "fal", "task_id": self.identifier, "request_id": self.identifier,
                 "endpoint": fal.EDIT_ENDPOINTS[self.option], "option": self.option}
        fal._paths(self.output)[0].write_text(json.dumps(value), encoding="utf-8")
        return value

    def edit(self, **kwargs):
        return providers.edit_video(self.references[:2], self.clip, self.output, self.option,
                                    role_descriptions=self.roles[:2], prompt="保留人物互相看向对方的互动", **kwargs)

    def test_ordered_roles_use_edit_payload_and_single_post_without_motion_fields(self):
        events = []
        result = self.edit(progress=events.append)
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], fal.QUEUE_BASE + fal.EDIT_ENDPOINTS[self.option])
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["image_urls"], ["https://upload.test/role_1.png", "https://upload.test/role_2.png"])
        self.assertEqual(body["video_url"], "https://upload.test/reference.mp4")
        self.assertTrue(body["keep_audio"])
        self.assertTrue(body["enable_safety_checker"])
        self.assertIn("@Video1", body["prompt"])
        self.assertIn("Source character 1: 左侧穿红衣的人. Replace only this character with the appearance of @Image1.", body["prompt"])
        self.assertIn("Source character 2: 右侧穿蓝衣的人. Replace only this character with the appearance of @Image2.", body["prompt"])
        self.assertIn("interactions", body["prompt"])
        self.assertIn("camera motion", body["prompt"])
        self.assertIn("保留人物互相看向对方的互动", body["prompt"])
        for field in ("image_url", "character_orientation", "keep_original_sound", "elements"):
            self.assertNotIn(field, body)
        self.assertEqual(self.client.upload_file.call_count, 3)
        self.assertEqual(result["operation"], "video_edit")
        self.assertTrue(result["keep_audio"])
        self.assertNotIn("character_orientation", result)
        self.assertNotIn("keep_original_sound", result)
        self.assertEqual(events[-1]["status"], "downloaded")
        self.assertTrue(self.output.is_file())
        for path in fal._paths(self.output)[:2]:
            text = path.read_text(encoding="utf-8")
            for private in ("https://", "offline-edit-key", "左侧穿红衣的人"):
                self.assertNotIn(private, text)

    def test_query_failure_retains_edit_id_and_recovery_never_uploads_or_submits(self):
        self.get.side_effect = requests.Timeout("offline")
        with self.assertRaises(fal.MotionTransientError):
            self.edit()
        self.post.assert_called_once()
        self.assertFalse(self.output.exists())
        self.assertTrue(fal._paths(self.output)[0].exists())
        self.get.side_effect = self.success_get
        self.client.upload_file.reset_mock()
        self.new_client.reset_mock()
        result = providers.resume_motion(self.output, self.option)
        self.assertEqual(result["operation"], "video_edit")
        self.assertEqual(result["task_id"], self.identifier)
        self.assertTrue(self.output.is_file())
        self.post.assert_called_once()
        self.client.upload_file.assert_not_called()
        self.new_client.assert_not_called()
        self.assertTrue(all(call.args[0].startswith(fal.QUEUE_REQUESTS) or call.args[0].startswith("https://download.test/")
                            for call in self.get.call_args_list))

    def test_unknown_submit_keeps_marker_and_blocks_validation_upload_and_reposting(self):
        self.post.side_effect = requests.Timeout("unknown")
        with self.assertRaises(fal.MotionSubmissionUncertainError):
            self.edit()
        self.post.assert_called_once()
        self.assertTrue(fal._paths(self.output)[1].exists())
        self.assertFalse(fal._paths(self.output)[0].exists())
        self.new_client.reset_mock()
        with patch.object(fal, "validate_edit_inputs", side_effect=AssertionError("existing marker must win")):
            with self.assertRaises(fal.MotionSubmissionUncertainError):
                self.edit()
            with self.assertRaises(fal.MotionSubmissionUncertainError):
                providers.resume_motion(self.output, self.option)
        self.new_client.assert_not_called()
        self.get.assert_not_called()
        self.post.assert_called_once()

    def test_edit_local_source_and_cached_link_resume_without_key_or_input_materials(self):
        task = self.task()
        fal._paths(self.output)[2].write_text(json.dumps({**task, "status": "done", "video_url": "https://download.test/cached-edit.mp4"}), encoding="utf-8")
        for path in self.references + [self.clip]:
            path.unlink()
        with patch.dict(os.environ, {"FAL_KEY": ""}):
            first = providers.resume_motion(self.output, self.option)
            self.get.assert_called_once()
            self.assertEqual(first["operation"], "video_edit")
            self.get.reset_mock()
            second = providers.resume_motion(self.output, self.option)
        self.assertTrue(second["reused_local_video"])
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.new_client.assert_not_called()

    def test_edit_endpoint_record_is_not_reused_as_motion_control(self):
        self.task()
        with self.assertRaises(fal.MotionSubmissionUncertainError):
            providers.resume_motion(self.output, "fal-kling-v3-pro")
        with self.assertRaises(ValueError):
            fal.motion(self.references[0], self.clip, self.output, self.option)
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.new_client.assert_not_called()

    def test_reference_count_roles_and_total_prompt_limit_reject_before_upload(self):
        cases = [([], [], ""), (self.references + self.references[:1], self.roles + ["第四人"], ""),
                 (self.references[:2], self.roles[:1], ""), (self.references[:1], [" "], ""),
                 (self.references[:1], ["左侧人物"], "长" * 2500),
                 (self.references[:1], ["原人物" * 900], "")]
        for refs, roles, prompt in cases:
            with self.subTest(count=len(refs), description_count=len(roles), prompt_length=len(prompt)):
                with self.assertRaises(ValueError):
                    fal.video_edit(refs, self.clip, self.output, self.option, role_descriptions=roles, prompt=prompt)
        for count in (1, 2, 3):
            checked = fal.validate_edit_inputs(self.references[:count], self.clip, self.option, role_descriptions=self.roles[:count])
            self.assertEqual(checked["reference_count"], count)
            self.assertIn(f"@Image{count}", checked["request_prompt"])
        self.post.assert_not_called()
        self.get.assert_not_called()
        self.new_client.assert_not_called()

    def test_edit_video_dimensions_duration_and_frame_rate_are_local_gates(self):
        valid = {"width": 720, "height": 960, "duration": 5, "fps": 30}
        cases = [{**valid, "width": 719}, {**valid, "height": 3841}, {**valid, "duration": 2.99},
                 {**valid, "duration": 15.06}, {**valid, "fps": 22.99}, {**valid, "fps": 60.01},
                 {**valid, "fps": float("nan")}, {"width": 720, "height": 960, "duration": 5}]
        for info in cases:
            with self.subTest(info=info):
                self.probe.return_value = info
                with self.assertRaises(ValueError):
                    self.edit()
        for fps in (23, 60):
            self.probe.return_value = {"width": 720, "height": 3840, "duration": 15.05, "fps": fps}
            self.assertEqual(fal.validate_edit_inputs(self.references, self.clip, self.option, role_descriptions=self.roles)["video_fps"], fps)
        self.post.assert_not_called()
        self.get.assert_not_called()
        self.new_client.assert_not_called()

    def test_edit_image_and_file_size_limits_reject_without_modifying_materials(self):
        for size in ((299, 512), (300, 800), (800, 300)):
            with self.subTest(size=size):
                Image.new("RGB", size).save(self.references[0])
                before = self.references[0].read_bytes()
                with self.assertRaises(ValueError):
                    self.edit()
                self.assertEqual(self.references[0].read_bytes(), before)
        Image.new("RGB", (512, 768)).save(self.references[0])
        with self.references[0].open("r+b") as image:
            image.truncate(fal.MAX_EDIT_IMAGE_BYTES + 1)
        with self.assertRaises(ValueError):
            self.edit()
        Image.new("RGB", (512, 768)).save(self.references[0])
        with self.clip.open("r+b") as video:
            video.truncate(fal.MAX_EDIT_VIDEO_BYTES + 1)
        with self.assertRaises(ValueError):
            self.edit()
        self.post.assert_not_called()
        self.get.assert_not_called()
        self.new_client.assert_not_called()

    def test_probe_supports_fractional_fps_and_zero_denominator_without_breaking_motion(self):
        response = SimpleNamespace(stdout=json.dumps({"streams": [{"width": 720, "height": 960, "avg_frame_rate": "0/0", "r_frame_rate": "30000/1001"}], "format": {"duration": "5"}}))
        with patch.object(fal.subprocess, "run", return_value=response):
            # setUp patches the probe for provider isolation; exercise the saved
            # real parser against a synthetic ffprobe response instead.
            info = self.original_probe(self.clip)
            self.assertAlmostEqual(info["fps"], 30000 / 1001)
            response.stdout = json.dumps({"streams": [{"width": 720, "height": 960, "avg_frame_rate": "0/0"}], "format": {"duration": "5"}})
            self.assertEqual(self.original_probe(self.clip)["fps"], 0)
        self.post.assert_not_called()

    def test_edit_catalog_and_resume_allowlist_are_public_and_consistent(self):
        self.assertEqual(providers.EDIT_ENDPOINTS, fal.EDIT_ENDPOINTS)
        self.assertEqual(providers.FAL_VIDEO_ENDPOINTS[self.option], fal.EDIT_ENDPOINTS[self.option])
        option = providers.option_for("edit", self.option)
        self.assertEqual(option["price"], {"currency": "USD", "per_run": 0, "per_second": 0.168})
        with patch.dict(os.environ, {"FAL_KEY": ""}):
            self.assertTrue(providers.missing_for("edit", self.option))
        self.post.assert_not_called()
        self.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()

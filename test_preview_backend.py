"""Offline coverage for explicit preview confirmation and private settings."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import app_settings

# Import the backend without opening the user's actual .env.
with patch.object(app_settings, "load_settings"):
    import app as workflow


class InlineThread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class PreviewBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.jobs = self.folder / "jobs"
        self.jobs.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(workflow, "JOBS", self.jobs))
        self.stack.enter_context(patch.object(workflow, "job_states", {}))
        self.stack.enter_context(patch.object(workflow, "_claimed_jobs", set()))
        self.stack.enter_context(patch.object(workflow, "_recovery_jobs", set()))
        self.stack.enter_context(patch.object(workflow, "_worker_threads", {}))
        self.stack.enter_context(patch.object(app_settings, "ENV_PATH", self.folder / ".env"))
        self.stack.enter_context(patch.object(workflow.providers, "missing_for", return_value=[]))
        self.stack.enter_context(patch.object(workflow, "video_duration", return_value=5))
        self.stack.enter_context(patch.object(workflow, "video_metrics", side_effect=lambda path:
            {"duration": 6.08 if path.name == "fashion_dance_source.mp4" else 5, "fps": 25}))
        self.stack.enter_context(patch.object(workflow, "trim_video", side_effect=lambda source, destination, *args:
            destination.write_bytes(b"offline reference")))
        self.stack.enter_context(patch("providers_jimeng.prepare_motion_clip", side_effect=lambda clip: clip))
        self.client = workflow.app.test_client()

    @staticmethod
    def image_file(name):
        buffer = io.BytesIO()
        Image.new("RGB", (512, 768), "white").save(buffer, format="PNG")
        buffer.seek(0)
        return buffer, name

    def upload(self, mode="compose", **extra):
        data = {"mode": mode, "model": self.image_file("model.png"),
                "video": (io.BytesIO(b"offline video"), "dance.mp4"), **extra}
        if mode == "compose":
            data.setdefault("garment", self.image_file("garment.png"))
            if data.get("background_mode") != "video_frame":
                data.setdefault("background", self.image_file("background.png"))
        return self.client.post("/api/jobs", data=data, content_type="multipart/form-data")

    def prepare_preview(self, mode="compose", **extra):
        def compose(model, garment, background, destination, option, prompt="", *, pants=None, garment_mode="outfit"):
            Image.new("RGB", (512, 768), "white").save(destination)
            return {"model": "offline-compose"}

        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow.providers, "compose", side_effect=compose, create=True) as composition:
            response = self.upload(mode, **extra)
        self.assertEqual(response.status_code, 202, response.json)
        return response.json["id"], composition

    def test_both_input_modes_stop_at_preview_without_motion(self):
        with patch.object(workflow.providers, "motion") as motion:
            first_id, composition = self.prepare_preview()
            composition.assert_called_once()
            second_id, ready_composition = self.prepare_preview("ready_image")
            ready_composition.assert_not_called()
        motion.assert_not_called()
        for job_id in (first_id, second_id):
            state = self.client.get(f"/api/jobs/{job_id}").json
            self.assertEqual(state["status"], "awaiting_confirmation")
            self.assertTrue(state["imageURL"].endswith("model_wearing_garment.png"))
            self.assertIsNone(state["video"])
            self.assertTrue((self.jobs / job_id / "job_state.json").is_file())
        self.assertEqual(len(self.client.get("/api/jobs").json["jobs"]), 2)

    def test_ready_image_original_background_ignores_hidden_images_and_needs_no_image_api(self):
        with patch.object(workflow.providers, "compose_background", create=True) as background_api, \
             patch.object(workflow.providers, "motion") as motion, \
             patch.object(workflow.providers, "missing_for", side_effect=AssertionError("original image needs no ARK")):
            for fields in ({}, {"background_mode": "original"}):
                with self.subTest(fields=fields):
                    job_id, compose = self.prepare_preview(
                        "ready_image", **fields,
                        background=(io.BytesIO(b"ignored invalid background"), "hidden.png"),
                        garment=(io.BytesIO(b"ignored garment"), "hidden-garment.png"),
                        pants=(io.BytesIO(b"ignored pants"), "hidden-pants.png"),
                    )
                    compose.assert_not_called()
                    folder = self.jobs / job_id
                    state = self.client.get(f"/api/jobs/{job_id}").json
                    config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
                    self.assertEqual(state["background_mode"], "original")
                    self.assertEqual(config["background_mode"], "original")
                    self.assertEqual(state["status"], "awaiting_confirmation")
                    self.assertTrue((folder / "model_wearing_garment.png").is_file())
                    for name in ("model.png", "background.png", "garment.png", "pants.png"):
                        self.assertFalse((folder / name).exists())
            background_api.assert_not_called()
            motion.assert_not_called()
        # Old ready-image histories lack the field and retain their original image.
        saved = json.loads((folder / "job_state.json").read_text(encoding="utf-8"))
        saved.pop("background_mode")
        (folder / "job_state.json").write_text(json.dumps(saved), encoding="utf-8")
        workflow.job_states.clear()
        workflow.restore_job_history()
        self.assertEqual(workflow.job_states[job_id]["background_mode"], "original")
        self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")

    def test_ready_image_replaces_background_once_then_waits_for_video_confirmation(self):
        def replace(model, background, destination, option, prompt=""):
            Image.new("RGB", (512, 768), "blue").save(destination)
            return {"model": "offline-background-edit", "background_replaced": True}

        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow.providers, "compose_background", side_effect=replace, create=True) as background_api, \
             patch.object(workflow.providers, "compose") as compose, \
             patch.object(workflow.providers, "motion") as motion:
            response = self.upload("ready_image", background_mode="replace", background=self.image_file("background.png"),
                                   prompt="使用给定场景", garment_mode="ignored", pants=(io.BytesIO(b"ignored"), "hidden.png"))
            self.assertEqual(response.status_code, 202, response.json)
            job_id = response.json["id"]
            folder = self.jobs / job_id
            background_api.assert_called_once_with(folder / "model.png", folder / "background.png",
                                                   folder / "model_wearing_garment.png", "volc-seedream", prompt="使用给定场景")
            compose.assert_not_called()
            motion.assert_not_called()
            self.assertFalse((folder / "garment.png").exists())
            self.assertFalse((folder / "pants.png").exists())
            state = self.client.get(f"/api/jobs/{job_id}").json
            self.assertEqual(state["status"], "awaiting_confirmation")
            self.assertEqual(state["background_mode"], "replace")
            self.assertIsNone(state["video"])
            self.assertEqual(state["actual_models"]["image"]["model"], "offline-background-edit")
            self.assertEqual(json.loads((folder / "job_config.json").read_text(encoding="utf-8"))["background_mode"], "replace")
            workflow.job_states.clear()
            workflow.restore_job_history()
            self.assertEqual(workflow.job_states[job_id]["background_mode"], "replace")
            self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
            background_api.assert_called_once()

    def test_ready_image_background_validation_never_starts_paid_work(self):
        cases = [
            ({"background_mode": "unknown"}, [], "背景模式"),
            ({"background_mode": "replace"}, [], "背景图片"),
            ({"background_mode": "replace", "background": self.image_file("background.png")}, ["请配置 ARK_API_KEY"], "ARK_API_KEY"),
            ({"background_mode": "replace", "background": (io.BytesIO(b"invalid image"), "background.png")}, [], "背景图"),
        ]
        with patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow.providers, "compose_background", create=True) as background_api, \
             patch.object(workflow.providers, "compose") as compose, \
             patch.object(workflow.providers, "motion") as motion:
            for fields, missing, expected in cases:
                with self.subTest(expected=expected), patch.object(workflow.providers, "missing_for", return_value=missing):
                    response = self.upload("ready_image", **fields)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(expected, response.json["error"])
            thread.assert_not_called()
            background_api.assert_not_called()
            compose.assert_not_called()
            motion.assert_not_called()
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_video_frame_background_uses_selected_clip_and_stops_both_modes_at_preview(self):
        events = []

        def trim(source, clip, start, seconds):
            events.append(("trim", start, seconds))
            clip.write_bytes(b"selected clip first frame")

        def frame(clip, destination):
            events.append(("frame", clip.name))
            self.assertEqual(clip.read_bytes(), b"selected clip first frame")
            Image.new("RGB", (512, 768), "blue").save(destination)

        def prepare(clip):
            events.append(("prepare", clip.name))
            prepared = clip.with_name("dance_clip_jimeng.mp4")
            prepared.write_bytes(b"normalized reference")
            return prepared

        def compose(*args, **kwargs):
            Image.new("RGB", (512, 768), "white").save(args[3])
            return {"model": "offline-compose", "reference_count": 3}

        def background(*args, **kwargs):
            Image.new("RGB", (512, 768), "white").save(args[2])
            return {"model": "offline-background", "reference_count": 2}

        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow, "video_duration", return_value=20), \
             patch.object(workflow, "trim_video", side_effect=trim), \
             patch.object(workflow, "extract_background_frame", side_effect=frame) as extractor, \
             patch("providers_jimeng.prepare_motion_clip", side_effect=prepare), \
             patch.object(workflow.providers, "compose", side_effect=compose) as composition, \
             patch.object(workflow.providers, "compose_background", side_effect=background) as background_api, \
             patch.object(workflow.providers, "motion") as motion:
            for mode in ("ready_image", "compose"):
                for hidden_background in (False, True):
                    with self.subTest(mode=mode, hidden_background=hidden_background):
                        fields = {"background_mode": "video_frame", "start": "2", "seconds": "5"}
                        if hidden_background:
                            fields["background"] = (io.BytesIO(b"ignore invalid hidden background"), "hidden.png")
                        events.clear()
                        response = self.upload(mode, **fields)
                        self.assertEqual(response.status_code, 202, response.json)
                        job_id = response.json["id"]
                        folder = self.jobs / job_id
                        state = self.client.get(f"/api/jobs/{job_id}").json
                        self.assertEqual(state["status"], "awaiting_confirmation")
                        self.assertEqual(state["background_mode"], "video_frame")
                        self.assertEqual(events, [("trim", 2, 5), ("frame", "dance_clip.mp4"), ("prepare", "dance_clip.mp4")])
                        self.assertFalse((folder / "background.png").exists())
                        config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
                        self.assertEqual(config["background_mode"], "video_frame")
                        self.assertEqual(config["start"], 2)
                        info = json.loads((folder / "image_info.json").read_text(encoding="utf-8"))
                        self.assertEqual(info["background_reference_start_seconds"], 2)
                        self.assertIn("不逐帧保留", info["background_reference_note"])
                        if mode == "ready_image":
                            self.assertEqual(background_api.call_args.args, (folder / "model.png", folder / "reference_background.png", folder / "model_wearing_garment.png", "volc-seedream"))
                            self.assertFalse((folder / "garment.png").exists())
                        else:
                            self.assertEqual(composition.call_args.args[:3], (folder / "model.png", folder / "garment.png", folder / "reference_background.png"))
                        url = f"/files/{job_id}/reference_background.png"
                        self.assertEqual(state["background_reference"], url)
                        image = self.client.get(url)
                        self.assertEqual(image.status_code, 200)
                        self.assertEqual(image.mimetype, "image/png")
                        image.close()
                        workflow.job_states.clear()
                        workflow.restore_job_history()
                        self.assertEqual(workflow.job_states[job_id]["background_reference"], url)
                        self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
            self.assertEqual(extractor.call_count, 4)
            self.assertEqual(composition.call_count, 2)
            self.assertEqual(background_api.call_count, 2)
            motion.assert_not_called()

    def test_video_frame_validation_and_extraction_failure_never_submit_image_or_motion(self):
        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow.providers, "compose_background") as background_api, \
             patch.object(workflow.providers, "compose") as compose, \
             patch.object(workflow.providers, "motion") as motion, \
             patch.object(workflow, "extract_background_frame", side_effect=ValueError("无法提取首帧")) as extractor:
            for mode in ("ready_image", "compose"):
                with patch.object(workflow.providers, "missing_for", return_value=["请配置 ARK_API_KEY"]):
                    response = self.upload(mode, background_mode="video_frame")
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("ARK_API_KEY", response.json["error"])
                response = self.upload(mode, background_mode="invalid-mode")
                self.assertEqual(response.status_code, 400)
            response = self.upload("compose", background_mode="original")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(list(self.jobs.iterdir()), [])
            extractor.assert_not_called()
            response = self.upload("ready_image", background_mode="video_frame")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json["state"]["status"], "error")
            self.assertIn("无法提取首帧", response.json["state"]["error"])
            self.assertIsNone(response.json["state"]["background_reference"])
            extractor.assert_called_once()
            background_api.assert_not_called()
            compose.assert_not_called()
            motion.assert_not_called()

    def test_video_frame_history_only_links_existing_frame_for_frame_mode_and_resume_skips_images(self):
        def frame(clip, destination):
            Image.new("RGB", (512, 768), "blue").save(destination)

        def background(model, reference, destination, *args, **kwargs):
            Image.new("RGB", (512, 768), "white").save(destination)
            return {"model": "offline-background"}

        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow, "extract_background_frame", side_effect=frame) as extractor, \
             patch.object(workflow.providers, "compose_background", side_effect=background) as background_api, \
             patch.object(workflow.providers, "compose") as compose, \
             patch.object(workflow.providers, "motion") as motion:
            response = self.upload("ready_image", background_mode="video_frame")
            job_id = response.json["id"]
            folder = self.jobs / job_id
            state_file = folder / "job_state.json"
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            saved.pop("background_mode")
            state_file.write_text(json.dumps(saved), encoding="utf-8")
            workflow.job_states.clear()
            workflow.restore_job_history()
            self.assertEqual(workflow.job_states[job_id]["background_mode"], "video_frame")
            self.assertTrue(self.client.get(f"/api/jobs/{job_id}").json["background_reference"])
            workflow.set_state(job_id, status="running")
            (folder / "fashion_dance_source_task.json").write_text(json.dumps({"task_id": "existing-frame-task"}), encoding="utf-8")
            workflow.job_states.clear()
            workflow.restore_job_history()

            def resume(destination, option, **kwargs):
                destination.write_bytes(b"offline downloaded motion")
                return {"model": "offline-resumed-motion"}

            def export(source, destination, *args, **kwargs):
                destination.write_bytes(b"offline export")
                return {"source_width": 694, "source_height": 1230, "note": "offline export"}

            with patch.object(workflow.providers, "resume_motion", side_effect=resume) as recovery, \
                 patch.object(workflow, "video_dimensions", return_value=(694, 1230)), \
                 patch.object(workflow, "export_1080p", side_effect=export):
                self.assertEqual(self.client.post(f"/api/jobs/{job_id}/resume").status_code, 202)
                recovery.assert_called_once()
            self.assertEqual(workflow.job_states[job_id]["status"], "done")
            extractor.assert_called_once()
            background_api.assert_called_once()
            compose.assert_not_called()
            motion.assert_not_called()
        url = f"/files/{job_id}/reference_background.png"
        workflow.set_state(job_id, background_mode="original")
        self.assertIsNone(self.client.get(f"/api/jobs/{job_id}").json["background_reference"])
        self.assertEqual(self.client.get(url).status_code, 404)
        workflow.set_state(job_id, background_mode="video_frame")
        (folder / "reference_background.png").unlink()
        self.assertIsNone(self.client.get("/api/jobs").json["jobs"][0]["background_reference"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_persisted_output_copyright_error_is_localized_without_private_writes(self):
        job_id = "e" * 32
        folder = self.jobs / job_id
        folder.mkdir()
        raw = ("Seedream背景替换失败：The request failed because the output image may be related to copyright restrictions. "
               "Request id: copyright-request-123")
        state = {"id": job_id, "mode": "ready_image", "background_mode": "replace", "status": "error",
                 "stage": "失败", "error": raw, "image": None}
        state_file = folder / "job_state.json"
        private_file = folder / "model_wearing_garment_image_request.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        private_file.write_text(json.dumps({"status": "rejected", "error": raw}), encoding="utf-8")
        originals = state_file.read_bytes(), private_file.read_bytes()
        workflow.restore_job_history()
        with patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow.providers, "compose_background") as background_api, \
             patch.object(workflow.providers, "motion") as motion:
            responses = [self.client.get(f"/api/jobs/{job_id}").json,
                         self.client.get("/api/jobs").json["jobs"][0],
                         self.client.post(f"/api/jobs/{job_id}/confirm").json["state"]]
            for public in responses:
                self.assertEqual(public["status"], "error")
                self.assertIn("有使用授权的人物或背景素材", public["error"])
                self.assertIn("保留原图背景", public["error"])
                self.assertIn("不会自动重试", public["error"])
                self.assertIn("本次视频生成尚未提交", public["error"])
                self.assertIn("copyright-request-123", public["error"])
            thread.assert_not_called()
            background_api.assert_not_called()
            motion.assert_not_called()
        self.assertEqual(workflow.job_states[job_id]["error"], raw)
        self.assertEqual((state_file.read_bytes(), private_file.read_bytes()), originals)
        (folder / "fashion_dance_source_submission.json").write_text("{}", encoding="utf-8")
        self.assertNotIn("视频生成尚未提交", workflow._public_job(workflow.job_states[job_id])["error"])

    def test_copyright_display_only_matches_seedream_output_restrictions(self):
        exact = "The request failed because the output image may be related to copyright restrictions."
        for raw in ("Kling生成失败：" + exact, "Seedream输入失败：The input image may be related to copyright restrictions.",
                    "Seedream图片合成失败（HTTP 429）：额度限制", "Seedream OutputImageSensitiveContentDetected.PolicyViolation: unsafe content"):
            state = {"status": "error", "error": raw}
            public = workflow._public_job(state)
            self.assertEqual(public["error"], raw)
            self.assertIsNot(public, state)
        raw = "Seedream OutputImageSensitiveContentDetected.PolicyViolation: copyright restriction"
        self.assertIn("版权限制", workflow._public_job({"status": "error", "error": raw})["error"])

    def test_new_background_error_post_returns_chinese_without_retrying(self):
        raw = ("Seedream背景替换失败：The request failed because the output image may be related to copyright restrictions. "
               "Request id: new-output-request")
        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow.providers, "compose_background", side_effect=RuntimeError(raw)) as background_api, \
             patch.object(workflow.providers, "motion") as motion:
            response = self.upload("ready_image", background_mode="replace", background=self.image_file("background.png"))
            self.assertEqual(response.status_code, 202)
            public = response.json["state"]
            self.assertIn("版权限制", public["error"])
            self.assertIn("new-output-request", public["error"])
            self.assertEqual(public["status"], "error")
            self.assertEqual(workflow.job_states[public["id"]]["error"], raw)
            saved = json.loads((self.jobs / public["id"] / "job_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["error"], raw)
            background_api.assert_called_once()
            motion.assert_not_called()

    def test_garment_modes_preserve_reference_order_and_only_forward_optional_separate_pants(self):
        cases = [
            ("default_legacy", {}, "outfit", False),
            ("outfit_ignores_stale_pants", {"garment_mode": "outfit", "pants": self.image_file("stale.png")}, "outfit", False),
            ("top_keeps_model_pants", {"garment_mode": "separate"}, "separate", False),
            ("top_with_pants", {"garment_mode": "separate", "pants": self.image_file("pants.png")}, "separate", True),
        ]
        with patch.object(workflow.providers, "motion") as motion:
            for name, fields, expected_mode, has_pants in cases:
                with self.subTest(name=name):
                    job_id, composition = self.prepare_preview(prompt="保留服装细节", **fields)
                    folder = self.jobs / job_id
                    composition.assert_called_once()
                    self.assertEqual(composition.call_args.args, (
                        folder / "model.png", folder / "garment.png", folder / "background.png",
                        folder / "model_wearing_garment.png", "volc-seedream",
                    ))
                    self.assertEqual(composition.call_args.kwargs, {
                        "prompt": "保留服装细节", "garment_mode": expected_mode,
                        "pants": folder / "pants.png" if has_pants else None,
                    })
                    config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
                    self.assertEqual(config["garment_mode"], expected_mode)
                    self.assertEqual(config["pants_file"], "pants.png" if has_pants else None)
                    self.assertEqual((folder / "pants.png").exists(), has_pants)
                    self.assertEqual(workflow.job_states[job_id]["garment_mode"], expected_mode)
                    self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
                    self.assertIsNone(workflow.job_states[job_id]["video"])
            job_id, composition = self.prepare_preview(
                "ready_image", garment_mode="ignored-old-value", pants=(io.BytesIO(b"not an image"), "stale-pants.png"),
            )
            composition.assert_not_called()
            self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
            self.assertFalse((self.jobs / job_id / "pants.png").exists())
        motion.assert_not_called()

    def test_invalid_garment_mode_never_starts_paid_work(self):
        with patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow.providers, "compose", create=True) as compose, \
             patch.object(workflow.providers, "motion") as motion:
            response = self.upload(garment_mode="unknown-mode")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(list(self.jobs.iterdir()), [])
            thread.assert_not_called()
            compose.assert_not_called()
            motion.assert_not_called()

    def test_confirmation_submits_once_and_illegal_states_never_resubmit(self):
        job_id, _ = self.prepare_preview("ready_image")

        def motion(image, clip, destination, option, **kwargs):
            destination.write_bytes(b"offline generated source")
            return {"model": "offline-motion"}

        def export(source, destination, *args, **kwargs):
            destination.write_bytes(b"offline export")
            return {"source_width": 694, "source_height": 1230, "note": "offline export",
                    "output_duration_seconds": kwargs["target_duration"]}

        with patch.object(workflow.threading, "Thread") as queued, \
             patch.object(workflow.providers, "motion", side_effect=motion) as submitted, \
             patch.object(workflow, "video_dimensions", return_value=(694, 1230)), \
             patch.object(workflow, "export_1080p", side_effect=export) as exported:
            accepted = self.client.post(f"/api/jobs/{job_id}/confirm")
            self.assertEqual(accepted.status_code, 202, accepted.json)
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 409)
            self.assertEqual(self.client.post("/api/jobs/does-not-exist/confirm").status_code, 404)
            queued.assert_called_once()
            submitted.assert_not_called()
            workflow.run_motion_job(job_id)
            submitted.assert_called_once()
            self.assertEqual(exported.call_args.kwargs["source_start_seconds"], 1)
            self.assertEqual(exported.call_args.kwargs["target_duration"], 5)
            self.assertEqual(exported.call_args.kwargs["output_fps"], 25)
        self.assertEqual(workflow.job_states[job_id]["status"], "done")

        another_id, _ = self.prepare_preview("ready_image")
        with patch.object(workflow.providers, "missing_for", return_value=["请配置动作API"]), \
             patch.object(workflow.threading, "Thread") as thread:
            self.assertEqual(self.client.post(f"/api/jobs/{another_id}/confirm").status_code, 400)
            self.assertEqual(workflow.job_states[another_id]["status"], "awaiting_confirmation")
            thread.assert_not_called()
        workflow.set_state(another_id, status="running")
        with patch.object(workflow.threading, "Thread") as thread:
            self.assertEqual(self.client.post(f"/api/jobs/{another_id}/confirm").status_code, 409)
            thread.assert_not_called()

    def test_fal_preview_locks_selected_model_and_confirmation_validates_before_submit(self):
        option = "fal-kling-v3-pro"
        job_id, composition = self.prepare_preview("ready_image", motion_model=option)
        composition.assert_not_called()
        state = self.client.get(f"/api/jobs/{job_id}").json
        self.assertEqual(state["selection"]["motion"]["id"], option)
        self.assertEqual(state["status"], "awaiting_confirmation")
        validator = Mock(side_effect=ValueError("参考视频尺寸低于340像素"))
        with patch.dict(sys.modules, {"providers_fal": Mock(validate_motion_inputs=validator)}), \
             patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow.providers, "motion") as motion:
            rejected = self.client.post(f"/api/jobs/{job_id}/confirm")
            self.assertEqual(rejected.status_code, 400)
            self.assertIn("340", rejected.json["error"])
            self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
            thread.assert_not_called()
            motion.assert_not_called()
            validator.side_effect = None
            accepted = self.client.post(f"/api/jobs/{job_id}/confirm")
            self.assertEqual(accepted.status_code, 202, accepted.json)
            self.assertEqual(accepted.json["state"]["selection"]["motion"]["id"], option)
            thread.assert_called_once()
            motion.assert_not_called()

    def test_fal_short_clips_and_unknown_submission_markers_cannot_generate(self):
        with patch.object(workflow.threading, "Thread") as thread:
            short = self.upload("ready_image", motion_model="fal-kling-v3-pro", seconds="2")
            self.assertEqual(short.status_code, 400)
            self.assertIn("3–30", short.json["error"])
            short_full = self.upload("ready_image", motion_model="fal-kling-v3-pro", duration_mode="full", start="3")
            self.assertEqual(short_full.status_code, 400)
            self.assertEqual(list(self.jobs.iterdir()), [])
            thread.assert_not_called()
        job_id, _ = self.prepare_preview("ready_image", motion_model="fal-kling-v3-pro")
        (self.jobs / job_id / "fashion_dance_source_submission.json").write_text("{}", encoding="utf-8")
        with patch.object(workflow.threading, "Thread") as thread, patch.object(workflow.providers, "motion") as motion:
            response = self.client.post(f"/api/jobs/{job_id}/confirm")
            self.assertEqual(response.status_code, 409)
            self.assertIn("阻止重复提交", response.json["error"])
            thread.assert_not_called()
            motion.assert_not_called()
    def test_settings_never_return_secrets_and_blank_secret_keeps_value(self):
        secret = "private-setting-test-only"
        with patch.dict(os.environ, {"ARK_API_KEY": "", "VOLC_ACCESS_KEY_ID": "", "VOLC_SECRET_ACCESS_KEY": ""}):
            response = self.client.put("/api/settings", json={"ARK_API_KEY": secret, "TOS_BUCKET": "offline-bucket"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["configured"]["ARK_API_KEY"])
            self.assertEqual(response.json["values"]["TOS_BUCKET"], "offline-bucket")
            self.assertNotIn(secret, response.get_data(as_text=True))
            self.assertNotIn("ARK_API_KEY", response.json["values"])
            response = self.client.put("/api/settings", json={"ARK_API_KEY": ""})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(os.environ["ARK_API_KEY"], secret)
            self.assertIn(secret, app_settings.ENV_PATH.read_text(encoding="utf-8"))
            self.assertNotIn(secret, self.client.get("/api/settings").get_data(as_text=True))
            with patch.object(app_settings, "save_settings") as save:
                rejected = self.client.put("/api/settings", json={"ARK_API_KEY": "attacker"},
                                           headers={"Origin": "https://other.example"})
                self.assertEqual(rejected.status_code, 403)
                save.assert_not_called()
            self.assertEqual(self.client.put("/api/settings", json={"UNSAFE_SETTING": "x"}).status_code, 400)

    def test_history_retains_preview_and_interrupts_without_resubmission(self):
        job_id, _ = self.prepare_preview("ready_image")
        workflow.job_states.clear()
        with patch.object(workflow.threading, "Thread") as thread:
            workflow.restore_job_history()
            self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
            workflow.set_state(job_id, status="running")
            (self.jobs / job_id / "fashion_dance_source_task.json").write_text(
                json.dumps({"task_id": "existing-task-123"}), encoding="utf-8")
            workflow.job_states.clear()
            workflow.restore_job_history()
            self.assertEqual(workflow.job_states[job_id]["status"], "running")
            self.assertTrue(workflow.job_states[job_id]["can_resume"])
            self.assertEqual(workflow.job_states[job_id]["recovery_status"], "pending")
            self.assertTrue(workflow.job_states[job_id]["safe_to_close"])
            self.assertEqual(workflow.job_states[job_id]["task_id"], "existing-task-123")
            self.assertTrue(workflow.job_states[job_id]["task_recorded"])
            workflow.set_state(job_id, status="running")
            (self.jobs / job_id / "fashion_dance_source_task.json").write_text("broken checkpoint", encoding="utf-8")
            workflow.job_states.clear()
            workflow.restore_job_history()
            self.assertEqual(workflow.job_states[job_id]["status"], "interrupted")
            self.assertFalse(workflow.job_states[job_id]["can_resume"])
            self.assertTrue(workflow.job_states[job_id]["task_recorded"])
            self.assertIn("勿重复提交", workflow.job_states[job_id]["resume_message"])
            thread.assert_not_called()

    def test_file_routes_serve_preview_but_never_private_job_config_or_task_ids(self):
        job_id, _ = self.prepare_preview("ready_image")
        preview = self.client.get(f"/files/{job_id}/model_wearing_garment.png")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "image/png")
        preview.close()
        for filename in ("job_config.json", "job_state.json", "fashion_dance_source_task.json", ".env",
                         "fashion_dance_source_submission.json", "fashion_dance_source_result.json",
                         "input.mp4", "dance_clip.mp4", "dance_clip_jimeng.mp4"):
            self.assertEqual(self.client.get(f"/files/{job_id}/{filename}").status_code, 404)
        self.assertEqual(self.client.get("/files/missing/model_wearing_garment.png").status_code, 404)

    def test_reference_clip_is_published_before_confirmation_supports_range_and_expires_when_missing(self):
        job_id, _ = self.prepare_preview("ready_image")
        url = f"/files/{job_id}/reference_clip.mp4"
        state = self.client.get(f"/api/jobs/{job_id}").json
        self.assertEqual(state["status"], "awaiting_confirmation")
        self.assertEqual(state["reference_video"], url)
        self.assertEqual(state["referenceVideoURL"], url)
        saved = json.loads((self.jobs / job_id / "job_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["reference_video"], url)
        expected = (self.jobs / job_id / "dance_clip.mp4").read_bytes()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "video/mp4")
        self.assertEqual(response.data, expected)
        response.close()
        response = self.client.get(url, headers={"Range": "bytes=0-3"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, expected[:4])
        self.assertEqual(response.headers["Content-Range"], f"bytes 0-3/{len(expected)}")
        response.close()
        (self.jobs / job_id / "dance_clip.mp4").unlink()
        self.assertIsNone(self.client.get(f"/api/jobs/{job_id}").json["reference_video"])
        self.assertIsNone(self.client.get("/api/jobs").json["jobs"][0]["referenceVideoURL"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_history_recovers_only_existing_safe_reference_clips(self):
        cases = [
            ("a" * 32, None, "dance_clip.mp4", True),
            ("b" * 32, {"clip_file": "dance_clip_jimeng.mp4"}, "dance_clip_jimeng.mp4", True),
            ("c" * 32, {"clip_file": "dance_clip.mp4"}, None, False),
        ]
        for job_id, config, filename, expected in cases:
            folder = self.jobs / job_id
            folder.mkdir()
            (folder / "job_state.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
            (folder / "output_info.json").write_text(json.dumps({"reference_duration_seconds": 5}), encoding="utf-8")
            if config:
                (folder / "job_config.json").write_text(json.dumps(config), encoding="utf-8")
            if filename:
                (folder / filename).write_bytes(b"offline old reference")
        with patch.object(workflow.threading, "Thread") as thread:
            workflow.restore_job_history()
            thread.assert_not_called()
        for job_id, config, filename, expected in cases:
            state = self.client.get(f"/api/jobs/{job_id}").json
            self.assertEqual(state["seconds"], 5)
            self.assertEqual(state["reference_video"], f"/files/{job_id}/reference_clip.mp4" if expected else None)
            self.assertEqual(state["reference_video"], state["referenceVideoURL"])

    def test_reference_clip_rejects_arbitrary_config_paths_even_with_safe_fallback_present(self):
        job_id, _ = self.prepare_preview("ready_image")
        folder = self.jobs / job_id
        outside = self.folder / "private.mp4"
        outside.write_bytes(b"private local bytes")
        config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
        for malicious in ("../private.mp4", "..\\private.mp4", str(outside), "input.mp4", ".env", ["dance_clip.mp4"]):
            with self.subTest(clip_file=malicious):
                config["clip_file"] = malicious
                (folder / "job_config.json").write_text(json.dumps(config), encoding="utf-8")
                response = self.client.get(f"/files/{job_id}/reference_clip.mp4")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"private local bytes", response.data)
                self.assertIsNone(self.client.get(f"/api/jobs/{job_id}").json["reference_video"])
                workflow.job_states.clear()
                workflow.restore_job_history()
                self.assertIsNone(workflow.job_states[job_id]["referenceVideoURL"])

    def test_reference_clip_accepts_virtualized_paths_by_file_identity_and_still_rejects_escape(self):
        job_id, _ = self.prepare_preview("ready_image")
        folder = self.jobs / job_id
        clip = folder / "dance_clip.mp4"
        redirected_jobs = self.folder / "virtualized-local-cache" / "jobs"
        redirected_folder = redirected_jobs / job_id
        original_resolve, original_samefile = Path.resolve, Path.samefile
        identity = {"jobs": True, "clip": True}

        def virtualized_resolve(path, *args, **kwargs):
            if path == folder:
                return redirected_folder
            if path == clip:
                return redirected_folder / clip.name
            return original_resolve(path, *args, **kwargs)

        def virtualized_samefile(path, other):
            if path == redirected_jobs and other == self.jobs:
                return identity["jobs"]
            if path == redirected_folder and other == folder:
                return identity["clip"]
            return original_samefile(path, other)

        url = f"/files/{job_id}/reference_clip.mp4"
        with patch.object(Path, "resolve", virtualized_resolve), patch.object(Path, "samefile", virtualized_samefile):
            # Windows app virtualization can resolve a child under LocalCache while
            # the parent retains its logical AppData name, despite matching identity.
            self.assertNotEqual(folder.resolve().parent, self.jobs.resolve())
            self.assertEqual(self.client.get(f"/api/jobs/{job_id}").json["reference_video"], url)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, clip.read_bytes())
            response.close()
            for mismatch in ("jobs", "clip"):
                with self.subTest(mismatch=mismatch):
                    identity[mismatch] = False
                    self.assertEqual(self.client.get(url).status_code, 404)
                    self.assertIsNone(self.client.get(f"/api/jobs/{job_id}").json["reference_video"])
                    identity[mismatch] = True

    def test_prompt_limit_rejects_before_cloud_submission(self):
        with patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow.providers, "compose", create=True) as compose, \
             patch.object(workflow.providers, "motion") as motion:
            response = self.upload(prompt="x" * 3001)
            self.assertEqual(response.status_code, 400)
            self.assertIn("3000", response.json["error"])
            thread.assert_not_called()
            compose.assert_not_called()
            motion.assert_not_called()


class FirstFrameExtractionTests(unittest.TestCase):
    def test_real_selected_clip_first_frame_and_failed_extraction_are_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video, clip, destination = (folder / name for name in ("colors.mp4", "dance_clip.mp4", "reference_background.png"))
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "color=c=red:s=160x240:r=25:d=1", "-f", "lavfi",
                 "-i", "color=c=blue:s=160x240:r=25:d=1", "-filter_complex",
                 "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", str(video)],
                check=True, capture_output=True, **workflow.quiet_subprocess_kwargs(),
            )
            workflow.trim_video(video, clip, 1.2, 0.5)
            Image.new("RGB", (160, 240), "red").save(destination)
            workflow.extract_background_frame(clip, destination)
            with Image.open(destination) as image:
                red, green, blue = image.convert("RGB").getpixel((80, 120))
                self.assertTrue(blue > 200 and red < 30 and green < 30)
            original = destination.read_bytes()
            with self.assertRaisesRegex(ValueError, "无法读取所选舞蹈片段的首帧"):
                workflow.extract_background_frame(folder / "missing.mp4", destination)
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(list(folder.glob(".reference_background.*.png")), [])


class FrozenSettingsPathsTests(unittest.TestCase):
    def test_frozen_resources_and_private_settings_use_separate_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "bundle"
            local_data = Path(temporary) / "local-data"
            resource.mkdir()
            code = """
import os, sys
from pathlib import Path
from unittest.mock import patch
sys.frozen = True
sys._MEIPASS = sys.argv[1]
os.environ.pop('FASHION_DANCE_HOME', None)
os.environ['LOCALAPPDATA'] = sys.argv[2]
os.environ['USERPROFILE'] = sys.argv[2]
import app_settings
import app
expected = Path(sys.argv[2]).resolve() / 'FashionDanceStudio'
assert app.JOBS == expected / 'jobs'
assert app_settings.ENV_PATH == expected / '.env'
assert Path(app.app.template_folder) == Path(sys.argv[1]).resolve() / 'templates'
assert Path(app.app.static_folder) == Path(sys.argv[1]).resolve() / 'static'
with patch.object(app.providers, 'missing_for', return_value=[]):
    response = app.app.test_client().put('/api/settings', json={'ARK_API_KEY': 'offline-frozen-private-value'})
assert response.status_code == 200
assert response.json['configured']['ARK_API_KEY']
assert 'offline-frozen-private-value' not in response.get_data(as_text=True)
assert app_settings.ENV_PATH.is_file()
assert not (Path(sys.argv[1]) / '.env').exists()
print('frozen_paths_ok')
"""
            process = subprocess.run(
                [sys.executable, "-c", code, str(resource), str(local_data)],
                cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("frozen_paths_ok", process.stdout)


if __name__ == "__main__":
    unittest.main()

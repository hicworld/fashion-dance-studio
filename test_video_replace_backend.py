"""Role mapping, explicit billing boundary, and recovery using temporary media."""
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app_settings
with patch.object(app_settings, "load_settings"):
    import app as workflow
import providers_fal


class InlineThread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


class VideoReplaceBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs"
        self.jobs.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("JOBS", self.jobs), ("job_states", {}), ("_claimed_jobs", set()),
                            ("_recovery_jobs", set()), ("_worker_threads", {}), ("worker_stop_event", threading.Event())):
            self.stack.enter_context(patch.object(workflow, name, value))
        self.stack.enter_context(patch.object(workflow, "video_duration", return_value=13))
        self.stack.enter_context(patch.object(workflow, "video_metrics", return_value={"duration": 5, "fps": 30}))
        self.stack.enter_context(patch.object(workflow, "video_dimensions", return_value=(720, 960)))
        self.trim = self.stack.enter_context(patch.object(workflow, "trim_video", side_effect=lambda src, dst, *a, **k: dst.write_bytes(b"offline clip")))
        self.frame = self.stack.enter_context(patch.object(workflow, "extract_background_frame", side_effect=self.make_frame))
        self.validate = self.stack.enter_context(patch.object(providers_fal, "validate_edit_inputs", return_value={}))
        self.compose = self.stack.enter_context(patch.object(workflow.providers, "compose"))
        self.background = self.stack.enter_context(patch.object(workflow.providers, "compose_background"))
        self.motion = self.stack.enter_context(patch.object(workflow.providers, "motion"))
        self.edit = self.stack.enter_context(patch.object(workflow.providers, "edit_video"))
        self.resume = self.stack.enter_context(patch.object(workflow.providers, "resume_motion"))
        self.client = workflow.app.test_client()

    @staticmethod
    def make_frame(clip, destination):
        Image.new("RGB", (720, 960), "gray").save(destination)

    @staticmethod
    def image_file():
        buffer = io.BytesIO()
        Image.new("RGB", (512, 768), "white").save(buffer, format="PNG")
        buffer.seek(0)
        return buffer, "role.png"

    def upload(self, count=2, **extra):
        data = {"mode": "video_replace", "edit_model": "fal-kling-o3-pro-edit",
                "video": (io.BytesIO(b"offline source"), "original.mp4"), "seconds": "5"}
        for index in range(1, count + 1):
            data[f"role_{index}"] = self.image_file()
            data[f"role_description_{index}"] = f"开场第{index}个人物"
        data.update(extra)
        return self.client.post("/api/jobs", data=data, content_type="multipart/form-data")

    def preview(self, count=2, **extra):
        with patch.object(workflow.threading, "Thread", InlineThread):
            response = self.upload(count=count, **extra)
        self.assertEqual(response.status_code, 202, response.json)
        self.assertEqual(response.json["state"]["status"], "awaiting_confirmation", response.json)
        return response.json["id"]

    def test_one_to_three_roles_preview_locally_without_any_generation_or_credentials(self):
        with patch.object(workflow.providers, "missing_for", side_effect=AssertionError("no credential preflight before local preview")):
            for count in (1, 2, 3):
                job_id = self.preview(count=count, edit_prompt="保留原动作与背景")
                state = self.client.get(f"/api/jobs/{job_id}").json
                self.assertEqual(state["mode"], "video_replace")
                self.assertEqual(len(state["roles"]), count)
                self.assertEqual(state["selection"]["motion"]["id"], "fal-kling-o3-pro-edit")
                self.assertEqual(state["billing"]["image"]["status"], "not_used")
                self.assertEqual(state["billing"]["image"]["estimated_amount"], 0)
                self.assertIn("原片首帧", state["stage"])
                self.assertTrue(state["reference_video"].endswith("reference_clip.mp4"))
                self.assertFalse(list((self.jobs / job_id).glob("*_submission.json")))
        for cloud in (self.compose, self.background, self.motion, self.edit, self.resume):
            cloud.assert_not_called()
        self.assertEqual(self.trim.call_args.kwargs["crf"], 18)

    def test_invalid_mapping_duration_and_model_never_start_workers(self):
        cases = [
            {"role_description_1": ""}, {"role_description_2": "x" * 201},
            {"seconds": "2"}, {"seconds": "16"}, {"edit_model": "fal-kling-v3-pro"},
            {"edit_prompt": "x" * 1001},
            {"duration_mode": "full", "start": "11"},
            {"role_2": (io.BytesIO(), ""), "role_3": self.image_file(), "role_description_3": "third"},
            {"role_4": self.image_file()},
        ]
        with patch.object(workflow.threading, "Thread") as thread:
            for extra in cases:
                response = self.upload(**extra)
                self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.upload(count=0).status_code, 400)
            thread.assert_not_called()
        self.assertEqual(list(self.jobs.iterdir()), [])
        self.edit.assert_not_called()

    def test_full_thirteen_seconds_is_supported_but_over_fifteen_rejected(self):
        with patch.object(workflow, "video_metrics", return_value={"duration": 13, "fps": 30}):
            job_id = self.preview(duration_mode="full")
        config = json.loads((self.jobs / job_id / "job_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["reference_duration"], 13)
        self.assertEqual(config["seconds"], 13)
        with patch.object(workflow, "video_duration", return_value=18), patch.object(workflow.threading, "Thread") as thread:
            self.assertEqual(self.upload(duration_mode="full").status_code, 400)
            thread.assert_not_called()

    def test_preflight_failure_keeps_preview_and_confirmation_uses_edit_credentials(self):
        job_id = self.preview()
        with patch.object(workflow.providers, "missing_for", return_value=["请配置 FAL_KEY"]) as missing, \
             patch.object(workflow.threading, "Thread") as thread:
            response = self.client.post(f"/api/jobs/{job_id}/confirm")
            self.assertEqual(response.status_code, 400)
            missing.assert_called_once_with("edit", "fal-kling-o3-pro-edit")
            self.assertEqual(workflow.job_states[job_id]["status"], "awaiting_confirmation")
            thread.assert_not_called()
        with patch.object(workflow.providers, "missing_for", return_value=[]), patch.object(workflow.threading, "Thread") as thread:
            self.validate.side_effect = ValueError("原视频尺寸不足720像素")
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 400)
            thread.assert_not_called()
        self.edit.assert_not_called()

    def test_confirm_submits_only_edit_once_and_preserves_generated_dimensions(self):
        job_id = self.preview(edit_prompt="保持两人动作")
        def edit(references, clip, destination, option, *, role_descriptions, prompt, progress, stop_event):
            self.assertEqual([p.name for p in references], ["role_1.png", "role_2.png"])
            self.assertEqual(role_descriptions, ["开场第1个人物", "开场第2个人物"])
            self.assertEqual(prompt, "保持两人动作")
            destination.write_bytes(b"offline generated output")
            return {"provider": "fal", "model": workflow.providers.EDIT_ENDPOINTS[option]}
        def export(source, destination, *args, **kwargs):
            self.assertEqual(kwargs, {"preserve_dimensions": True})
            destination.write_bytes(b"offline packaged output")
            return {"source_width": 720, "source_height": 960, "output_width": 720, "output_height": 960,
                    "source_duration_seconds": 5, "output_duration_seconds": 5, "note": "沿用原片尺寸"}
        self.edit.side_effect = edit
        with patch.object(workflow.providers, "missing_for", return_value=[]), \
             patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow, "export_1080p", side_effect=export):
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 202)
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 409)
            thread.assert_called_once()
            workflow.run_motion_job(job_id)
        state = self.client.get(f"/api/jobs/{job_id}").json
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["output_dimensions"], "720×960")
        self.assertEqual(state["billing"]["motion"]["currency"], "USD")
        self.assertAlmostEqual(state["billing"]["estimated_amount"], .84)
        self.edit.assert_called_once()
        self.motion.assert_not_called()
        self.compose.assert_not_called()
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 409)

    def test_history_restores_same_edit_request_by_query_only(self):
        job_id = self.preview()
        folder = self.jobs / job_id
        task_id = "offline-edit-request"
        (folder / "fashion_dance_source_task.json").write_text(json.dumps({"provider": "fal", "task_id": task_id,
            "request_id": task_id, "option": "fal-kling-o3-pro-edit",
            "endpoint": workflow.providers.EDIT_ENDPOINTS["fal-kling-o3-pro-edit"]}), encoding="utf-8")
        workflow.set_state(job_id, status="running")
        workflow.job_states.clear()
        workflow.restore_job_history()
        state = self.client.get(f"/api/jobs/{job_id}").json
        self.assertTrue(state["can_resume"])
        self.assertEqual(len(state["roles"]), 2)
        self.resume.assert_not_called()
        def resume(destination, option, **kwargs):
            self.assertEqual(option, "fal-kling-o3-pro-edit")
            destination.write_bytes(b"offline recovered source")
            return {"provider": "fal", "model": workflow.providers.EDIT_ENDPOINTS[option]}
        def export(src, dst, *args, **kwargs):
            dst.write_bytes(b"offline result")
            return {"source_width": 720, "source_height": 960, "output_width": 720, "output_height": 960, "note": "offline"}
        self.resume.side_effect = resume
        with patch.object(workflow.threading, "Thread", InlineThread), patch.object(workflow, "export_1080p", side_effect=export):
            self.assertEqual(workflow.start_recovery_workers(), 1)
            self.assertEqual(workflow.start_recovery_workers(), 0)
        self.resume.assert_called_once()
        self.edit.assert_not_called()
        self.motion.assert_not_called()
        self.compose.assert_not_called()
        self.assertEqual(workflow.job_states[job_id]["status"], "done")

    def test_official_models_preview_and_confirm_keep_the_selected_provider_and_resolution(self):
        cases = (("minimax-h3-edit", "2K"), ("volc-seedance-2.5-edit", "720p"))
        for option, resolution in cases:
            with self.subTest(option=option), patch.object(workflow.providers, "validate_edit_inputs", return_value={}) as validate:
                job_id = self.preview(edit_model=option, video_resolution=resolution)
                state = self.client.get(f"/api/jobs/{job_id}").json
                self.assertEqual(state["selection"]["motion"]["id"], option)
                self.assertEqual(state["video_resolution"], resolution)
                self.assertEqual(validate.call_args.kwargs["resolution"], resolution)
                self.edit.assert_not_called()
                spec = workflow.providers.OFFICIAL_EDIT_MODELS[option]
                def edit(refs, clip, destination, selected, **kwargs):
                    self.assertEqual(selected, option)
                    self.assertEqual(kwargs["resolution"], resolution)
                    self.assertEqual(len(refs), 2)
                    destination.write_bytes(b"offline official result")
                    return {"provider": spec["provider"], "model": spec["model"], "resolution": resolution}
                def export(source, destination, *args, **kwargs):
                    self.assertEqual(kwargs, {"preserve_dimensions": True})
                    destination.write_bytes(b"offline export")
                    return {"source_width": 720, "source_height": 960, "output_width": 720, "output_height": 960,
                            "source_duration_seconds": 5, "output_duration_seconds": 5, "note": "actual dimensions"}
                self.edit.side_effect = edit
                with patch.object(workflow.providers, "edit_missing_for", return_value=[]), \
                     patch.object(workflow.threading, "Thread"), patch.object(workflow, "export_1080p", side_effect=export):
                    self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 202)
                    self.assertEqual(self.client.post(f"/api/jobs/{job_id}/confirm").status_code, 409)
                    workflow.run_motion_job(job_id)
                self.assertEqual(workflow.job_states[job_id]["status"], "done")
                self.edit.assert_called_once()
                self.edit.reset_mock()
        self.motion.assert_not_called()
        self.compose.assert_not_called()

    def test_official_recovery_requires_the_original_provider_model_and_resolution(self):
        for option, resolution in (("minimax-h3-edit", "768P"), ("volc-seedance-2.5-edit", "1080p")):
            with self.subTest(option=option), patch.object(workflow.providers, "validate_edit_inputs", return_value={}):
                job_id = self.preview(edit_model=option, video_resolution=resolution)
                folder = self.jobs / job_id
                spec = workflow.providers.OFFICIAL_EDIT_MODELS[option]
                record = {"provider": spec["provider"], "option": option, "model": spec["model"],
                          "endpoint": spec["endpoint"], "task_id": "offline-existing-task", "resolution": resolution, "duration": 5}
                task_file = folder / "fashion_dance_source_task.json"
                task_file.write_text(json.dumps(record), encoding="utf-8")
                self.assertEqual(workflow._saved_task_id(job_id), record["task_id"])
                for field, value in (("provider", "fal"), ("model", "wrong-model"), ("endpoint", "https://wrong.invalid"),
                                     ("resolution", "2K" if resolution != "2K" else "768P")):
                    task_file.write_text(json.dumps({**record, field: value}), encoding="utf-8")
                    self.assertIsNone(workflow._saved_task_id(job_id))
                task_file.write_text(json.dumps(record), encoding="utf-8")
                workflow.set_state(job_id, status="running")
                workflow.restore_job_history()
                self.assertTrue(workflow.job_states[job_id]["can_resume"])
                self.assertEqual(workflow._recovery_plan(job_id, workflow.job_states[job_id])["kind"], "query")
                workflow.set_state(job_id, status="interrupted")
        self.resume.assert_not_called()
        self.edit.assert_not_called()

    def test_official_duration_resolution_and_asset_fields_reject_invalid_requests(self):
        invalid = [
            {"edit_model": "minimax-h3-edit", "seconds": "3"},
            {"edit_model": "minimax-h3-edit", "seconds": "5.5"},
            {"edit_model": "minimax-h3-edit", "video_resolution": "1080p"},
            {"edit_model": "minimax-h3-edit", "video_asset_id": "asset-offline"},
            {"edit_model": "volc-seedance-2.5-edit", "seconds": "3"},
            {"edit_model": "volc-seedance-2.5-edit", "video_resolution": "2K"},
            {"edit_model": "volc-seedance-2.5-edit", "video_asset_id": "https://private.invalid/secret"},
        ]
        for extra in invalid:
            with self.subTest(extra=extra), patch.object(workflow.threading, "Thread") as thread:
                self.assertEqual(self.upload(**extra).status_code, 400)
                thread.assert_not_called()
        with patch.object(workflow.providers, "validate_edit_inputs", return_value={}):
            job_id = self.preview(edit_model="volc-seedance-2.5-edit", video_asset_id="asset-offline-video",
                                  role_asset_id_1="asset://asset-offline-one", role_asset_id_2="asset-offline-two")
        config = workflow._read_job_config(job_id)
        self.assertEqual(config["video_asset_id"], "asset://asset-offline-video")
        self.assertEqual([role["asset_id"] for role in config["roles"]],
                         ["asset://asset-offline-one", "asset://asset-offline-two"])
        self.edit.assert_not_called()

    def test_role_downloads_are_whitelisted_and_private_records_stay_hidden(self):
        job_id = self.preview(count=2)
        with self.client.get(f"/files/{job_id}/role_1.png") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/files/{job_id}/role_3.png").status_code, 404)
        for name in ("job_config.json", "job_state.json", "fashion_dance_source_task.json", "role_4.png"):
            self.assertEqual(self.client.get(f"/files/{job_id}/{name}").status_code, 404)
        before = (self.jobs / job_id / "job_state.json").read_bytes()
        self.client.get("/api/jobs")
        self.assertEqual((self.jobs / job_id / "job_state.json").read_bytes(), before)
        self.edit.assert_not_called()


class NativeEditExportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "local video tools required")
    def test_video_edit_export_keeps_source_dimensions_and_frame_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.mp4", Path(directory) / "output.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "color=c=blue:size=320x240:rate=24:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                check=True, capture_output=True)
            result = workflow.export_1080p(source, target, preserve_dimensions=True)
            self.assertEqual((result["output_width"], result["output_height"]), (320, 240))
            self.assertEqual(result["output_fps"], 24)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertFalse(result["resized"])


if __name__ == "__main__":
    unittest.main()

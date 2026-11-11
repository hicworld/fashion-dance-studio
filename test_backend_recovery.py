"""Query-only application recovery, with no cloud calls or real .env reads."""

import io
import json
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
import providers_jimeng


class InlineThread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class BackendRecoveryTests(unittest.TestCase):
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
        self.stack.enter_context(patch.object(workflow, "video_dimensions", return_value=(694, 1230)))
        self.stack.enter_context(patch.object(workflow, "video_metrics", side_effect=lambda path:
            {"duration": 6.08 if path.name == "fashion_dance_source.mp4" else 5, "fps": 25}))
        self.compose = self.stack.enter_context(patch.object(workflow.providers, "compose"))
        self.background_api = self.stack.enter_context(patch.object(workflow.providers, "compose_background", create=True))
        self.motion = self.stack.enter_context(patch.object(workflow.providers, "motion"))
        self.resume = self.stack.enter_context(patch.object(workflow.providers, "resume_motion", side_effect=self.fake_resume))
        self.export = self.stack.enter_context(patch.object(workflow, "export_1080p", side_effect=self.fake_export))
        self.client = workflow.app.test_client()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def job(self, job_id="a" * 32, *, task=True, source=False, status="running", info=True):
        folder = self.jobs / job_id
        folder.mkdir()
        (folder / "dance_clip.mp4").write_bytes(b"offline reference")
        (folder / "model_wearing_garment.png").write_bytes(b"offline preview")
        self.write(folder / "job_state.json", {"id": job_id, "status": status, "background_mode": "replace",
                   "selection": {"motion": {"id": "volc-dreamactor-2"}}, "seconds": 5})
        self.write(folder / "job_config.json", {"mode": "ready_image", "motion_model": "volc-dreamactor-2", "background_mode": "replace",
                   "clip_file": "dance_clip.mp4", "reference_duration": 5, "export_duration": 5,
                   "output_fps": 25, "start": 0, "seconds": 5})
        self.write(folder / "image_info.json", {"model": "uploaded"})
        if task:
            self.write(folder / "fashion_dance_source_task.json", {"task_id": "saved-" + job_id,
                       "req_key": "jimeng_dreamactor_m20_gen_video", "request_id": "safe-request"})
        if source:
            (folder / "fashion_dance_source.mp4").write_bytes(b"offline valid source")
            if info:
                self.write(folder / "motion_info.json", {"model": "saved-motion", "native_fps": 25})
        return folder

    def fake_resume(self, destination, option, *, progress, stop_event):
        self.assertEqual(option, "volc-dreamactor-2")
        self.assertIs(stop_event, workflow.worker_stop_event)
        progress({"status": "processing", "stage": "云端生成中", "message": "查询原任务"})
        state = workflow.job_states[destination.parent.name]
        self.assertTrue(state["safe_to_close"])
        self.assertNotIn("percent", state)
        destination.write_bytes(b"offline downloaded source")
        return {"model": "resumed-motion", "task_id": "saved-" + destination.parent.name}

    @staticmethod
    def fake_export(source, destination, *args, **kwargs):
        destination.write_bytes(b"offline final video")
        return {"source_width": 694, "source_height": 1230, "note": "offline export",
                "output_duration_seconds": kwargs["target_duration"]}

    def test_restore_and_get_are_local_then_startup_queries_each_task_once(self):
        folder = self.job()
        workflow.restore_job_history()
        state = self.client.get(f"/api/jobs/{folder.name}").json
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["recovery_status"], "pending")
        self.assertTrue(state["can_resume"])
        self.assertTrue(state["safe_to_close"])
        self.client.get("/api/jobs")
        self.resume.assert_not_called()
        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow.providers, "missing_for", side_effect=AssertionError("TOS preflight forbidden")):
            self.assertEqual(workflow.start_recovery_workers(), 1)
            self.assertEqual(workflow.start_recovery_workers(), 0)
        self.resume.assert_called_once()
        self.compose.assert_not_called()
        self.background_api.assert_not_called()
        self.motion.assert_not_called()
        self.assertEqual(workflow.job_states[folder.name]["status"], "done")
        self.assertEqual((folder / "fashion_dance.mp4").read_bytes(), b"offline final video")
        self.assertEqual(self.export.call_args.kwargs["source_start_seconds"], 1)
        self.assertEqual(self.export.call_args.kwargs["target_duration"], 5)
        self.assertTrue((folder / "motion_info.json").is_file())

    def test_local_source_finishes_without_query_or_credentials_and_done_never_repeats(self):
        folder = self.job(task=False, source=True)
        workflow.restore_job_history()
        self.assertTrue(workflow.job_states[folder.name]["safe_to_close"])
        with patch.object(workflow.threading, "Thread", InlineThread), \
             patch.object(workflow.providers, "missing_for", side_effect=AssertionError("no credentials needed")):
            response = self.client.post(f"/api/jobs/{folder.name}/resume")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 409)
            self.assertEqual(workflow.start_recovery_workers(), 0)
        self.resume.assert_not_called()
        self.compose.assert_not_called()
        self.background_api.assert_not_called()
        self.motion.assert_not_called()
        self.export.assert_called_once()
        self.assertEqual(workflow.job_states[folder.name]["recovery_status"], "complete")

    def test_missing_id_or_uncertain_submission_never_queries_or_resubmits(self):
        folder = self.job(task=False)
        self.write(folder / "fashion_dance_source_submission.json", {"status": "submitting"})
        workflow.restore_job_history()
        state = workflow.job_states[folder.name]
        self.assertEqual(state["status"], "interrupted")
        self.assertFalse(state["can_resume"])
        self.assertFalse(state["safe_to_close"])
        with patch.object(workflow.threading, "Thread") as thread:
            self.assertEqual(workflow.start_recovery_workers(), 0)
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 400)
            thread.assert_not_called()
        self.resume.assert_not_called()
        self.compose.assert_not_called()
        self.motion.assert_not_called()

    def test_image_stage_errors_without_motion_evidence_keep_original_cause(self):
        for job_id, message in (("c" * 32, "Seedream 请求失败（HTTP 429）：额度限制"),
                                ("d" * 32, "Seedream 请求失败（HTTP 400）：版权校验未通过")):
            folder = self.job(job_id, task=False, status="error")
            state = json.loads((folder / "job_state.json").read_text(encoding="utf-8"))
            state.update(error=message, stage="合成图失败")
            self.write(folder / "job_state.json", state)
        workflow.restore_job_history()
        for state in workflow.job_states.values():
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["stage"], "合成图失败")
            self.assertIn("Seedream 请求失败", state["error"])
            self.assertFalse(state["can_resume"])
        with patch.object(workflow.threading, "Thread") as thread:
            self.assertEqual(workflow.start_recovery_workers(), 0)
            thread.assert_not_called()
        self.resume.assert_not_called()
        self.motion.assert_not_called()
        self.compose.assert_not_called()

    def test_claims_prevent_duplicate_resume_but_history_does_not_block_new_upload(self):
        first = self.job("a" * 32)
        second = self.job("b" * 32)
        workflow.restore_job_history()
        buffer = io.BytesIO()
        Image.new("RGB", (512, 768), "white").save(buffer, format="PNG")
        buffer.seek(0)
        with patch.object(workflow.threading, "Thread") as thread, \
             patch.object(workflow, "video_duration", return_value=5):
            self.assertEqual(workflow.start_recovery_workers(), 2)
            self.assertEqual(thread.call_count, 2)
            self.assertEqual(workflow.start_recovery_workers(), 0)
            self.assertEqual(self.client.post(f"/api/jobs/{first.name}/resume").status_code, 409)
            self.assertEqual(self.client.post(f"/api/jobs/{second.name}/resume").status_code, 409)
            response = self.client.post("/api/jobs", data={"mode": "ready_image", "model": (buffer, "model.png"),
                "video": (io.BytesIO(b"fake input"), "dance.mp4")}, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 202, response.json)
            self.assertEqual(thread.call_count, 3)
        self.resume.assert_not_called()
        self.motion.assert_not_called()

    def test_transient_terminal_and_cancelled_recovery_have_distinct_safe_states(self):
        folder = self.job()
        workflow.restore_job_history()
        with patch.object(workflow.threading, "Thread", InlineThread):
            self.resume.side_effect = providers_jimeng.MotionTransientError("网络暂不可用")
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 202)
            state = workflow.job_states[folder.name]
            self.assertEqual(state["status"], "interrupted")
            self.assertTrue(state["can_resume"])
            self.assertEqual(state["recovery_status"], "retryable")
            self.resume.side_effect = providers_jimeng.MotionCancelledError("软件关闭")
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 202)
            self.assertEqual(state["recovery_status"], "pending")
            self.assertTrue(state["safe_to_close"])
            self.resume.side_effect = providers_jimeng.MotionTerminalError("任务已过期", task_status="expired")
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 202)
            self.assertEqual(state["recovery_status"], "failed")
            self.assertFalse(state["can_resume"])
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 409)
        self.motion.assert_not_called()
        self.compose.assert_not_called()
        self.export.assert_not_called()

    def test_stop_persists_pending_and_restart_recovers_same_id_without_new_submission(self):
        folder = self.job()
        workflow.restore_job_history()
        with patch.object(workflow.threading, "Thread"):
            self.assertEqual(workflow.start_recovery_workers(), 1)
        workflow.stop_recovery_workers()
        self.assertTrue(workflow.worker_stop_event.is_set())
        saved = json.loads((folder / "job_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["recovery_status"], "pending")
        self.assertTrue(saved["safe_to_close"])
        self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 503)
        workflow._claimed_jobs.clear()
        workflow._recovery_jobs.clear()
        workflow._worker_threads.clear()
        workflow.job_states.clear()
        workflow.restore_job_history()
        with patch.object(workflow.threading, "Thread", InlineThread):
            self.assertEqual(workflow.start_recovery_workers(), 1)
        self.resume.assert_called_once()
        self.motion.assert_not_called()
        self.compose.assert_not_called()

    def test_shutdown_during_export_keeps_original_and_only_resumes_local_export(self):
        folder = self.job(task=False, source=True)
        workflow.restore_job_history()
        final = folder / "fashion_dance.mp4"
        final.write_bytes(b"previous-intact-file")

        def export_then_stop(*args, **kwargs):
            result = self.fake_export(*args, **kwargs)
            workflow.worker_stop_event.set()
            return result

        with patch.object(workflow.threading, "Thread", InlineThread):
            self.export.side_effect = export_then_stop
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 202)
            self.assertEqual(final.read_bytes(), b"previous-intact-file")
            self.assertEqual(workflow.job_states[folder.name]["recovery_status"], "pending")
            self.assertTrue((folder / "motion_info.json").is_file())
            self.assertEqual(list(folder.glob("fashion_dance_export_*.mp4")), [])
            self.export.side_effect = self.fake_export
            self.assertEqual(workflow.start_recovery_workers(), 1)
        self.assertEqual(final.read_bytes(), b"offline final video")
        self.assertEqual(workflow.job_states[folder.name]["status"], "done")
        self.resume.assert_not_called()
        self.motion.assert_not_called()
        self.compose.assert_not_called()

    def test_stop_never_overwrites_final_state_in_worker_release_window(self):
        folder = self.job()
        workflow.restore_job_history()
        workflow._claimed_jobs.add(folder.name)
        for status in ("done", "error", "interrupted"):
            with self.subTest(status=status):
                workflow.job_states[folder.name].update(status=status, recovery_status="finished-state", safe_to_close=True)
                workflow.stop_recovery_workers()
                self.assertEqual(workflow.job_states[folder.name]["status"], status)
                self.assertEqual(workflow.job_states[folder.name]["recovery_status"], "finished-state")

    def test_fal_saved_request_restores_by_query_once_and_has_no_jimeng_trim(self):
        folder = self.job()
        option = "fal-kling-v3-pro"
        config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
        config["motion_model"] = option
        self.write(folder / "job_config.json", config)
        self.write(folder / "fashion_dance_source_task.json", {
            "provider": "fal", "task_id": "saved-fal-id", "request_id": "saved-fal-id",
            "endpoint": workflow.providers.KLING_ENDPOINTS[option], "option": option,
        })
        def resume_fal(destination, model, *, progress, stop_event):
            self.assertEqual(model, option)
            progress({"status": "processing", "message": "查询原可灵任务"})
            self.assertTrue(workflow.job_states[folder.name]["safe_to_close"])
            destination.write_bytes(b"offline downloaded fal source")
            return {"provider": "fal", "model": workflow.providers.KLING_ENDPOINTS[option]}
        self.resume.side_effect = resume_fal
        workflow.restore_job_history()
        state = self.client.get(f"/api/jobs/{folder.name}").json
        self.assertTrue(state["can_resume"])
        self.assertTrue(state["safe_to_close"])
        self.resume.assert_not_called()
        with patch.object(workflow.threading, "Thread", InlineThread):
            self.assertEqual(workflow.start_recovery_workers(), 1)
            self.assertEqual(workflow.start_recovery_workers(), 0)
        self.resume.assert_called_once()
        self.motion.assert_not_called()
        self.compose.assert_not_called()
        self.assertEqual(workflow.job_states[folder.name]["status"], "done")
        self.assertEqual(self.export.call_args.kwargs["source_start_seconds"], 0)

    def test_h3_verified_rejection_is_terminal_across_restart_and_never_resubmits(self):
        import providers_minimax as h3
        folder = self.job(task=False)
        config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
        config.update(mode="video_replace", motion_model=h3.OPTION, video_resolution="768P")
        self.write(folder / "job_config.json", config)
        self.write(folder / "fashion_dance_source_submission.json", {
            "provider": "minimax", "model": h3.MODEL, "option": h3.OPTION, "endpoint": h3.CREATE_URL,
            "resolution": "768P", "duration": 5, "status": "rejected", "rejection_verified": True,
            "http_status": 402, "error_type": "insufficient_balance_error", "provider_code": "1008",
        })
        with patch.object(h3.requests, "post") as post, patch.object(h3.requests, "get") as get:
            workflow.restore_job_history()
            state = workflow.job_states[folder.name]
            self.assertEqual(state["task_status"], "rejected")
            self.assertEqual(state["status"], "error")
            self.assertTrue(state["safe_to_close"])
            self.assertFalse(state["can_resume"])
            self.assertIn("未创建视频任务", state["stage"])
            self.assertIn("按量", state["error"])
            workflow._motion_worker_error(folder.name, providers_jimeng.MotionTerminalError("余额不足", task_status="rejected"))
            self.assertIn("未创建视频任务", state["stage"])
            self.assertEqual(self.client.post(f"/api/jobs/{folder.name}/resume").status_code, 409)
            with patch.object(workflow.threading, "Thread") as thread:
                self.assertEqual(workflow.start_recovery_workers(), 0)
                thread.assert_not_called()
            post.assert_not_called()
            get.assert_not_called()

    def test_h3_legacy_402_keeps_uncertain_marker_and_shows_actionable_message(self):
        folder = self.job(task=False)
        config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
        config.update(mode="video_replace", motion_model="minimax-h3-edit", video_resolution="768P")
        self.write(folder / "job_config.json", config)
        marker = folder / "fashion_dance_source_submission.json"
        self.write(marker, {"status": "submitting", "provider": "minimax"})
        original = marker.read_bytes()
        state = json.loads((folder / "job_state.json").read_text(encoding="utf-8"))
        state.update(status="interrupted", error="MiniMax提交未确认成功（HTTP 402）：请检查按量账户余额。标记已保留，勿重复提交。")
        self.write(folder / "job_state.json", state)
        workflow.restore_job_history()
        public = self.client.get(f"/api/jobs/{folder.name}").json
        self.assertEqual(public["status"], "interrupted")
        self.assertFalse(public["safe_to_close"])
        self.assertFalse(public["can_resume"])
        self.assertIn("没有保存完整拒绝响应", public["error"])
        self.assertIn("新建预览", public["error"])
        self.assertEqual(marker.read_bytes(), original)
        self.assertIn("提交未确认成功", workflow.job_states[folder.name]["error"])
        self.resume.assert_not_called()
        self.motion.assert_not_called()

    def test_seedance_verified_privacy_rejection_stays_terminal_without_network(self):
        import providers_seedance as sd
        for index, media in enumerate(("Image", "Video"), 1):
            folder = self.job(str(index) * 32, task=False)
            config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
            config.update(mode="video_replace", motion_model=sd.OPTION, video_resolution="1080p")
            self.write(folder / "job_config.json", config)
            code = f"Input{media}SensitiveContentDetected.PrivacyInformation"
            self.write(folder / "fashion_dance_source_submission.json", {
                "provider": sd.PROVIDER, "model": sd.MODEL, "option": sd.OPTION, "endpoint": sd.CREATE_URL,
                "resolution": "1080p", "status": "rejected", "rejection_verified": True,
                "http_status": 400, "error_type": "BadRequest", "provider_code": code, "error_code": code,
            })
        with patch.object(sd.requests, "post") as post, patch.object(sd.requests, "get") as get:
            workflow.restore_job_history()
            for job_id, state in workflow.job_states.items():
                self.assertEqual(state["task_status"], "rejected")
                self.assertEqual(state["status"], "error")
                self.assertTrue(state["safe_to_close"])
                self.assertFalse(state["can_resume"])
                self.assertIn("未创建视频任务", state["stage"])
                self.assertIn("可能包含真人", state["error"])
                self.assertEqual(self.client.post(f"/api/jobs/{job_id}/resume").status_code, 409)
            with patch.object(workflow.threading, "Thread") as thread:
                self.assertEqual(workflow.start_recovery_workers(), 0)
                thread.assert_not_called()
            post.assert_not_called()
            get.assert_not_called()

    def test_seedance_legacy_privacy_error_display_never_upgrades_submission_proof(self):
        folder = self.job(task=False)
        config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
        config.update(mode="video_replace", motion_model="volc-seedance-2.5-edit", video_resolution="1080p")
        self.write(folder / "job_config.json", config)
        marker = folder / "fashion_dance_source_submission.json"
        self.write(marker, {"status": "submitting", "provider": "volcengine-ark"})
        original = marker.read_bytes()
        state = json.loads((folder / "job_state.json").read_text(encoding="utf-8"))
        state.update(status="interrupted", error="Seedance提交未能可靠确认（HTTP 400）：InputImageSensitiveContentDetected.PrivacyInformation；The input image 'content[1]' 'content[2]' may contain real person.")
        self.write(folder / "job_state.json", state)
        workflow.restore_job_history()
        public = self.client.get(f"/api/jobs/{folder.name}").json
        self.assertFalse(public["safe_to_close"])
        self.assertFalse(public["can_resume"])
        self.assertIn("角色参考图1、2", public["error"])
        self.assertIn("其他AI", public["error"])
        self.assertIn("旧记录缺少完整拒绝证据", public["error"])
        self.assertEqual(marker.read_bytes(), original)
        self.assertIn("提交未能可靠确认", workflow.job_states[folder.name]["error"])
        self.resume.assert_not_called()
        self.motion.assert_not_called()

    def test_fal_record_mismatch_or_missing_id_cannot_query_or_resubmit(self):
        for index, changes in enumerate((
            {"endpoint": "fal-ai/kling-video/v3/standard/motion-control"},
            {"request_id": "different-request"}, {"provider": "other"}, {"task_id": ""},
        )):
            folder = self.job(f"{index + 1:x}" * 32)
            config = json.loads((folder / "job_config.json").read_text(encoding="utf-8"))
            config["motion_model"] = "fal-kling-v3-pro"
            self.write(folder / "job_config.json", config)
            self.write(folder / "fashion_dance_source_task.json", {
                "provider": "fal", "task_id": "saved-fal-id", "request_id": "saved-fal-id",
                "endpoint": workflow.providers.KLING_ENDPOINTS["fal-kling-v3-pro"], **changes,
            })
        workflow.restore_job_history()
        with patch.object(workflow.threading, "Thread") as thread:
            self.assertEqual(workflow.start_recovery_workers(), 0)
            thread.assert_not_called()
        for job_id, state in workflow.job_states.items():
            self.assertFalse(state["can_resume"])
            self.assertFalse(state["safe_to_close"])
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/resume").status_code, 400)
        self.resume.assert_not_called()
        self.motion.assert_not_called()


if __name__ == "__main__":
    unittest.main()

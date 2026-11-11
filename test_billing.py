"""Offline price-estimate provenance, historical enrichment and privacy checks."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import billing
import app_settings

with patch.object(app_settings, "load_settings"):
    import app as workflow


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name)
        self.id = "b" * 32
        self.folder = self.jobs / self.id
        self.folder.mkdir()
        self.state = {"id": self.id, "status": "done", "mode": "ready_image", "background_mode": "original", "seconds": 24.9,
                      "reference_video": None, "referenceVideoURL": None}

    def save(self, name, value):
        (self.folder / name).write_text(json.dumps(value), encoding="utf-8")

    def uploaded_motion(self):
        self.save("job_config.json", {"mode": "ready_image", "background_mode": "original", "motion_model": "volc-dreamactor-2", "reference_duration": 24.9})
        self.save("image_info.json", {"provider": "uploaded", "model": "ready_image"})
        self.save("motion_info.json", {"provider": "volcengine", "model": "jimeng_dreamactor_m20_gen_video"})
        self.save("output_info.json", {"reference_duration_seconds": 24.9, "source_duration_seconds": 25.96, "output_duration_seconds": 24.9,
                                       "source_width": 694, "source_height": 1230, "source_fps": 25, "output_width": 1080, "output_height": 1920, "output_fps": 30})

    def seedream(self, *, size=(1440, 2560), usage=None, model="doubao-seedream-5-0-pro-260628"):
        self.state.update(mode="compose", status="awaiting_confirmation")
        self.save("job_config.json", {"mode": "compose", "motion_model": "volc-dreamactor-2"})
        self.save("image_info.json", {"provider": "volcengine", "model_id": model, "reference_count": 3})
        self.save("seedream_request.private.json", {"status": "generated", "model": model, "reference_roles": ["model", "garment", "background"],
                                                   "prompt": "private prompt", "image_url": "https://private.invalid/result?secret=hidden", "usage": usage})
        Image.new("RGB", size).save(self.folder / "model_wearing_garment.png")

    def fal_motion(self, *, model=billing.FAL_MOTION_MODEL, native=5.0):
        config_path = self.folder / "job_config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        config["motion_model"] = "fal-kling-v3-pro"
        self.save("job_config.json", config)
        self.save("motion_info.json", {"provider": "fal", "model": model})
        self.save("output_info.json", {"reference_duration_seconds": 3, "source_duration_seconds": native,
                                       "output_duration_seconds": 4.9, "source_width": 720, "source_height": 1280,
                                       "source_fps": 25, "output_width": 1080, "output_height": 1920, "output_fps": 30})

    def video_replace(self, *, model=billing.FAL_EDIT_MODEL, native=5.2):
        self.state.update(mode="video_replace", status="done", seconds=13)
        self.save("job_config.json", {"mode": "video_replace", "motion_model": "fal-kling-o3-pro-edit",
                                      "reference_duration": 13})
        self.save("image_info.json", {"provider": "local", "operation": "video_replace",
                                      "model": "source_frame_preview"})
        Image.new("RGB", (768, 1024)).save(self.folder / "model_wearing_garment.png")
        self.save("motion_info.json", {"provider": "fal", "model": model})
        self.save("output_info.json", {"reference_duration_seconds": 13, "source_duration_seconds": native,
                                       "output_duration_seconds": 5, "source_width": 768, "source_height": 1024,
                                       "source_fps": 24, "output_width": 768, "output_height": 1024, "output_fps": 24})

    def test_video_replace_preview_is_local_and_has_no_image_currency_or_generation(self):
        self.video_replace()
        result = billing.summarize_job(self.folder, self.state)
        image = result["image"]
        self.assertEqual(image["status"], "not_used")
        self.assertEqual(image["estimated_amount"], 0)
        self.assertIsNone(image["currency"])
        self.assertIsNone(image["model"])
        self.assertEqual(image["usage"], {})
        self.assertNotIn("generated_images", image)
        self.assertIn("预览不是", image["pricing_basis"])
        # A saved first frame alone must not become an inferred generated image.
        (self.folder / "image_info.json").unlink()
        self.assertEqual(billing.summarize_job(self.folder, self.state)["image"]["status"], "not_used")
        # Metadata is sufficient to identify an older local preview even when
        # its mode was not preserved in the job config/state.
        self.save("job_config.json", {"motion_model": "fal-kling-o3-pro-edit"})
        self.state["mode"] = "compose"
        self.save("image_info.json", {"provider": "local", "operation": "video_replace"})
        self.assertIsNone(billing.summarize_job(self.folder, self.state)["image"]["currency"])

    def test_o3_edit_alias_and_endpoint_use_actual_source_seconds_in_usd(self):
        for model in (billing.FAL_EDIT_MODEL, "fal-kling-o3-pro-edit"):
            with self.subTest(model=model):
                self.video_replace(model=model)
                result = billing.summarize_job(self.folder, self.state)
                motion = result["motion"]
                self.assertEqual(motion["model"], model)
                self.assertEqual(motion["currency"], "USD")
                self.assertEqual(motion["rate_per_second"], 0.168)
                self.assertEqual(motion["reference_duration_seconds"], 13)
                self.assertEqual(motion["source_duration_seconds"], 5.2)
                self.assertEqual(motion["output_duration_seconds"], 5)
                self.assertEqual(motion["estimated_amount"], 0.8736)
                self.assertEqual(motion["duration_basis"], "local_source_metadata")
                self.assertIn("O3 Pro Video Edit", motion["pricing_basis"])
                self.assertEqual(result["estimated_totals"], {"USD": 0.8736})
                self.assertEqual(result["known_estimated_totals"], {"USD": 0.8736})
                self.assertEqual(result["estimated_amount"], 0.8736)
                self.assertFalse(result["mixed_currencies"])
                self.assertIsNone(result["actual_amount"])
                self.assertFalse(result["actual_available"])

    def test_o3_edit_pending_or_failure_never_substitutes_reference_duration(self):
        self.video_replace(native=None)
        self.save("fashion_dance_source_task.json", {"request_id": "existing-edit-task"})
        for status in ("running", "error", "interrupted"):
            with self.subTest(status=status):
                self.state["status"] = status
                result = billing.summarize_job(self.folder, self.state)
                self.assertIsNone(result["motion"]["estimated_amount"])
                self.assertIsNone(result["motion"]["source_duration_seconds"])
                self.assertIsNone(result["estimated_amount"])
                self.assertEqual(result["estimated_totals"], {})
                self.assertEqual(result["known_estimated_totals"], {})
                self.assertEqual(result["currency"], "USD")
                self.assertFalse(result["estimate_complete"])

    def test_o3_edit_mapping_preview_is_unsubmitted_and_no_other_fal_rate_is_guessed(self):
        self.video_replace()
        self.state["status"] = "awaiting_confirmation"
        (self.folder / "motion_info.json").unlink()
        (self.folder / "output_info.json").unlink()
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["motion"]["status"], "not_submitted")
        self.assertEqual(result["motion"]["estimated_amount"], 0)
        self.assertIsNone(result["actual_amount"])
        self.video_replace(model="fal-ai/kling-video/o3/standard/video-to-video/edit")
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["motion"]["currency"], "USD")
        self.assertIsNone(result["motion"]["rate_per_second"])
        self.assertIsNone(result["motion"]["estimated_amount"])

    def test_uploaded_image_and_native_video_duration_are_not_a_settled_bill(self):
        self.uploaded_motion()
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["estimated_amount"], 0)
        self.assertEqual(result["image"]["status"], "not_used")
        self.assertEqual(result["motion"]["source_duration_seconds"], 25.96)
        self.assertEqual(result["motion"]["reference_duration_seconds"], 24.9)
        self.assertEqual(result["motion"]["output_duration_seconds"], 24.9)
        self.assertEqual(result["estimated_amount"], 10.384)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["estimated_totals"], {"CNY": 10.384})
        self.assertEqual(result["known_estimated_totals"], {"CNY": 10.384})
        self.assertFalse(result["mixed_currencies"])
        self.assertEqual(result["motion"]["source_fps"], 25)
        self.assertIsNone(result["actual_amount"])
        self.assertFalse(result["actual_available"])
        self.assertIn("非实际账单", result["display"])
        self.assertEqual(result["motion"]["usage"], {})

    def test_seedream_uses_downloaded_pixel_tier_and_reported_input_usage(self):
        self.seedream(usage={"input_images": 4, "generated_images": 1, "output_tokens": 14400, "total_tokens": 14400})
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["estimated_amount"], 0.66)
        self.assertEqual(result["image"]["reference_count"], 4)
        self.assertEqual(result["image"]["usage_source"], "api_response")
        self.assertEqual(result["motion"]["status"], "not_submitted")
        self.assertEqual(result["estimated_amount"], 0.66)
        self.seedream(size=(900, 1600), usage={"input_images": 2, "generated_images": 1})
        self.assertEqual(billing.summarize_job(self.folder, self.state)["image"]["estimated_amount"], 0.32)

    def test_historical_missing_usage_can_only_infer_local_image_counts(self):
        self.seedream()
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["estimated_amount"], 0.64)
        self.assertEqual(result["image"]["usage"], {})
        self.assertIsNone(result["image"]["usage_source"])
        self.assertIn("API未返回", result["image"]["input_count_basis"])
        self.assertIn("API未返回", result["image"]["generation_count_basis"])

    def test_usage_is_numeric_whitelist_and_never_exposes_payload_or_signed_url(self):
        self.seedream(usage={"input_images": 3, "generated_images": 1, "output_tokens": 20, "total_tokens": float("nan"),
                             "secret": "SECRET", "video_url": "https://private.invalid", "cost": 999,
                             "tool_usage": {"web_search": 2, "api_key": "SECRET"}})
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["usage"], {"input_images": 3, "generated_images": 1, "output_tokens": 20, "tool_usage": {"web_search": 2}})
        self.assertFalse(result["estimate_complete"])
        self.assertIsNone(result["estimated_amount"])
        self.assertEqual(result["image"]["unestimated_usage"], ["web_search"])
        rendered = json.dumps(result)
        for forbidden in ("SECRET", "private.invalid", "private prompt", "video_url", '"cost"'):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(billing.safe_usage({"generated_images": True, "input_images": -1, "output_tokens": 3.2, "total_tokens": float("inf")}), {})

    def test_pending_or_failed_motion_does_not_assume_reference_seconds_or_zero_cost(self):
        self.uploaded_motion()
        (self.folder / "output_info.json").unlink()
        self.save("fashion_dance_source_result.json", {"status": "failed", "time_elapsed": 24.9, "video_url": "https://private.invalid"})
        self.state["status"] = "error"
        result = billing.summarize_job(self.folder, self.state)
        self.assertIsNone(result["motion"]["estimated_amount"])
        self.assertIsNone(result["estimated_amount"])
        self.assertFalse(result["estimate_complete"])
        self.assertIsNone(result["motion"]["source_duration_seconds"])
        self.assertEqual(result["motion"]["usage"], {})

    def test_unknown_model_and_historical_multi_image_pixel_tiers_are_not_guessed(self):
        self.seedream(model="ep-custom-unknown", usage={"input_images": 3, "generated_images": 1})
        self.assertIsNone(billing.summarize_job(self.folder, self.state)["image"]["estimated_amount"])
        self.seedream(usage={"input_images": 3, "generated_images": 2})
        self.assertIsNone(billing.summarize_job(self.folder, self.state)["image"]["estimated_amount"])

    def test_historical_failure_or_deleted_motion_records_never_imply_free_generation(self):
        self.save("image_info.json", {"provider": "uploaded"})
        for state in ({**self.state, "status": "error"}, {**self.state, "status": "done", "video": "/files/old/fashion_dance.mp4"}):
            with self.subTest(status=state["status"]):
                result = billing.summarize_job(self.folder, state)
                self.assertIsNone(result["motion"]["estimated_amount"])
                self.assertIsNone(result["estimated_amount"])

    def test_fal_uses_native_seconds_and_ready_image_does_not_create_cny_zero(self):
        self.uploaded_motion()
        for model in (billing.FAL_MOTION_MODEL, "fal-kling-v3-pro"):
            with self.subTest(model=model):
                self.fal_motion(model=model)
                result = billing.summarize_job(self.folder, self.state)
                self.assertEqual(result["motion"]["model"], model)
                self.assertEqual(result["motion"]["currency"], "USD")
                self.assertIsNone(result["image"]["currency"])
                self.assertEqual(result["image"]["estimated_amount"], 0)
                self.assertEqual(result["motion"]["source_duration_seconds"], 5)
                self.assertEqual(result["motion"]["reference_duration_seconds"], 3)
                self.assertEqual(result["motion"]["output_duration_seconds"], 4.9)
                self.assertEqual(result["motion"]["estimated_amount"], 0.84)
                self.assertEqual(result["motion"]["rate_per_second"], 0.168)
                self.assertEqual(result["currency"], "USD")
                self.assertEqual(result["estimated_amount"], 0.84)
                self.assertEqual(result["estimated_totals"], {"USD": 0.84})
                self.assertEqual(result["known_estimated_totals"], {"USD": 0.84})
                self.assertNotIn("CNY", result["display"])
                self.assertFalse(result["mixed_currencies"])
                self.assertIsNone(result["actual_amount"])
                self.assertEqual(result["motion"]["usage"], {})

    def test_seedream_and_fal_keep_two_currencies_without_adding_or_converting(self):
        self.seedream(usage={"input_images": 3, "generated_images": 1})
        self.fal_motion()
        self.state["status"] = "done"
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["currency"], "CNY")
        self.assertEqual(result["motion"]["currency"], "USD")
        self.assertEqual(result["estimated_totals"], {"CNY": 0.64, "USD": 0.84})
        self.assertEqual(result["known_estimated_totals"], result["estimated_totals"])
        self.assertTrue(result["mixed_currencies"])
        self.assertTrue(result["estimate_complete"])
        for field in ("currency", "estimated_amount", "known_estimated_amount", "actual_amount"):
            self.assertIsNone(result[field])
        self.assertIn("CNY ¥0.64", result["display"])
        self.assertIn("USD $0.84", result["display"])
        self.assertIn("非实际账单", result["display"])

    def test_failed_or_pending_fal_without_native_duration_never_uses_reference_cost(self):
        self.uploaded_motion()
        self.fal_motion(native=None)
        self.save("fashion_dance_source_task.json", {"request_id": "saved-fal-request"})
        for status in ("running", "error"):
            with self.subTest(status=status):
                self.state["status"] = status
                result = billing.summarize_job(self.folder, self.state)
                self.assertIsNone(result["motion"]["estimated_amount"])
                self.assertIsNone(result["estimated_amount"])
                self.assertEqual(result["known_estimated_totals"], {})
                self.assertEqual(result["estimated_totals"], {})
                self.assertEqual(result["currency"], "USD")
                self.assertFalse(result["estimate_complete"])
                self.assertIsNone(result["actual_amount"])

    def test_legacy_other_fal_endpoints_do_not_inherit_v3_pro_price(self):
        self.uploaded_motion()
        for model in ("fal-ai/kling-video/v2.6/pro/motion-control", "fal-ai/kling-video/v3/standard/motion-control"):
            with self.subTest(model=model):
                self.fal_motion(model=model)
                result = billing.summarize_job(self.folder, self.state)
                self.assertEqual(result["motion"]["model"], model)
                self.assertEqual(result["motion"]["currency"], "USD")
                self.assertIsNone(result["motion"]["rate_per_second"])
                self.assertIsNone(result["motion"]["estimated_amount"])
                self.assertEqual(result["estimated_totals"], {})
        self.save("image_info.json", {"provider": "fal", "model": "fal-ai/fashn/tryon/v1.6"})
        self.state["mode"] = "compose"
        self.save("job_config.json", {"mode": "compose", "motion_model": "fal-kling-v3-standard"})
        Image.new("RGB", (900, 1600)).save(self.folder / "model_wearing_garment.png")
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["currency"], "USD")
        self.assertIsNone(result["image"]["estimated_amount"])
        self.assertEqual(result["currency"], "USD")
        self.assertNotIn("CNY", result["known_estimated_totals"])

    def test_unknown_image_cost_does_not_hide_a_known_usd_motion_estimate(self):
        self.seedream(model="ep-custom-unknown", usage={"input_images": 3, "generated_images": 1})
        self.fal_motion()
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["estimated_totals"], {"USD": 0.84})
        self.assertEqual(result["known_estimated_totals"], {"USD": 0.84})
        self.assertTrue(result["mixed_currencies"])
        self.assertFalse(result["estimate_complete"])
        self.assertIsNone(result["estimated_amount"])
        self.assertIn("已知部分", result["display"])

    def test_unestimated_search_usage_keeps_only_usd_total_complete(self):
        self.seedream(usage={"input_images": 3, "generated_images": 1, "tool_usage": {"web_search": 1}})
        self.fal_motion()
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["known_estimated_totals"], {"CNY": 0.64, "USD": 0.84})
        self.assertEqual(result["estimated_totals"], {"USD": 0.84})
        self.assertFalse(result["estimate_complete"])

    def test_model_endpoint_validation_never_exposes_a_url_or_dot_path(self):
        self.assertEqual(billing._model(billing.FAL_MOTION_MODEL), billing.FAL_MOTION_MODEL)
        self.assertEqual(billing._model("doubao-seedream-5-0-pro-260628"), "doubao-seedream-5-0-pro-260628")
        for value in ("https://private.invalid/model?secret=SECRET", "private.invalid/model", "//private.invalid/model",
                      "fal-ai/../private", "fal-ai//model", "fal-ai/model?secret=SECRET", "fal-ai/model#SECRET"):
            with self.subTest(value=value):
                self.assertIsNone(billing._model(value))
        self.uploaded_motion()
        self.fal_motion(model="https://private.invalid/model?secret=SECRET")
        self.save("job_config.json", {"motion_model": "fal-kling-v3-pro", "mode": "ready_image", "background_mode": "original"})
        rendered = json.dumps(billing.summarize_job(self.folder, self.state))
        self.assertNotIn("private.invalid", rendered)
        self.assertNotIn("SECRET", rendered)

    def test_rejected_image_only_is_zero_estimate_without_assuming_motion_is_free(self):
        self.state.update(mode="ready_image", background_mode="replace", status="error")
        self.save("job_config.json", {"mode": "ready_image", "background_mode": "replace", "motion_model": "volc-dreamactor-2"})
        self.save("seedream_request.private.json", {"status": "rejected", "model": "doubao-seedream-5-0-pro-260628", "error": "copyright"})
        self.save("fashion_dance_source_task.json", {"task_id": "existing-id"})
        result = billing.summarize_job(self.folder, self.state)
        self.assertEqual(result["image"]["estimated_amount"], 0)
        self.assertIsNone(result["motion"]["estimated_amount"])
        self.assertIsNone(result["actual_amount"])

    def test_get_list_and_get_state_enrich_history_without_writes_or_cloud_calls(self):
        self.uploaded_motion()
        self.save("job_state.json", self.state)
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        original = copy.deepcopy(self.state)
        with patch.object(workflow, "JOBS", self.jobs), patch.object(workflow, "job_states", {self.id: self.state}), \
             patch.object(workflow.providers, "motion") as motion, patch.object(workflow.providers, "resume_motion") as resume, \
             patch.object(workflow.providers, "compose") as compose:
            client = workflow.app.test_client()
            listed = client.get("/api/jobs").json["jobs"][0]
            retrieved = client.get(f"/api/jobs/{self.id}").json
        self.assertEqual(listed["billing"]["estimated_amount"], 10.384)
        self.assertEqual(retrieved["billing"], listed["billing"])
        motion.assert_not_called()
        resume.assert_not_called()
        compose.assert_not_called()
        self.assertEqual(self.state, original)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.folder.iterdir()})

    def test_corrupt_or_deep_local_json_does_not_break_history_or_trigger_cloud_calls(self):
        self.state.update(mode="compose")
        with patch.object(workflow, "JOBS", self.jobs), patch.object(workflow, "job_states", {self.id: self.state}), \
             patch.object(workflow.providers, "motion") as motion, patch.object(workflow.providers, "resume_motion") as resume, \
             patch.object(workflow.providers, "compose") as compose:
            client = workflow.app.test_client()
            for content in ('{"broken":', '{"nested":' * 1500 + '0' + '}' * 1500):
                with self.subTest(kind="deep" if len(content) > 100 else "corrupt"):
                    for name in ("image_info.json", "job_config.json", "output_info.json", "seedream_request.private.json"):
                        (self.folder / name).write_text(content, encoding="utf-8")
                    before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
                    listed = client.get("/api/jobs")
                    retrieved = client.get(f"/api/jobs/{self.id}")
                    self.assertEqual(listed.status_code, 200)
                    self.assertEqual(retrieved.status_code, 200)
                    for state in (listed.json["jobs"][0], retrieved.json):
                        self.assertIsNone(state["billing"]["estimated_amount"])
                        self.assertFalse(state["billing"]["estimate_complete"])
                        self.assertEqual(state["billing"]["status"], "unknown")
                    self.assertEqual(before, {path.name: path.read_bytes() for path in self.folder.iterdir()})
            motion.assert_not_called()
            resume.assert_not_called()
            compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()

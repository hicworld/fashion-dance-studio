"""Official H3 and Seedance estimates: billable provenance and public privacy."""
import json
import tempfile
import unittest
from pathlib import Path

import billing


class OfficialVideoBillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = {"id": "a" * 32, "mode": "video_replace", "status": "done", "seconds": 5}

    def save(self, filename, value):
        (self.folder / filename).write_text(json.dumps(value), encoding="utf-8")

    def configure(self, model, resolution):
        self.save("job_config.json", {"mode": "video_replace", "motion_model": model,
                                      "video_resolution": resolution, "reference_duration": 5,
                                      "roles": [{"index": 1}, {"index": 2}]})
        self.save("image_info.json", {"provider": "local", "operation": "video_replace"})

    def h3(self, *, resolution="768P", usage=None, native=5):
        self.configure("minimax-h3-edit", resolution)
        self.save("motion_info.json", {"provider": "minimax", "model": "MiniMax-H3", "resolution": resolution,
                                      "usage": usage if usage is not None else
                                      {"input_seconds": 5, "output_seconds": 5, "input_image_count": 2, "total_seconds": 10}})
        if native is not None:
            self.save("output_info.json", {"source_duration_seconds": native, "reference_duration_seconds": 5,
                                           "output_duration_seconds": 5})

    def seedance(self, *, resolution="1080p", usage=None):
        self.configure("volc-seedance-2.5-edit", resolution)
        self.save("motion_info.json", {"provider": "volcengine-ark", "model": "doubao-seedance-2-5-260628",
                                      "requested_resolution": resolution, "usage": usage if usage is not None else
                                      {"completion_tokens": 100000, "total_tokens": 200000}})

    def summarize(self):
        return billing.summarize_job(self.folder, self.state)

    def assert_complete_estimate(self, value, amount):
        self.assertEqual(value["estimated_amount"], amount)
        self.assertEqual(value["estimated_totals"], {"CNY": amount})
        self.assertEqual(value["currency"], "CNY")
        self.assertTrue(value["estimate_complete"])
        self.assertIsNone(value["actual_amount"])
        self.assertFalse(value["actual_available"])
        self.assertEqual(value["image"]["status"], "not_used")
        self.assertEqual(value["image"]["estimated_amount"], 0)
        self.assertIn("非实际账单", value["display"])

    def test_h3_768p_five_input_five_output_estimates_five_yuan(self):
        self.h3()
        result = self.summarize()
        self.assert_complete_estimate(result, 5.0)
        self.assertEqual(result["motion"]["input_duration_seconds"], 5)
        self.assertEqual(result["motion"]["billable_output_seconds"], 5)
        self.assertEqual(result["motion"]["input_rate_per_second"], .5)
        self.assertEqual(result["motion"]["output_rate_per_second"], .5)

    def test_h3_2k_five_input_five_output_estimates_eight_yuan(self):
        self.h3(resolution="2K")
        self.assert_complete_estimate(self.summarize(), 8.0)

    def test_h3_billable_api_seconds_take_priority_over_trimmed_export(self):
        self.h3(usage={"input_seconds": 5, "output_seconds": 6, "input_image_count": 2, "total_seconds": 11}, native=4.9)
        self.assert_complete_estimate(self.summarize(), 5.5)

    def test_h3_can_estimate_from_local_native_measurement_and_recorded_roles(self):
        self.h3(usage={}, native=5.2)
        self.assert_complete_estimate(self.summarize(), 5.1)
        self.assertIsNone(self.summarize()["motion"]["usage_source"])

    def test_h3_more_than_five_images_is_not_silently_counted_free(self):
        self.h3(usage={"input_seconds": 5, "output_seconds": 5, "input_image_count": 6})
        result = self.summarize()
        self.assertIsNone(result["estimated_amount"])
        self.assertIsNone(result["motion"]["estimated_amount"])
        self.assertFalse(result["estimate_complete"])

    def test_h3_failed_or_pending_without_native_or_api_usage_is_not_free(self):
        self.h3(usage={}, native=None)
        self.save("fashion_dance_source_submission.json", {"provider": "minimax", "status": "submitting"})
        for status in ("error", "running", "interrupted"):
            with self.subTest(status=status):
                self.state["status"] = status
                result = self.summarize()
                self.assertIsNone(result["motion"]["estimated_amount"])
                self.assertIsNone(result["estimated_amount"])
                self.assertEqual(result["estimated_totals"], {})
                self.assertFalse(result["estimate_complete"])

    def test_seedance_1080p_completion_tokens_estimate_four_point_six_yuan(self):
        self.seedance()
        self.assert_complete_estimate(self.summarize(), 4.6)
        self.assertEqual(self.summarize()["motion"]["rate_per_million_tokens"], 46)

    def test_seedance_720p_completion_tokens_estimate_four_point_two_yuan(self):
        self.seedance(resolution="720p")
        self.assert_complete_estimate(self.summarize(), 4.2)

    def test_seedance_total_tokens_and_native_seconds_do_not_invent_completion_usage(self):
        self.seedance(usage={"total_tokens": 200000})
        self.save("output_info.json", {"source_duration_seconds": 5, "reference_duration_seconds": 5})
        result = self.summarize()
        self.assertIsNone(result["motion"]["estimated_amount"])
        self.assertEqual(result["motion"]["usage"], {"total_tokens": 200000})
        self.assertIsNone(result["estimated_amount"])
        self.assertFalse(result["estimate_complete"])

    def test_seedance_failed_without_usage_does_not_mean_free(self):
        self.seedance(usage={})
        self.state["status"] = "error"
        self.save("fashion_dance_source_result.json", {"status": "failed", "private_url": "https://private.invalid"})
        result = self.summarize()
        self.assertIsNone(result["estimated_amount"])
        self.assertIsNone(result["motion"]["estimated_amount"])
        self.assertFalse(result["estimate_complete"])

    def test_public_usage_is_numeric_allowlist_with_no_urls_or_credentials(self):
        cases = [(self.h3, {"input_seconds": 5, "output_seconds": 5, "input_image_count": 2,
                            "total_seconds": 10, "cost": "secret-key", "url": "https://private.invalid?secret=KEY",
                            "input_audio_seconds": 50, "total_tokens": "secret-token"}),
                 (self.seedance, {"completion_tokens": 100000, "total_tokens": 200000,
                                  "url": "https://private.invalid?secret=KEY", "api_key": "secret-key",
                                  "input_tokens": "secret-token"})]
        for configure, usage in cases:
            with self.subTest(model=configure.__name__):
                configure(usage=usage)
                result = self.summarize()
                rendered = json.dumps(result)
                for secret in ("private.invalid", "secret-key", "secret-token"):
                    self.assertNotIn(secret, rendered)
                self.assertTrue(all(isinstance(value, (int, float)) for value in result["motion"]["usage"].values()))

    def test_resolution_fields_reject_urls_and_non_hashable_corrupt_records(self):
        for configure in (self.h3, self.seedance):
            for invalid in ("https://private.invalid?secret=PRIVATE_KEY", ["secret-list"], {"secret": "secret-dict"}):
                with self.subTest(model=configure.__name__, invalid=invalid):
                    configure(resolution=invalid)
                    result = self.summarize()
                    rendered = json.dumps(result)
                    for secret in ("private.invalid", "PRIVATE_KEY", "secret-list", "secret-dict"):
                        self.assertNotIn(secret, rendered)
                    self.assertIsNone(result["motion"]["estimated_amount"])


if __name__ == "__main__":
    unittest.main()

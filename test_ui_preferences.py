"""Durable theme/model choices, without changing API secrets or cloud jobs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_settings
with patch.object(app_settings, "load_settings"):
    import app as workflow
import ui_preferences


class UIPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "profile" / "ui_preferences.json"
        self.patcher = patch.object(ui_preferences, "PREFERENCES_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = workflow.app.test_client()

    def test_defaults_are_jimeng_and_get_has_no_write(self):
        response = self.client.get("/api/preferences", base_url="http://127.0.0.1:12345")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, ui_preferences.DEFAULTS)
        self.assertFalse(self.path.exists())

    def test_choices_survive_new_port_and_partial_updates(self):
        response = self.client.put("/api/preferences", json={"theme": "dark", "motion_model": "fal-kling-v3-pro",
                                                              "creation_mode": "ready_image"},
                                   base_url="http://127.0.0.1:12345")
        self.assertEqual(response.status_code, 200)
        second_client = workflow.app.test_client()
        saved = second_client.get("/api/preferences", base_url="http://127.0.0.1:54321").json
        self.assertEqual(saved, response.json)
        updated = second_client.put("/api/preferences", json={"theme": "light"}).json
        self.assertEqual(updated, {**saved, "theme": "light"})
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_invalid_or_secret_fields_do_not_overwrite_profile(self):
        ui_preferences.save_preferences({"theme": "dark"})
        before = self.path.read_bytes()
        for payload in ({"theme": "system"}, {"theme": False}, {"motion_model": "fal-kling-o3-pro-edit"},
                        {"ARK_API_KEY": "offline-do-not-store"}, {"creation_mode": "unknown"}, ["dark"]):
            with self.subTest(payload=payload):
                response = self.client.put("/api/preferences", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("offline-do-not-store", self.path.read_text(encoding="utf-8"))

    def test_atomic_failure_keeps_prior_selection(self):
        ui_preferences.save_preferences({"theme": "dark"})
        before = self.path.read_bytes()
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation), patch.object(ui_preferences.os, operation, side_effect=OSError("offline write error")):
                response = self.client.put("/api/preferences", json={"theme": "light"})
                self.assertEqual(response.status_code, 500)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(ui_preferences.load_preferences()["theme"], "dark")
            self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_corrupt_profile_falls_back_and_repair_is_non_secret(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("invalid-json", encoding="utf-8")
        self.assertEqual(ui_preferences.load_preferences(), ui_preferences.DEFAULTS)
        ui_preferences.save_preferences({"theme": "dark"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), set(ui_preferences.DEFAULTS))
        self.assertEqual(saved["theme"], "dark")

    def test_cross_origin_write_is_rejected_and_dark_theme_is_server_rendered(self):
        response = self.client.put("/api/preferences", json={"theme": "dark"},
                                   headers={"Origin": "https://example.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.path.exists())
        ui_preferences.save_preferences({"theme": "dark"})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-theme="dark"', response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

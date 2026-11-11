"""Offline persistence checks using only temporary files and invented credentials."""

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

import app_settings


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "new-profile"
        self.env_path = self.root / ".env"
        self.environment = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(app_settings, "ENV_PATH", self.env_path))
        self.stack.enter_context(patch.object(app_settings, "DATA_ROOT", self.root))
        self.stack.enter_context(patch.object(os, "environ", self.environment))
        self.stack.enter_context(patch("providers.missing_for", return_value=[]))

    def write_env(self, content):
        self.root.mkdir(parents=True, exist_ok=True)
        self.env_path.write_text(content, encoding="utf-8")

    def test_saved_values_win_after_restart_and_unsaved_keys_fall_back(self):
        for inherited in ("", "offline-stale-value"):
            with self.subTest(inherited_is_blank=not inherited):
                self.environment.clear()
                self.environment.update(ARK_API_KEY=inherited, TOS_BUCKET="inherited-old-bucket",
                                        VOLC_ACCESS_KEY_ID="unsaved-environment-fallback",
                                        FAL_KEY="legacy-environment-value")
                self.write_env("ARK_API_KEY='offline-saved-key'\nTOS_BUCKET='saved-bucket'\n"
                               "FAL_KEY='legacy-file-value'\nLEGACY_ONLY='legacy-loads'\n"
                               "LEGACY_REFERENCE='${ARK_API_KEY}'\n")
                app_settings.load_settings()
                self.assertEqual(self.environment["ARK_API_KEY"], "offline-saved-key")
                self.assertEqual(self.environment["TOS_BUCKET"], "saved-bucket")
                self.assertEqual(self.environment["VOLC_ACCESS_KEY_ID"], "unsaved-environment-fallback")
                self.assertEqual(self.environment["FAL_KEY"], "legacy-file-value")
                self.assertEqual(self.environment["LEGACY_ONLY"], "legacy-loads")
                self.assertEqual(self.environment["LEGACY_REFERENCE"], "offline-saved-key")

    def test_save_new_profile_and_blank_secret_survives_restart(self):
        self.assertFalse(self.root.exists())
        result = app_settings.save_settings({"ARK_API_KEY": "offline-new-key", "TOS_BUCKET": "test-bucket"})
        self.assertTrue(self.env_path.is_file())
        self.assertEqual(result["data_directory"], str(self.root))
        self.assertTrue(result["configured"]["ARK_API_KEY"])
        self.assertNotIn("offline-new-key", str(result))
        app_settings.save_settings({"ARK_API_KEY": "  ", "TOS_REGION": "cn-test"})
        self.assertEqual(self.environment["ARK_API_KEY"], "offline-new-key")
        self.environment.clear()
        app_settings.load_settings()
        self.assertEqual(self.environment["ARK_API_KEY"], "offline-new-key")
        self.assertEqual(self.environment["TOS_BUCKET"], "test-bucket")
        self.assertEqual(self.environment["TOS_REGION"], "cn-test")
        self.assertFalse(list(self.root.glob(".env.*.tmp")))

    def test_fal_secret_persists_without_public_exposure_and_models_are_independent(self):
        secret = "offline-fal-id:offline-fal-secret"
        def missing(stage, option):
            return [] if option in {"fal-kling-v3-pro", "fal-kling-o3-pro-edit"} and self.environment.get("FAL_KEY") else ["offline missing"]
        with patch("providers.missing_for", side_effect=missing):
            result = app_settings.save_settings({"FAL_KEY": secret})
            self.assertTrue(result["configured"]["FAL_KEY"])
            self.assertTrue(result["motion_models"]["fal-kling-v3-pro"]["ready"])
            self.assertTrue(result["edit_models"]["fal-kling-o3-pro-edit"]["ready"])
            self.assertFalse(result["motion_models"]["volc-dreamactor-2"]["ready"])
            self.assertFalse(result["motion_ready"])
            self.assertNotIn(secret, str(result))
            self.assertNotIn("FAL_KEY", result["values"])
            app_settings.save_settings({"FAL_KEY": " "})
            self.environment["FAL_KEY"] = "stale-inherited-key"
            app_settings.load_settings()
            self.assertEqual(self.environment["FAL_KEY"], secret)

    def test_minimax_key_survives_restart_and_is_not_returned_to_browser(self):
        secret = "offline-minimax-key"
        result = app_settings.save_settings({"MINIMAX_API_KEY": secret})
        self.assertTrue(result["configured"]["MINIMAX_API_KEY"])
        self.assertNotIn(secret, str(result))
        self.assertNotIn("MINIMAX_API_KEY", result["values"])
        self.assertIn("minimax-h3-edit", result["edit_models"])
        self.assertIn("volc-seedance-2.5-edit", result["edit_models"])
        app_settings.save_settings({"MINIMAX_API_KEY": ""})
        self.environment["MINIMAX_API_KEY"] = "stale-inherited"
        app_settings.load_settings()
        self.assertEqual(self.environment["MINIMAX_API_KEY"], secret)

    def test_preserves_legacy_lines_and_round_trips_literal_characters(self):
        self.write_env("# retained comment\nFAL_KEY='offline-legacy-key'\n"
                       "ARK_API_KEY='old-one'\nARK_API_KEY='old-two'\nLEGACY_LAST=kept")
        literal = "offline\\path\\\\pair'quote${UNCHANGED}"
        self.environment["UNCHANGED"] = "must-not-expand"
        app_settings.save_settings({"ARK_API_KEY": literal, "TOS_ENDPOINT": "https://example.invalid"})
        parsed = dotenv_values(self.env_path, interpolate=False)
        self.assertEqual(parsed["ARK_API_KEY"], literal)
        self.assertEqual(parsed["FAL_KEY"], "offline-legacy-key")
        self.assertEqual(parsed["LEGACY_LAST"], "kept")
        self.assertTrue(self.env_path.read_text(encoding="utf-8").startswith("# retained comment\n"))
        self.assertEqual(self.env_path.read_text(encoding="utf-8").count("ARK_API_KEY="), 1)
        self.environment["ARK_API_KEY"] = "stale-after-restart"
        app_settings.load_settings()
        self.assertEqual(self.environment["ARK_API_KEY"], literal)

    def test_disk_failure_rolls_back_entire_batch_and_process_values(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.write_env("# unchanged bytes\nARK_API_KEY='offline-old-key'\nTOS_BUCKET='old-bucket'\n")
                before = self.env_path.read_bytes()
                self.environment.clear()
                self.environment.update(ARK_API_KEY="offline-old-key", TOS_BUCKET="old-bucket")
                old_environment = dict(self.environment)
                with patch.object(app_settings.os, operation, side_effect=OSError("offline write failure")):
                    with self.assertRaises(OSError):
                        app_settings.save_settings({"ARK_API_KEY": "offline-replacement-key", "TOS_BUCKET": "new-bucket"})
                self.assertEqual(self.env_path.read_bytes(), before)
                self.assertEqual(self.environment, old_environment)
                self.assertFalse(list(self.root.glob(".env.*.tmp")))

    def test_commit_uses_one_replacement_in_same_directory(self):
        self.write_env("LEGACY_ONLY=retained\n")
        real_replace = os.replace
        with patch.object(app_settings.os, "replace", wraps=real_replace) as replace:
            app_settings.save_settings({"ARK_API_KEY": "offline-batch-key", "TOS_BUCKET": "new-bucket",
                                        "TOS_REGION": "cn-test"})
        replace.assert_called_once()
        source, destination = replace.call_args.args
        self.assertEqual(Path(source).parent, self.root)
        self.assertEqual(Path(destination), self.env_path)
        parsed = dotenv_values(self.env_path, interpolate=False)
        self.assertEqual(parsed["ARK_API_KEY"], "offline-batch-key")
        self.assertEqual(parsed["TOS_BUCKET"], "new-bucket")
        self.assertEqual(parsed["TOS_REGION"], "cn-test")
        self.assertEqual(parsed["LEGACY_ONLY"], "retained")

    def test_sound_effect_key_is_persisted_and_never_returned(self):
        secret='offline-sound-effect-key'
        result=app_settings.save_settings({'ELEVENLABS_API_KEY':secret})
        self.assertTrue(result['configured']['ELEVENLABS_API_KEY'])
        self.assertNotIn(secret,str(result));self.assertNotIn('ELEVENLABS_API_KEY',result['values'])
        app_settings.save_settings({'ELEVENLABS_API_KEY':''})
        self.environment['ELEVENLABS_API_KEY']='stale-environment'
        app_settings.load_settings()
        self.assertEqual(self.environment['ELEVENLABS_API_KEY'],secret)


if __name__ == "__main__":
    unittest.main()

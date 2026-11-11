"""Offline migration regressions using isolated user profiles and dummy keys."""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import dotenv_values, set_key

import profile_migration as migration
import runtime_paths


class ProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "isolated-user"
        self.target = self.home / "FashionDanceStudio"
        self.real = self.home / "AppData" / "Local" / "FashionDanceStudio"
        self.virtual = (self.home / "AppData" / "Local" / "Packages" / "OpenAI.Codex_test" /
                        "LocalCache" / "Local" / "FashionDanceStudio")
        self.environ = {"USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.real.parent),
                        "APPDATA": str(self.home / "AppData" / "Roaming")}

    def config(self, folder, values, timestamp=None):
        folder.mkdir(parents=True, exist_ok=True)
        for key, value in values.items():
            set_key(str(folder / ".env"), key, value)
        if timestamp is not None:
            os.utime(folder / ".env", (timestamp, timestamp))

    def job(self, folder, job_id, label, timestamp):
        destination = folder / "jobs" / job_id
        destination.mkdir(parents=True, exist_ok=True)
        state = destination / "job_state.json"
        state.write_text(json.dumps({"id": job_id, "status": "done", "label": label}), encoding="utf-8")
        os.utime(state, (timestamp, timestamp))
        return destination

    def migrate(self):
        return migration.migrate_legacy_profiles(self.target, user_profile=self.home, environ=self.environ)

    def test_merges_both_profiles_without_overwriting_target_or_deleting_sources(self):
        self.config(self.target, {"ARK_API_KEY": "dummy-target-ark", "TOS_REGION": ""})
        self.config(self.real, {"ARK_API_KEY": "dummy-old-ark", "VOLC_ACCESS_KEY_ID": "dummy-real-ak",
                               "VOLC_SECRET_ACCESS_KEY": "dummy-real-sk", "TOS_BUCKET": "legacy-bucket"}, 100)
        self.config(self.virtual, {"ARK_API_KEY": "dummy-virtual-ark", "VOLC_ACCESS_KEY_ID": "dummy-virtual-ak",
                                  "VOLC_SECRET_ACCESS_KEY": "dummy-virtual-sk", "TOS_REGION": "cn-beijing",
                                  "TOS_ENDPOINT": "https://tos-cn-beijing.volces.com"}, 200)
        existing = self.job(self.target, "a" * 32, "target-kept", 300)
        self.job(self.real, "a" * 32, "old-target-copy", 100)
        old = self.job(self.real, "b" * 32, "older-source", 100)
        recent = self.job(self.virtual, "b" * 32, "latest-source", 200)
        (old / "garment.png").write_bytes(b"missing-legacy-image")
        (old / "fashion_dance.mp4").write_bytes(b"older-video")
        (recent / "fashion_dance.mp4").write_bytes(b"newer-video")
        self.job(self.real, "c" * 32, "real-only", 100)
        self.job(self.virtual, "d" * 32, "virtual-only", 200)
        original_target = (existing / "job_state.json").read_bytes()
        original_source_env = (self.virtual / ".env").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            report = self.migrate()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["source_profiles"], 2)
        self.assertEqual(len(list((self.target / "jobs").iterdir())), 4)
        self.assertEqual((existing / "job_state.json").read_bytes(), original_target)
        copied = self.target / "jobs" / ("b" * 32)
        self.assertEqual(json.loads((copied / "job_state.json").read_text())["label"], "latest-source")
        self.assertEqual((copied / "fashion_dance.mp4").read_bytes(), b"newer-video")
        self.assertEqual((copied / "garment.png").read_bytes(), b"missing-legacy-image")
        values = dotenv_values(self.target / ".env")
        self.assertEqual(values["ARK_API_KEY"], "dummy-target-ark")
        self.assertEqual(values["VOLC_ACCESS_KEY_ID"], "dummy-virtual-ak")
        self.assertEqual(values["VOLC_SECRET_ACCESS_KEY"], "dummy-virtual-sk")
        self.assertEqual(values["TOS_BUCKET"], "legacy-bucket")
        self.assertEqual(values["TOS_REGION"], "cn-beijing")
        self.assertEqual((self.virtual / ".env").read_bytes(), original_source_env)
        self.assertTrue(old.is_dir())
        audit = (self.target / "profile_migration.json").read_text(encoding="utf-8")
        self.assertTrue(json.loads(audit)["completed"])
        for value in ("dummy-target-ark", "dummy-virtual-ak", "dummy-virtual-sk", "dummy-old-ark"):
            self.assertNotIn(value, json.dumps(report))
            self.assertNotIn(value, audit)

    def test_success_marker_never_revives_cleared_settings_or_removed_tasks(self):
        self.config(self.real, {"ARK_API_KEY": "dummy-ark", "TOS_BUCKET": "old-bucket"})
        source = self.job(self.real, "a" * 32, "old-task", 100)
        self.migrate()
        set_key(str(self.target / ".env"), "TOS_BUCKET", "")
        copied = self.target / "jobs" / source.name
        (copied / "job_state.json").unlink()
        copied.rmdir()
        before_config = (self.target / ".env").read_bytes()
        before_report = (self.target / "profile_migration.json").read_bytes()
        with patch.object(migration, "discover_legacy_profiles", side_effect=AssertionError("must skip migration")):
            report = self.migrate()
        self.assertTrue(report["skipped"])
        self.assertEqual((self.target / ".env").read_bytes(), before_config)
        self.assertEqual((self.target / "profile_migration.json").read_bytes(), before_report)
        self.assertFalse(copied.exists())
        self.assertTrue(source.exists())

    def test_never_pairs_credentials_from_different_sources_or_imports_source_project(self):
        self.config(self.real, {"VOLC_ACCESS_KEY_ID": "dummy-only-ak", "TOS_ACCESS_KEY_ID": "dummy-tos-ak"})
        self.config(self.virtual, {"VOLC_SECRET_ACCESS_KEY": "dummy-only-sk", "TOS_SECRET_ACCESS_KEY": "dummy-tos-sk"})
        project = self.home / "Documents" / "debug-project"
        self.config(project, {"ARK_API_KEY": "dummy-project-key"})
        self.job(project, "e" * 32, "debug-never-import", 500)
        report = self.migrate()
        values = dotenv_values(self.target / ".env")
        self.assertFalse(values.get("ARK_API_KEY"))
        for pair in migration.CREDENTIAL_PAIRS[:2]:
            self.assertFalse(values.get(pair[0]))
            self.assertFalse(values.get(pair[1]))
            self.assertIn("/".join(pair), report["credential_pairs_needing_configuration"])
        self.assertFalse((self.target / "jobs" / ("e" * 32)).exists())

    def test_partial_target_pair_only_fills_from_matching_complete_source(self):
        self.config(self.target, {"VOLC_ACCESS_KEY_ID": "dummy-matching-ak"})
        self.config(self.real, {"VOLC_ACCESS_KEY_ID": "dummy-matching-ak", "VOLC_SECRET_ACCESS_KEY": "dummy-matching-sk"}, 100)
        self.config(self.virtual, {"VOLC_ACCESS_KEY_ID": "dummy-other-ak", "VOLC_SECRET_ACCESS_KEY": "dummy-other-sk"}, 200)
        self.migrate()
        self.assertEqual(dotenv_values(self.target / ".env")["VOLC_SECRET_ACCESS_KEY"], "dummy-matching-sk")

    def test_failed_configuration_batch_keeps_original_and_retries_on_next_start(self):
        self.config(self.target, {"ARK_API_KEY": "dummy-keep-ark"})
        self.config(self.real, {"TOS_BUCKET": "legacy-bucket", "TOS_REGION": "cn-beijing"})
        original = (self.target / ".env").read_bytes()
        actual_set_key = migration.set_key

        def fail_second(path, key, value, **kwargs):
            if key == "TOS_REGION":
                raise OSError("dummy-private-value must never appear in report")
            return actual_set_key(path, key, value, **kwargs)

        with patch.object(migration, "set_key", side_effect=fail_second):
            report = self.migrate()
        self.assertEqual((self.target / ".env").read_bytes(), original)
        self.assertEqual(report["config_keys_merged"], [])
        self.assertTrue(report["errors"])
        self.assertNotIn("dummy-private-value", json.dumps(report))
        self.assertFalse(json.loads((self.target / "profile_migration.json").read_text())["completed"])
        self.assertEqual(list(self.target.glob(".env-migration-*.tmp")), [])
        report = self.migrate()
        self.assertEqual(report["errors"], [])
        self.assertEqual(dotenv_values(self.target / ".env")["TOS_REGION"], "cn-beijing")
        self.assertTrue(json.loads((self.target / "profile_migration.json").read_text())["completed"])

    def test_reparse_point_is_skipped_without_python312_junction_api(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=info):
            self.assertTrue(migration._linked(Path("fake-junction")))
        with patch.object(Path, "lstat", side_effect=PermissionError("blocked")):
            self.assertTrue(migration._linked(Path("unreadable")))


class RuntimePathsTests(unittest.TestCase):
    def test_codex_and_explorer_share_fixed_root_while_source_and_override_stay_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "isolated-user"
            real = {"USERPROFILE": str(home), "LOCALAPPDATA": str(home / "AppData" / "Local")}
            virtual = {**real, "LOCALAPPDATA": str(home / "AppData" / "Local" / "Packages" / "OpenAI.Codex_test" / "LocalCache" / "Local")}
            expected = home / "FashionDanceStudio"
            self.assertEqual(runtime_paths.data_root_for(frozen=True, environ=real), expected)
            self.assertEqual(runtime_paths.data_root_for(frozen=True, environ=virtual), expected)
            self.assertEqual(runtime_paths.data_root_for(frozen=False, environ=real), runtime_paths.SOURCE_ROOT)
            custom = home / "explicit-test-profile"
            custom_env = {**virtual, "FASHION_DANCE_HOME": str(custom)}
            self.assertEqual(runtime_paths.data_root_for(frozen=True, environ=custom_env), custom)
            with patch.dict(os.environ, custom_env), patch.object(sys, "frozen", True, create=True), \
                 patch.object(runtime_paths, "DATA_ROOT", custom), \
                 patch("profile_migration.migrate_legacy_profiles") as migrate:
                runtime_paths.initialize()
                migrate.assert_not_called()

    def test_frozen_startup_migrates_before_settings_load_and_history_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "isolated-user"
            source = home / "AppData" / "Local" / "Packages" / "OpenAI.Codex_test" / "LocalCache" / "Local" / "FashionDanceStudio"
            source.mkdir(parents=True)
            set_key(str(source / ".env"), "ARK_API_KEY", "dummy-startup-ark")
            job_id = "f" * 32
            job = source / "jobs" / job_id
            job.mkdir(parents=True)
            (job / "job_state.json").write_text(json.dumps({"status": "done", "seconds": 5}), encoding="utf-8")
            (job / "dance_clip.mp4").write_bytes(b"offline-reference")
            resource = Path(temporary).resolve() / "bundle"
            resource.mkdir()
            code = """
import os, sys
from pathlib import Path
sys.frozen = True
sys._MEIPASS = sys.argv[2]
os.environ['USERPROFILE'] = sys.argv[1]
os.environ['LOCALAPPDATA'] = str(Path(sys.argv[1]) / 'AppData' / 'Local')
os.environ['APPDATA'] = str(Path(sys.argv[1]) / 'AppData' / 'Roaming')
os.environ.pop('FASHION_DANCE_HOME', None)
os.environ.pop('ARK_API_KEY', None)
import app
assert os.environ['ARK_API_KEY'] == 'dummy-startup-ark'
assert app.JOBS == Path(sys.argv[1]) / 'FashionDanceStudio' / 'jobs'
assert app.job_states[sys.argv[3]]['reference_video'].endswith('/reference_clip.mp4')
assert (app.JOBS.parent / 'profile_migration.json').is_file()
print('isolated-startup-ok')
"""
            result = subprocess.run([sys.executable, "-c", code, str(home), str(resource), job_id],
                                    cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
                                    **runtime_paths.quiet_subprocess_kwargs())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("isolated-startup-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()

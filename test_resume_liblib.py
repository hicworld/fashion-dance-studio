"""Offline guarantees for recovering an existing paid task only."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import providers_liblib as liblib
import resume_liblib


class ResumeExistingTaskTests(unittest.TestCase):
    def test_generate_platform_timeout_submits_once_preserves_task_and_never_downloads(self):
        task_uuid = "1234567890abcdef1234567890abcdef"
        payload = {"templateUuid": "abcdef1234567890abcdef1234567890",
                   "generateParams": {"workflowUuid": "abcdefabcdefabcdefabcdefabcdefab"}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Unknown status 7 messages must not be relabelled as timeouts.
            for index, (status, message, expected) in enumerate((
                (7, "generate task timeout https://example.invalid/private?token=hidden", "平台生成超时"),
                (7, "任务超时", "平台生成超时"),
                (7, "unspecified state", "未知任务状态"),
                (8, "generate task timeout", "未知任务状态"),
            )):
                with self.subTest(status=status, message=message):
                    destination = root / f"source_{index}.mp4"
                    with patch.object(liblib, "_signed_post", side_effect=[
                        {"generateUuid": task_uuid},
                        {"generateStatus": status, "generateMsg": message, "percentCompleted": 1,
                         "pointsCost": None, "accountBalance": None, "videos": []},
                    ]) as post, patch.object(liblib, "download_file") as download, \
                         patch("requests.post", side_effect=AssertionError("no real requests")):
                        with self.assertRaisesRegex(RuntimeError, expected) as raised:
                            liblib._generate(payload, "motion", destination, "a" * 64, "liblib-motion")
                    self.assertEqual([call.args[0] for call in post.call_args_list],
                                     [liblib.GENERATE_URI, liblib.STATUS_URI])
                    self.assertEqual(sum(call.args[0] == liblib.GENERATE_URI for call in post.call_args_list), 1)
                    download.assert_not_called()
                    self.assertFalse(destination.exists())
                    saved = json.loads(destination.with_name(destination.stem + "_task.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["generate_uuid"], task_uuid)
                    self.assertNotIn("https://", str(raised.exception))

    def test_resume_platform_timeout_only_queries_saved_task_and_never_generates(self):
        task_uuid = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_file = root / "source_task.json"
            task_file.write_text(json.dumps({"generate_uuid": task_uuid, "stage": "motion"}), encoding="utf-8")
            original = task_file.read_bytes()
            for index, (status, message, expected) in enumerate((
                (7, "generate task timeout https://example.invalid/private?token=hidden", "平台生成超时"),
                (7, "任务超时", "平台生成超时"),
                (7, "unspecified state", "未知任务状态"),
                (8, "generate task timeout", "未知任务状态"),
            )):
                with self.subTest(status=status, message=message):
                    destination = root / f"recovered_{index}.mp4"
                    with patch.object(liblib, "_signed_post", return_value={
                        "generateStatus": status, "generateMsg": message, "percentCompleted": 1,
                        "pointsCost": None, "accountBalance": None, "videos": [],
                    }) as post, patch.object(liblib, "download_file") as download, \
                         patch.object(liblib, "_generate", side_effect=AssertionError("must never generate")), \
                         patch("requests.post", side_effect=AssertionError("no real requests")):
                        result = resume_liblib.resume_task(task_file, destination)
                    self.assertEqual([call.args[0] for call in post.call_args_list], [liblib.STATUS_URI])
                    self.assertIn(expected, result["error"])
                    if expected == "平台生成超时":
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["failure_reason"], "platform_timeout")
                        self.assertEqual(result["generate_status"], 7)
                    self.assertNotIn("https://", result["error"])
                    download.assert_not_called()
                    self.assertFalse(destination.exists())
                    self.assertEqual(task_file.read_bytes(), original)

    def test_queries_only_status_and_does_not_create_video_before_done(self):
        task_uuid = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_file = root / "source_task.json"
            task_file.write_text(json.dumps({"generate_uuid": task_uuid, "stage": "motion"}), encoding="utf-8")
            original = task_file.read_bytes()
            destination = root / "recovered.mp4"
            states = iter((
                {"generateStatus": 2, "percentCompleted": 0.24},
                {"generateStatus": 5, "percentCompleted": 1, "pointsCost": 42,
                 "videos": [{"videoUrl": "https://example.invalid/result.mp4?token=private", "auditStatus": 3}]},
            ))
            called_uris = []

            def only_status(uri, payload):
                self.assertEqual(uri, liblib.STATUS_URI)
                self.assertNotEqual(uri, liblib.GENERATE_URI)
                self.assertEqual(payload, {"generateUuid": task_uuid})
                self.assertFalse(destination.exists())
                called_uris.append(uri)
                return next(states)

            def download_existing(url, target):
                self.assertFalse(destination.exists())
                target.write_bytes(b"existing-video")

            with patch.object(liblib, "_signed_post", side_effect=only_status), \
                 patch.object(liblib, "download_file", side_effect=download_existing) as download, \
                 patch.object(liblib, "_generate", side_effect=AssertionError("must never generate")), \
                 patch.object(liblib, "_upload_asset", side_effect=AssertionError("must never upload")), \
                 patch.object(resume_liblib.time, "sleep"), \
                 patch("requests.post", side_effect=AssertionError("no real requests")), \
                 patch("requests.get", side_effect=AssertionError("no real downloads")):
                result = resume_liblib.resume_task(task_file, destination)
            self.assertEqual(called_uris, [liblib.STATUS_URI, liblib.STATUS_URI])
            self.assertEqual(download.call_count, 1)
            self.assertEqual(destination.read_bytes(), b"existing-video")
            self.assertEqual(task_file.read_bytes(), original)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["points_cost"], 42)
            metadata = (root / "recovered_resume.json").read_text(encoding="utf-8")
            self.assertNotIn("https://", metadata)
            self.assertNotIn("token=private", metadata)

    def test_unfinished_timeout_never_downloads_or_creates_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_file = root / "source_task.json"
            task_file.write_text(json.dumps({"generate_uuid": "1234567890abcdef1234567890abcdef", "stage": "motion"}), encoding="utf-8")
            destination = root / "not_done.mp4"
            with patch.object(liblib, "_signed_post", return_value={"generateStatus": 2}) as status, \
                 patch.object(liblib, "download_file") as download, \
                 patch.object(liblib, "_generate", side_effect=AssertionError("must never generate")), \
                 patch.object(resume_liblib.time, "monotonic", side_effect=[0, 0, 3601, 3601]), \
                 patch("requests.post", side_effect=AssertionError("no real requests")):
                result = resume_liblib.resume_task(task_file, destination)
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(status.call_args.args[0], liblib.STATUS_URI)
            self.assertFalse(destination.exists())
            download.assert_not_called()

    def test_failure_redacts_reason_and_task_json_cannot_be_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_file = root / "source_task.json"
            task_file.write_text(json.dumps({"generate_uuid": "1234567890abcdef1234567890abcdef", "stage": "motion"}), encoding="utf-8")
            with patch.object(liblib, "_signed_post", return_value={"generateStatus": 6, "generateMsg": "failed https://example.invalid/private?token=secret"}), \
                 patch.object(liblib, "download_file") as download:
                result = resume_liblib.resume_task(task_file, root / "failed.mp4")
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("https://", result["error"])
            download.assert_not_called()
            with self.assertRaises(ValueError):
                resume_liblib.resume_task(task_file, task_file)


if __name__ == "__main__":
    unittest.main()

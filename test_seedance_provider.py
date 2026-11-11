"""Offline Seedance 2.5 payload, asset references and query-only recovery."""
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

import providers_seedance as seedance


class Response:
    def __init__(self, value=None, status_code=200, *, chunks=None, headers=None):
        self.value = value
        self.status_code = status_code
        self.chunks = chunks if chunks is not None else [b"offline generated video" * 16]
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


class SeedanceProviderTests(unittest.TestCase):
    identifier = "cgt-offline-20261021-001"
    privacy_code = "InputImageSensitiveContentDetected.PrivacyInformation"
    request_id = "0217925101994992a4bcff7a815297f0c14dc5136a71ea2afa993"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.refs = [self.folder / f"role_{number}.png" for number in range(1, 4)]
        for ref in self.refs:
            Image.new("RGB", (512, 768), "white").save(ref)
        self.roles = ["开场左侧穿粉衣的人", "开场右侧戴眼镜的人", "后方穿蓝衣的人"]
        self.clip = self.folder / "dance_clip.mp4"
        self.clip.write_bytes(b"offline source video" * 16)
        self.output = self.folder / "fashion_dance_source.mp4"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"ARK_API_KEY": "offline-seedance-key"}))
        self.valid_probe = {"width": 720, "height": 960, "duration": 5.0, "fps": 30.0,
                            "video_codec": "h264", "audio_codecs": ["aac"]}
        self.probe = self.stack.enter_context(patch.object(seedance, "_probe", return_value=self.valid_probe.copy()))
        self.upload_missing = self.stack.enter_context(patch.object(seedance, "upload_missing", return_value=[]))
        self.upload = self.stack.enter_context(patch.object(seedance.storage_tos, "upload_file", side_effect=lambda p: "https://uploads.example/" + p.name))
        self.post = self.stack.enter_context(patch.object(seedance.requests, "post", return_value=Response({"id": self.identifier})))
        self.get = self.stack.enter_context(patch.object(seedance.requests, "get", side_effect=self.success_get))
        self.wait = self.stack.enter_context(patch.object(seedance, "_wait"))

    def success_get(self, url, **kwargs):
        if url.startswith(seedance.CREATE_URL):
            self.assertEqual(url, seedance.CREATE_URL + "/" + self.identifier)
            task = json.loads(seedance._paths(self.output)[0].read_text(encoding="utf-8"))
            task.setdefault("resolution", "1080p")
            self.assertEqual(task, seedance._task_record(self.identifier, task.get("resolution", "1080p")))
            self.assertFalse(kwargs["allow_redirects"])
            return Response({"id": self.identifier, "model": seedance.MODEL, "status": "succeeded",
                             "content": {"video_url": "https://download.example/video.mp4?signature=private"},
                             "usage": {"completion_tokens": 300000, "total_tokens": 300000,
                                       "tool_usage": {"web_search": 0}, "secret": "not public"},
                             "duration": 4, "resolution": "1080p", "framespersecond": 24})
        return Response()

    def edit(self, **kwargs):
        return seedance.video_edit(self.refs[:2], self.clip, self.output,
                                    role_descriptions=self.roles[:2], prompt="保留两人互相看向对方的互动", **kwargs)

    def task(self):
        task = seedance._task_record(self.identifier)
        seedance._paths(self.output)[0].write_text(json.dumps(task), encoding="utf-8")
        return task

    def validate(self, **kwargs):
        return seedance.validate_edit_inputs(self.refs[:2], self.clip, role_descriptions=self.roles[:2], **kwargs)

    def refused(self, code=None, **extra):
        return {"error": {"code": code or self.privacy_code, "type": "BadRequest", "param": "content[1]",
                          "message": "The request failed because the input image 'content[1]', 'content[2]' may contain real person. "
                                     "offline-seedance-key https://private.example/video?token=private-url "
                                     "Request ID: " + self.request_id}, **extra}

    def test_native_edit_payload_and_exact_task_record_before_any_query(self):
        events = []
        result = self.edit(progress=events.append)
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args, (seedance.CREATE_URL,))
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.post.call_args.kwargs["headers"]["Authorization"], "Bearer offline-seedance-key")
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], seedance.MODEL)
        self.assertEqual(body["omni_reference_task_type"], "edit")
        self.assertEqual(body["duration"], -1)
        self.assertEqual(body["ratio"], "adaptive")
        self.assertEqual(body["resolution"], "1080p")
        self.assertEqual(body["output_format"], "mp4")
        self.assertTrue(body["watermark"])
        self.assertTrue(body["generate_audio"])
        prompt = body["content"][0]["text"]
        self.assertIn("编辑@视频1", prompt)
        self.assertIn("“开场左侧穿粉衣的人”替换为@图片1", prompt)
        self.assertIn("“开场右侧戴眼镜的人”替换为@图片2", prompt)
        self.assertIn("保留两人互相看向对方的互动", prompt)
        self.assertEqual(body["content"][1], {"type": "image_url", "role": "reference_image", "image_url": {"url": "https://uploads.example/role_1.png"}})
        self.assertEqual(body["content"][2]["image_url"]["url"], "https://uploads.example/role_2.png")
        self.assertEqual(body["content"][3], {"type": "video_url", "role": "reference_video", "video_url": {"url": "https://uploads.example/dance_clip.mp4"}})
        for forbidden in ("frames", "seed", "enable_safety_checker", "motion_bucket", "task_type", "width", "height"):
            self.assertNotIn(forbidden, body)
        self.assertTrue(self.output.exists())
        self.assertEqual(self.upload.call_count, 3)
        self.assertEqual(result["provider"], "volcengine-ark")
        self.assertEqual(result["operation"], "video_edit")
        self.assertEqual(result["requested_resolution"], "1080p")
        self.assertTrue(result["restore_reference_audio"])
        self.assertEqual(result["usage"], {"completion_tokens": 300000, "total_tokens": 300000, "tool_usage": {"web_search": 0}})
        for forbidden in ("video_url", "raw_result", "width", "height", "secret", "prompt"):
            self.assertNotIn(forbidden, result)
        self.assertNotIn("signature=", json.dumps(result))
        self.assertEqual(events[-1]["status"], "downloaded")
        for path in seedance._paths(self.output)[:2]:
            record = path.read_text(encoding="utf-8")
            self.assertNotIn("offline-seedance-key", record)
            self.assertNotIn("https://uploads.example", record)
            self.assertNotIn("穿粉衣", record)

    def test_timeout_marks_uncertain_and_blocks_all_reposting_and_upload(self):
        self.post.side_effect = requests.Timeout("unknown acceptance")
        with self.assertRaises(seedance.MotionSubmissionUncertainError):
            self.edit()
        self.assertTrue(seedance._paths(self.output)[1].exists())
        self.assertFalse(seedance._paths(self.output)[0].exists())
        self.upload.reset_mock()
        with patch.object(seedance, "validate_edit_inputs", side_effect=AssertionError("marker must win")):
            with self.assertRaises(seedance.MotionSubmissionUncertainError):
                self.edit()
            with self.assertRaises(seedance.MotionSubmissionUncertainError):
                seedance.resume_motion(self.output)
        self.post.assert_called_once()
        self.get.assert_not_called()
        self.upload.assert_not_called()

    def test_one_submit_survives_transient_queries_then_restart_get_only(self):
        self.get.side_effect = requests.Timeout("query outage")
        with self.assertRaises(seedance.MotionTransientError):
            self.edit()
        self.post.assert_called_once()
        self.assertEqual(json.loads(seedance._paths(self.output)[0].read_text()), seedance._task_record(self.identifier))
        self.assertFalse(self.output.exists())
        self.upload.reset_mock()
        self.get.side_effect = self.success_get
        result = seedance.resume_motion(self.output)
        self.assertEqual(result["task_id"], self.identifier)
        self.assertTrue(self.output.exists())
        self.upload.assert_not_called()
        self.post.assert_called_once()

    def test_cached_result_download_and_completed_local_source_need_no_key(self):
        task = self.task()
        seedance._paths(self.output)[2].write_text(json.dumps({**task, "status": "done", "video_url": "https://download.example/cached.mp4",
                                                             "usage": {"completion_tokens": 1, "total_tokens": 1}}), encoding="utf-8")
        with patch.dict(os.environ, {"ARK_API_KEY": ""}):
            first = seedance.resume_motion(self.output)
            self.assertEqual(first["usage"]["completion_tokens"], 1)
            self.assertEqual(self.get.call_count, 1)
            self.get.reset_mock()
            second = seedance.resume_motion(self.output)
        self.assertTrue(second["reused_local_video"])
        self.assertEqual(second["usage"]["completion_tokens"], 1)
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_asset_references_replace_uploads_and_normalize_ids(self):
        self.upload_missing.side_effect = AssertionError("all-approved assets need no TOS")
        result = self.edit(video_asset_id="asset-video-001", role_asset_ids=["asset://asset-role-001", "asset-role-002"])
        self.upload.assert_not_called()
        self.upload_missing.assert_not_called()
        content = self.post.call_args.kwargs["json"]["content"]
        self.assertEqual(content[1]["image_url"]["url"], "asset://asset-role-001")
        self.assertEqual(content[2]["image_url"]["url"], "asset://asset-role-002")
        self.assertEqual(content[3]["video_url"]["url"], "asset://asset-video-001")
        self.assertNotIn("asset://", json.dumps(result))

    def test_mixed_assets_upload_only_unbound_local_media(self):
        self.edit(video_asset_id="asset://asset-video-001", role_asset_ids=["asset-role-001", ""])
        self.upload.assert_called_once_with(self.refs[1])
        self.upload_missing.assert_called_once()

    def test_invalid_asset_ids_do_not_upload_or_submit_and_local_files_required(self):
        for value in ("https://example/asset-id", "asset://", "asset://asset-good/../x", "asset://asset-x?token=y", "asset://asset-x#x", "../asset-abc", "file:///C:/a"):
            with self.subTest(asset=value), self.assertRaises(ValueError):
                self.edit(video_asset_id=value)
        for roles in (["asset-role-001"], "asset://asset-role-001", ["asset-a1", object()]):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                self.edit(role_asset_ids=roles)
        self.refs[0].unlink()
        with self.assertRaises(ValueError):
            self.edit(video_asset_id="asset-video-001", role_asset_ids=["asset-role-001", "asset-role-002"])
        self.post.assert_not_called()
        self.get.assert_not_called()
        self.upload.assert_not_called()

    def test_ordinary_upload_requires_tos_but_readiness_does_not(self):
        self.upload_missing.return_value = ["请配置TOS_BUCKET"]
        with patch.object(seedance.storage_tos, "available", return_value=["请配置TOS_BUCKET"]):
            self.assertEqual(seedance.available("edit"), [])
        with self.assertRaisesRegex(ValueError, "TOS_BUCKET"):
            self.edit()
        self.post.assert_not_called()
        self.upload.assert_not_called()
        self.assertFalse(seedance._paths(self.output)[1].exists())

    def test_role_counts_mappings_and_prompt_reject_locally(self):
        cases = [([], []), (self.refs + [self.refs[0]], self.roles + ["第四人"]),
                 (self.refs[:2], self.roles[:1]), (self.refs[:1], [" "]),
                 (self.refs[:1], ["长" * 201])]
        for refs, roles in cases:
            with self.subTest(count=len(refs), roles=len(roles)), self.assertRaises(ValueError):
                seedance.video_edit(refs, self.clip, self.output, role_descriptions=roles)
        for prompt in (object(), "长" * 1001):
            with self.assertRaises(ValueError):
                self.validate(prompt=prompt)
        with self.assertRaises(ValueError):
            self.validate(resolution="480p")
        for number in (1, 2, 3):
            validated = seedance.validate_edit_inputs(self.refs[:number], self.clip, role_descriptions=self.roles[:number])
            self.assertEqual(validated["reference_count"], number)
            self.assertIn(f"@图片{number}", validated["request_prompt"])
        self.upload.assert_not_called()
        self.post.assert_not_called()

    def test_video_bounds_codec_and_fps_are_checked_before_upload(self):
        invalid = [{"duration": 3.99}, {"duration": 30.01}, {"duration": float("nan")},
                   {"fps": 23.99}, {"fps": 60.01}, {"fps": float("inf")},
                   {"width": 299}, {"height": 6001}, {"width": 400, "height": 600},
                   {"width": 1080, "height": 3000}, {"width": 3300, "height": 2600},
                   {"video_codec": "av1"}, {"audio_codecs": ["opus"]}]
        for changes in invalid:
            self.probe.return_value = {**self.valid_probe, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.edit()
        for fps, duration in ((24, 4), (60, 30)):
            self.probe.return_value = {**self.valid_probe, "duration": duration, "fps": fps}
            self.assertEqual(self.validate()["video_fps"], fps)
        self.upload.assert_not_called()
        self.post.assert_not_called()

    def test_invalid_images_and_oversized_files_do_not_submit(self):
        for size in ((299, 768), (512, 6001), (300, 1000)):
            Image.new("RGB", size).save(self.refs[0])
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.edit()
        Image.new("RGB", (512, 768)).save(self.refs[0])
        with self.refs[0].open("r+b") as output:
            output.truncate(seedance.MAX_IMAGE_BYTES)
        with self.assertRaises(ValueError):
            self.edit()
        Image.new("RGB", (512, 768)).save(self.refs[0])
        with self.clip.open("r+b") as output:
            output.truncate(seedance.MAX_VIDEO_BYTES + 1)
        with self.assertRaises(ValueError):
            self.edit()
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_terminal_statuses_persist_and_never_requery_or_submit(self):
        self.task()
        for phase in ("failed", "cancelled", "expired"):
            seedance._paths(self.output)[2].unlink(missing_ok=True)
            self.get.reset_mock()
            self.get.side_effect = None
            self.get.return_value = Response({"id": self.identifier, "model": seedance.MODEL, "status": phase,
                                               "error": {"code": "InputRejected", "message": "material rejected"}})
            with self.subTest(phase=phase):
                with self.assertRaises(seedance.MotionTerminalError) as error:
                    seedance.resume_motion(self.output)
                self.assertEqual(error.exception.task_status, phase)
                self.assertEqual(self.get.call_count, 1)
                record = json.loads(seedance._paths(self.output)[2].read_text())
                self.assertEqual(record["status"], phase)
                self.get.reset_mock()
                with self.assertRaises(seedance.MotionTerminalError):
                    seedance.resume_motion(self.output)
                self.get.assert_not_called()
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_wrong_task_model_endpoint_or_id_blocks_recovery(self):
        task = self.task()
        for field, value in (("provider", "fal"), ("model", "doubao-seedance-2-0-260128"),
                             ("option", "fal-kling-o3-pro-edit"), ("endpoint", "https://malicious.example"),
                             ("task_id", "cgt-good/../bad"), ("resolution", "4k")):
            seedance._paths(self.output)[0].write_text(json.dumps({**task, field: value}))
            with self.subTest(field=field), self.assertRaises(seedance.MotionSubmissionUncertainError):
                seedance.resume_motion(self.output)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_720p_is_saved_and_recovered_without_using_current_ui_resolution(self):
        self.edit(resolution="720p")
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["resolution"], "720p")
        task = json.loads(seedance._paths(self.output)[0].read_text())
        marker = json.loads(seedance._paths(self.output)[1].read_text())
        record = json.loads(seedance._paths(self.output)[2].read_text())
        self.assertEqual(task["resolution"], "720p")
        self.assertEqual(marker["resolution"], "720p")
        self.assertEqual(record["resolution"], "720p")
        self.assertEqual(record["api_resolution"], "1080p")  # Actual output is reported separately.
        self.get.reset_mock()
        result = seedance.resume_motion(self.output)
        self.assertEqual(result["requested_resolution"], "720p")
        self.assertEqual(result["api_resolution"], "1080p")
        with self.assertRaises(seedance.MotionSubmissionUncertainError):
            seedance.resume_motion(self.output, resolution="1080p")
        self.get.assert_not_called()
        self.post.assert_called_once()
        self.output.unlink()
        with self.assertRaises(seedance.MotionSubmissionUncertainError):
            seedance.resume_motion(self.output, resolution="1080p")
        self.get.assert_not_called()

    def test_old_task_without_resolution_recovers_as_1080p(self):
        task = self.task()
        task.pop("resolution")
        seedance._paths(self.output)[0].write_text(json.dumps(task))
        result = seedance.resume_motion(self.output)
        self.assertEqual(result["requested_resolution"], "1080p")
        self.assertTrue(self.output.exists())
        self.post.assert_not_called()

    def test_submit_redirect_rejection_and_malformed_response_never_retry(self):
        for response in (Response({}, 307), Response({"error": {"code": "Forbidden", "message": "no permission"}}, 403),
                         Response({}, 502), Response({"id": "../unsafe"}), Response(ValueError("invalid json"))):
            seedance._paths(self.output)[1].unlink(missing_ok=True)
            self.post.reset_mock()
            self.post.return_value = response
            with self.subTest(code=response.status_code), self.assertRaises(seedance.MotionSubmissionUncertainError):
                self.edit()
            self.post.assert_called_once()
            self.assertTrue(seedance._paths(self.output)[1].exists())
        self.get.assert_not_called()

    def test_official_400_privacy_refusal_is_terminal_and_saves_safe_atomic_proof(self):
        response = Response(self.refused(), 400)
        self.post.return_value = response
        original_atomic = seedance._atomic_record
        observed = []
        def record_atomically(path, value):
            self.assertEqual(path, seedance._paths(self.output)[1])
            self.assertEqual(seedance._read(path)["status"], "submitting")
            observed.append(value)
            return original_atomic(path, value)
        with patch.object(seedance, "_atomic_record", side_effect=record_atomically):
            with self.assertRaises(seedance.MotionTerminalError) as caught:
                self.edit()
        self.assertEqual(caught.exception.task_status, "rejected")
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.error_code, self.privacy_code)
        self.assertEqual(caught.exception.request_id, self.request_id)
        self.assertIn("角色参考图片", str(caught.exception))
        self.assertIn("真人授权", str(caught.exception))
        self.assertTrue(response.closed)
        self.assertEqual(len(observed), 1)
        self.assertFalse(seedance._paths(self.output)[0].exists())
        self.assertFalse(seedance._paths(self.output)[2].exists())
        marker = seedance._read(seedance._paths(self.output)[1])
        self.assertEqual(marker["status"], "rejected")
        self.assertEqual(marker["provider_code"], self.privacy_code)
        self.assertEqual(marker["error_code"], self.privacy_code)
        self.assertTrue(marker["rejection_verified"])
        self.assertEqual(marker["resolution"], "1080p")
        for private in ("private-url", "private.example", "offline-seedance-key", "content[1]", "content[2]", "message"):
            self.assertNotIn(private, json.dumps(marker))
            self.assertNotIn(private, str(caught.exception))
        proof = seedance.read_submission_rejection(self.output)
        self.assertEqual(set(proof), {"http_status", "error_type", "provider_code", "error_code", "request_id", "rejection_verified"})
        self.assertEqual(proof["error_code"], self.privacy_code)
        self.post.assert_called_once()
        self.get.assert_not_called()

    def test_verified_refusal_does_not_requery_upload_validate_or_resubmit(self):
        self.post.return_value = Response(self.refused(), 400)
        with self.assertRaises(seedance.MotionTerminalError):
            self.edit()
        self.upload.reset_mock()
        with patch.object(seedance, "validate_edit_inputs", side_effect=AssertionError("must preserve rejection")):
            for action in (lambda: self.edit(), lambda: seedance.resume_motion(self.output)):
                with self.assertRaises(seedance.MotionTerminalError) as caught:
                    action()
                self.assertEqual(caught.exception.task_status, "rejected")
        self.post.assert_called_once()
        self.upload.assert_not_called()
        self.get.assert_not_called()

    def test_only_exact_image_and_video_privacy_codes_at_http400_are_verified(self):
        for code in (self.privacy_code, "InputVideoSensitiveContentDetected.PrivacyInformation"):
            self.post.reset_mock()
            seedance._paths(self.output)[1].unlink(missing_ok=True)
            self.post.return_value = Response(self.refused(code), 400)
            with self.subTest(code=code), self.assertRaises(seedance.MotionTerminalError) as caught:
                self.edit()
            self.assertEqual(caught.exception.task_status, "rejected")
            self.assertEqual(seedance.read_submission_rejection(self.output)["error_code"], code)
            self.assertIn("角色参考图片" if code == self.privacy_code else "原视频", str(caught.exception))
            self.post.assert_called_once()
        self.get.assert_not_called()

    def test_bare_unknown_conflicting_and_acceptance_responses_stay_uncertain(self):
        invalid = [
            (400, {}), (400, {"error": self.privacy_code}),
            (400, {"code": self.privacy_code, "message": "material rejected"}),
            (400, {"error": {"code": self.privacy_code}}),
            (400, {"error": {"code": self.privacy_code, "message": " "}}),
            (400, {"error": {"code": self.privacy_code, "message": "x", "type": "Other"}}),
            (400, {"error": {"code": self.privacy_code, "message": "x", "type": None}}),
            (400, {"error": {"code": self.privacy_code, "message": "x", "param": []}}),
            (400, {"error": {"code": self.privacy_code, "message": "x", "unknown": "field"}}),
            (400, {"error": {"code": [self.privacy_code], "message": "x"}}),
            (400, self.refused(id="cgt-accepted")), (400, self.refused(id="")),
            (400, self.refused(task_id=None)), (400, self.refused(task={"id": "cgt-accepted"})),
            (400, self.refused(data={"task_id": "cgt-accepted"})),
            (400, self.refused(data=[{"task": "cgt-accepted"}])),
            (400, self.refused(unknown="other envelope")),
            (401, self.refused()), (422, self.refused()), (429, self.refused()),
            (500, self.refused()), (503, self.refused()),
        ]
        for code in ("InputImageSensitiveContentDetected", "InputImageSensitiveContentDetected.PolicyViolation",
                     "InputImageSensitiveContentDetected.PrivacyInformation.Other", "x" + self.privacy_code):
            invalid.append((400, self.refused(code)))
        for status, body in invalid:
            self.post.reset_mock()
            seedance._paths(self.output)[1].unlink(missing_ok=True)
            self.post.return_value = Response(body, status)
            with self.subTest(status=status, body=body), self.assertRaises(seedance.MotionSubmissionUncertainError):
                self.edit()
            self.assertIsNone(seedance.read_submission_rejection(self.output))
            self.assertEqual(seedance._read(seedance._paths(self.output)[1])["status"], "submitting")
            self.post.assert_called_once()
        self.get.assert_not_called()

    def test_success_response_with_task_and_error_is_uncertain(self):
        self.post.return_value = Response(self.refused(id=self.identifier))
        with self.assertRaises(seedance.MotionSubmissionUncertainError):
            self.edit()
        self.assertIsNone(seedance.read_submission_rejection(self.output))
        self.assertFalse(seedance._paths(self.output)[0].exists())
        self.post.assert_called_once()
        self.get.assert_not_called()

    def test_rejection_record_failure_remains_uncertain_with_original_marker(self):
        self.post.return_value = Response(self.refused(), 400)
        with patch.object(seedance, "_atomic_record", side_effect=seedance.MotionTransientError("disk failure")):
            with self.assertRaises(seedance.MotionSubmissionUncertainError):
                self.edit()
        marker = seedance._read(seedance._paths(self.output)[1])
        self.assertEqual(marker["status"], "submitting")
        self.assertNotIn("rejection_verified", marker)
        self.assertIsNone(seedance.read_submission_rejection(self.output))
        self.post.assert_called_once()
        self.get.assert_not_called()

    def test_rejection_cannot_overwrite_accepted_task_or_conflicting_marker(self):
        task = seedance._task_record(self.identifier)
        def conflicted_response(*args, **kwargs):
            seedance._paths(self.output)[0].write_text(json.dumps(task))
            return Response(self.refused(), 400)
        self.post.side_effect = conflicted_response
        with self.assertRaises(seedance.MotionSubmissionUncertainError):
            self.edit()
        self.assertEqual(seedance._read(seedance._paths(self.output)[0]), task)
        self.assertIsNone(seedance.read_submission_rejection(self.output))
        self.assertEqual(seedance._read(seedance._paths(self.output)[1])["status"], "submitting")
        self.post.assert_called_once()
        self.get.assert_not_called()

    def test_rejection_reader_checks_identity_resolution_proof_and_conflicts(self):
        self.post.return_value = Response(self.refused(), 400)
        with self.assertRaises(seedance.MotionTerminalError):
            self.edit()
        marker_path = seedance._paths(self.output)[1]
        good = seedance._read(marker_path)
        changes = [{"provider": "fal"}, {"model": "other"}, {"option": "minimax-h3-edit"},
                   {"endpoint": "https://private.invalid"}, {"resolution": "4k"}, {"resolution": {}},
                   {"status": "submitting"}, {"rejection_verified": False}, {"http_status": "400"},
                   {"http_status": True}, {"http_status": 500}, {"error_type": "Other"},
                   {"error_code": "InputRejected"}, {"provider_code": "InputRejected"},
                   {"provider_code": []}, {"task_id": ""}, {"nested": [{"id": self.identifier}]}]
        for change in changes:
            marker_path.write_text(json.dumps({**good, **change}))
            with self.subTest(change=change):
                self.assertIsNone(seedance.read_submission_rejection(self.output))
        marker_path.write_text(json.dumps(good))
        self.assertIsNotNone(seedance.read_submission_rejection(self.output))
        for conflict in (self.output, seedance._paths(self.output)[0], seedance._paths(self.output)[2]):
            conflict.write_bytes(b"conflicting file")
            self.assertIsNone(seedance.read_submission_rejection(self.output))
            conflict.unlink()

    def test_legacy_marker_is_not_reinterpreted_and_unsafe_request_id_is_not_retained(self):
        marker = {"provider": seedance.PROVIDER, "option": seedance.OPTION, "model": seedance.MODEL,
                  "endpoint": seedance.CREATE_URL, "resolution": "1080p", "status": "submitting",
                  "error_code": self.privacy_code, "http_status": 400}
        marker_path = seedance._paths(self.output)[1]
        marker_path.write_text(json.dumps(marker))
        before = marker_path.read_bytes()
        self.assertIsNone(seedance.read_submission_rejection(self.output))
        with self.assertRaises(seedance.MotionSubmissionUncertainError):
            seedance.resume_motion(self.output)
        self.assertEqual(marker_path.read_bytes(), before)
        marker_path.unlink()
        body = {"error": {"code": self.privacy_code, "message": "input may contain real person"},
                "request_id": "https://private.invalid?token=hidden"}
        self.post.return_value = Response(body, 400)
        with self.assertRaises(seedance.MotionTerminalError) as caught:
            self.edit()
        proof = seedance.read_submission_rejection(self.output)
        self.assertNotIn("request_id", proof)
        self.assertEqual(caught.exception.request_id, "")
        self.assertNotIn("private.invalid", marker_path.read_text())
        self.get.assert_not_called()

    def test_atomic_task_fsync_failure_preserves_accepted_id_and_stops(self):
        real_atomic = seedance._atomic_record
        def broken_atomic(path, value):
            if path == seedance._paths(self.output)[0]:
                raise seedance.MotionTransientError("fsync failure")
            return real_atomic(path, value)
        with patch.object(seedance, "_atomic_record", side_effect=broken_atomic):
            with self.assertRaisesRegex(seedance.MotionSubmissionUncertainError, self.identifier):
                self.edit()
        self.assertEqual(json.loads(seedance._paths(self.output)[0].read_text())["task_id"], self.identifier)
        self.assertEqual(json.loads(seedance._paths(self.output)[1].read_text())["task_id"], self.identifier)
        self.post.assert_called_once()
        self.get.assert_not_called()
        self.upload.reset_mock()
        result = seedance.resume_motion(self.output)
        self.assertEqual(result["task_id"], self.identifier)
        self.post.assert_called_once()
        self.upload.assert_not_called()

    def test_marker_fsync_failure_prevents_post(self):
        with patch.object(seedance.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(seedance.MotionSubmissionUncertainError):
                self.edit()
        self.assertTrue(seedance._paths(self.output)[1].exists())
        self.post.assert_not_called()

    def test_query_http_statuses_and_identity_mismatch_keep_original_task(self):
        self.task()
        for code, expected in ((404, "not_found"), (410, "expired")):
            seedance._paths(self.output)[2].unlink(missing_ok=True)
            self.get.reset_mock()
            self.get.side_effect = None
            self.get.return_value = Response({}, code)
            with self.subTest(code=code), self.assertRaises(seedance.MotionTerminalError) as error:
                seedance.resume_motion(self.output)
            self.assertEqual(error.exception.task_status, expected)
            self.get.assert_called_once()
        seedance._paths(self.output)[2].unlink(missing_ok=True)
        for response in (Response({}, 403), Response({}, 429), Response({}, 503),
                         Response({"id": "cgt-other", "model": seedance.MODEL, "status": "succeeded"}),
                         Response({"id": self.identifier, "model": "doubao-seedance-2-0-260128", "status": "succeeded"}),
                         Response({"id": self.identifier, "model": seedance.MODEL, "status": "unknown"})):
            self.get.reset_mock()
            self.get.return_value = response
            with self.subTest(code=response.status_code, body=response.value), self.assertRaises(seedance.MotionTransientError):
                seedance.resume_motion(self.output)
            self.assertEqual(self.get.call_count, 3)
            self.assertEqual(json.loads(seedance._paths(self.output)[0].read_text())["task_id"], self.identifier)
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_expired_cached_download_url_refreshes_only_the_existing_task(self):
        task = self.task()
        seedance._paths(self.output)[2].write_text(json.dumps({**task, "status": "done", "video_url": "https://download.example/expired.mp4"}))
        def refresh_get(url, **kwargs):
            if url.endswith("expired.mp4"):
                return Response({}, 403)
            return self.success_get(url, **kwargs)
        self.get.side_effect = refresh_get
        result = seedance.resume_motion(self.output)
        self.assertTrue(self.output.exists())
        self.assertEqual(result["task_id"], self.identifier)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.get.call_args_list[1].args[0], seedance.CREATE_URL + "/" + self.identifier)
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_api_error_details_redact_credentials_and_remote_links(self):
        self.post.return_value = Response({"error": {"code": "InputRejected", "message":
                                                     "offline-seedance-key https://storage.example/private?token=hidden"}}, 400)
        with self.assertRaises(seedance.MotionSubmissionUncertainError) as error:
            self.edit()
        text = str(error.exception)
        self.assertIn("InputRejected", text)
        self.assertNotIn("offline-seedance-key", text)
        self.assertNotIn("storage.example", text)
        self.assertNotIn("token=hidden", text)
        self.post.assert_called_once()

    def test_download_failure_is_atomic_and_resume_never_submits(self):
        self.task()
        query_response = self.success_get(seedance.CREATE_URL + "/" + self.identifier, allow_redirects=False)
        def empty_download(url, **kwargs):
            return query_response if url.startswith(seedance.CREATE_URL) else Response(chunks=[])
        self.get.side_effect = empty_download
        with self.assertRaises(seedance.MotionTransientError):
            seedance.resume_motion(self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_name(self.output.name + ".download").exists())
        self.assertTrue(seedance._paths(self.output)[2].exists())
        self.get.side_effect = self.success_get
        seedance.resume_motion(self.output)
        self.assertTrue(self.output.exists())
        self.post.assert_not_called()
        self.upload.assert_not_called()

    def test_shutdown_stops_before_upload_and_query_and_during_download(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(seedance.MotionCancelledError):
            self.edit(stop_event=stop)
        with self.assertRaises(seedance.MotionCancelledError):
            seedance.resume_motion(self.output, stop_event=stop)
        self.post.assert_not_called()
        self.upload.assert_not_called()
        self.get.assert_not_called()
        self.task()
        stop.clear()
        def interrupting_download(url, **kwargs):
            if url.startswith(seedance.CREATE_URL):
                return self.success_get(url, **kwargs)
            stop.set()
            return Response()
        self.get.side_effect = interrupting_download
        with self.assertRaises(seedance.MotionCancelledError):
            seedance.resume_motion(self.output, stop_event=stop)
        self.assertFalse(self.output.exists())
        self.assertTrue(seedance._paths(self.output)[2].exists())
        self.post.assert_not_called()

    def test_usage_sanitization_keeps_documented_numeric_counters_only(self):
        sanitized = seedance._safe_usage({"completion_tokens": True, "total_tokens": "10", "input_tokens": 3,
                                         "video_url": "https://private.example", "tool_usage": {"web_search": 2, "x": "private"}})
        self.assertEqual(sanitized, {"tool_usage": {"web_search": 2}})
        for value in (float("nan"), float("inf"), -1, 1.2, True, "3"):
            self.assertEqual(seedance._safe_usage({"completion_tokens": value}), {})

    def test_https_download_redirects_cannot_downgrade_or_use_local_hosts(self):
        for url in ("http://example/video.mp4", "https://user:pass@example/a", "https://127.0.0.1/a", "https://localhost/a", "https://example:444/a"):
            with self.subTest(url=url), self.assertRaises(seedance.MotionTransientError):
                seedance._download(url, self.output)
        self.get.return_value = Response(status_code=302, headers={"Location": "http://unsafe.example/video.mp4"})
        self.get.side_effect = None
        with self.assertRaises(seedance.MotionTransientError):
            seedance._download("https://download.example/start", self.output)
        self.get.assert_called_once()
        self.assertFalse(self.output.exists())
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

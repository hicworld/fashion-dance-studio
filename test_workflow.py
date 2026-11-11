"""Offline export, provider and preview-route regressions; no paid API calls."""

import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import app_settings
with patch.object(app_settings, "load_settings"):
    import app as workflow
import providers
import providers_liblib
from provider_common import redact_error


def _synthetic_liblib_tryon(image_value):
    """Minimal local stand-in; the identifiers are dummy values, never submitted."""
    return {
        "templateUuid": "a" * 32,
        "generateParams": {
            "workflowUuid": "b" * 32,
            "1": {"class_type": "LoadImage", "inputs": {"image": image_value}},
        },
    }


class WorkflowSmokeTest(unittest.TestCase):
    def test_legacy_helper_keeps_call_counts_and_exports_original_and_1080p(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.mp4"
            model = root / "model.png"
            garment = root / "garment.png"
            Image.new("RGB", (256, 384), "white").save(model)
            Image.new("RGB", (256, 384), "navy").save(garment)
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "color=c=white:s=320x480:r=12", "-t", "4", str(source)],
                check=True,
            )
            job_id = "offline_smoke"
            folder = root / job_id
            folder.mkdir()
            calls = []

            def fake_subscribe(endpoint, arguments):
                calls.append((endpoint, arguments))
                if endpoint == workflow.TRYON_MODEL:
                    return {"images": [{"url": "https://example.test/model.png"}]}
                return {"video": {"url": "https://example.test/video.mp4"}}

            fake_client = SimpleNamespace(
                upload_file=lambda path: "https://example.test/" + Path(path).name,
                subscribe=fake_subscribe,
            )
            state = {
                "status": "running", "stage": "排队中", "image": None, "video": None, "error": None
            }

            def fake_download(url, path):
                shutil.copyfile(source if url.endswith(".mp4") else model, path)

            def fake_motion(image, clip, destination, option, **kwargs):
                calls.append((providers.KLING_ENDPOINTS[option], {"character_orientation": "video"}))
                fake_download("https://example.test/video.mp4", destination)
                return {"provider": "fal", "model": providers.KLING_ENDPOINTS[option]}

            with patch.object(workflow, "JOBS", root), patch.object(workflow, "BASE_MODEL", model), \
                 patch.object(workflow, "job_states", {job_id: state}), \
                 patch.dict(sys.modules, {"fal_client": fake_client}), \
                 patch.object(providers, "download_file", side_effect=fake_download), \
                 patch("providers_fal.available", return_value=[]), \
                 patch("providers_fal.motion", side_effect=fake_motion), \
                 patch.dict(os.environ, {"FAL_KEY": "offline-test-placeholder"}):
                workflow.run_job(job_id, garment, source, 0, 3)
                self.assertEqual(state["status"], "done")
                self.assertTrue((folder / "dance_clip.mp4").exists())
                self.assertEqual([call[0] for call in calls], [workflow.TRYON_MODEL, workflow.MOTION_MODEL])
                self.assertEqual(calls[1][0], "fal-ai/kling-video/v3/pro/motion-control")
                self.assertNotIn("resolution", calls[1][1])
                self.assertEqual(calls[1][1]["character_orientation"], "video")
                self.assertEqual(workflow.video_dimensions(folder / "fashion_dance.mp4"), (1080, 1920))
                self.assertEqual(workflow.video_dimensions(folder / "fashion_dance_source.mp4"), (320, 480))
                info = json.loads((folder / "output_info.json").read_text(encoding="utf-8"))
                self.assertTrue(info["upscaled"])
                self.assertTrue(info["padded"])
                self.assertEqual(info["selection"]["motion"]["id"], providers.DEFAULT_MOTION)
                self.assertEqual(info["actual_models"]["motion"]["model"], workflow.MOTION_MODEL)

    def test_export_preserves_landscape_frame_and_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "landscape.mp4"
            output = Path(temp) / "portrait.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "color=c=red:s=640x360:r=4", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                 "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                check=True,
            )
            info = workflow.export_1080p(source, output)
            self.assertEqual(workflow.video_dimensions(output), (1080, 1920))
            self.assertTrue(info["padded"])
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(output)],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("audio", [s["codec_type"] for s in json.loads(probe.stdout)["streams"]])
            # The center remains red and the top is black: no crop or stretch-to-fill.
            for y, expected_red in [(10, False), (960, True)]:
                pixels = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1", "-vf",
                     f"crop=2:2:540:{y},format=rgb24", "-f", "rawvideo", "pipe:1"],
                    check=True, capture_output=True,
                ).stdout
                red, green, blue = pixels[:3]
                self.assertTrue(red > 200 and green < 30 and blue < 30 if expected_red else max(red, green, blue) < 20)

    def test_exact_1080p_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "exact.mp4"
            output = Path(temp) / "export.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", "color=c=white:s=1080x1920:r=4", "-t", "0.25", str(source)], check=True,
            )
            info = workflow.export_1080p(source, output)
            self.assertFalse(info["resized"])
            self.assertFalse(info["upscaled"])
            self.assertEqual(source.read_bytes(), output.read_bytes())

    def test_jimeng_export_restores_reference_audio_without_shortening_video(self):
        with tempfile.TemporaryDirectory() as temp:
            source, reference, output = (Path(temp) / name for name in ("silent.mp4", "reference.wav", "export.mp4"))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=8",
                            "-t", "0.5", str(source)], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "0.25", str(reference)], check=True)
            info = workflow.export_1080p(source, output, reference)
            self.assertTrue(info["reference_audio_restored"])
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                                    "-of", "json", str(output)], check=True, capture_output=True, text=True)
            media = json.loads(probe.stdout)
            self.assertIn("audio", [stream["codec_type"] for stream in media["streams"]])
            self.assertGreaterEqual(float(media["format"]["duration"]), 0.5)

    def test_full_length_export_has_exact_reference_duration_at_30fps(self):
        with tempfile.TemporaryDirectory() as temp:
            source, reference, output = (Path(temp) / name for name in ("native.mp4", "reference.wav", "export.mp4"))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=180x320:r=16",
                            "-t", "2", str(source)], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "1.9", str(reference)], check=True)
            info = workflow.export_1080p(source, output, reference, target_duration=1.9, output_fps=30)
            self.assertEqual(info["source_fps"], 16)
            self.assertEqual(info["output_fps"], 30)
            self.assertAlmostEqual(info["output_duration_seconds"], 1.9, places=3)
            with self.assertRaisesRegex(ValueError, "短于参考"):
                workflow.export_1080p(source, Path(temp) / "too_long.mp4", target_duration=3, output_fps=30)

    def test_key_is_not_rendered_in_page(self):
        dummy_key = "test-credential-never-display"
        with patch.dict(os.environ, {"FAL_KEY": dummy_key}):
            response = workflow.app.test_client().get("/")
            page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(dummy_key, page)
        self.assertIn("API", page)

    def test_missing_key_does_not_start_paid_job(self):
        with patch.dict(os.environ, {"ARK_API_KEY": ""}), \
             patch.object(workflow.threading, "Thread") as thread:
            response = workflow.app.test_client().post("/api/jobs")
        self.assertEqual(response.status_code, 400)
        self.assertIn("ARK_API_KEY", response.json["error"])
        thread.assert_not_called()


class ModelSelectionTest(unittest.TestCase):
    def test_liblib_error_explains_provider_rejection_without_exposing_credentials(self):
        secret = "private-liblib-error-test"
        response = SimpleNamespace(ok=True, status_code=200, json=lambda: {
            "code": 100000,
            "msg": "模型暂未通过审核校验 " + secret + " https://example.test/file?Signature=private",
        })
        with patch.dict(os.environ, {"LIBLIB_ACCESS_KEY": secret, "LIBLIB_SECRET_KEY": secret}), \
             patch.object(providers_liblib.requests, "post", return_value=response):
            with self.assertRaises(RuntimeError) as caught:
                providers_liblib._signed_post(providers_liblib.GENERATE_URI, {})
        message = str(caught.exception)
        self.assertIn("模型暂未通过审核校验", message)
        self.assertIn("提交生成", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("Signature", message)

    def test_single_garment_workflow_does_not_upload_builtin_person(self):
        payload = _synthetic_liblib_tryon(providers_liblib.GARMENT_IMAGE)
        self.assertEqual(providers_liblib._validate_template(payload, "tryon"), [])
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            garment = folder / "garment.png"
            Image.new("RGB", (256, 384), "navy").save(garment)
            with patch.object(providers_liblib, "available", return_value=[]), \
                 patch.object(providers_liblib, "_load_template", return_value=(payload, "test-sha")), \
                 patch.object(providers_liblib, "_upload_image", return_value="https://example.test/garment.png") as upload, \
                 patch.object(providers_liblib, "_generate", return_value={"model": "test-workflow"}) as generate:
                info = providers_liblib.tryon(folder / "unused-person.png", garment, folder / "output.png", "liblib-tryon")
        upload.assert_called_once_with(garment)
        self.assertFalse(info["reference_model_used"])
        self.assertEqual(info["input_mode"], "garment_only")
        self.assertEqual(generate.call_args.args[0]["generateParams"]["1"]["inputs"]["image"], "https://example.test/garment.png")

    def test_unmapped_original_workflow_is_blocked(self):
        payload = _synthetic_liblib_tryon("https://example.test/unmapped.png")
        errors = providers_liblib._validate_template(payload, "tryon")
        self.assertEqual(len(errors), 1)
        self.assertIn(providers_liblib.GARMENT_IMAGE, errors[0])

    def test_catalog_is_public_configuration_status_only(self):
        secret = "do-not-render-this-secret"
        with patch.dict(os.environ, {"FAL_KEY": secret, "ARK_API_KEY": secret}):
            response = workflow.app.test_client().get("/api/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertEqual(len(response.json["tryon"]), 3)
        self.assertEqual(len(response.json["motion"]), 4)
        self.assertTrue(next(item for item in response.json["motion"] if item["id"] == providers.DEFAULT_MOTION)["ready"])

    def test_unknown_selection_never_starts_job(self):
        with patch.object(workflow.threading, "Thread") as thread:
            response = workflow.app.test_client().post("/api/jobs", data={"motion_model": "arbitrary-paid-endpoint"})
        self.assertEqual(response.status_code, 400)
        thread.assert_not_called()

    def test_missing_motion_configuration_blocks_confirmation_without_submission(self):
        def missing(stage, option):
            return ["缺少动作工作流配置"] if stage == "motion" else []

        with tempfile.TemporaryDirectory() as temp:
            job_id = "a" * 32
            folder = Path(temp) / job_id
            folder.mkdir()
            (folder / "job_config.json").write_text(json.dumps({"motion_model": "liblib-motion"}), encoding="utf-8")
            state = {"id": job_id, "status": "awaiting_confirmation"}
            with patch.object(providers, "missing_for", side_effect=missing), \
                 patch.object(workflow, "JOBS", Path(temp)), \
                 patch.object(workflow, "job_states", {job_id: state}), \
                 patch.object(workflow.threading, "Thread") as thread:
                response = workflow.app.test_client().post(f"/api/jobs/{job_id}/confirm")
            self.assertEqual(state["status"], "awaiting_confirmation")
        self.assertEqual(response.status_code, 400)
        self.assertIn("缺少动作工作流配置", response.json["error"])
        thread.assert_not_called()

    def test_standard_routes_to_standard_without_fake_resolution(self):
        endpoint = "fal-ai/kling-video/v3/standard/motion-control"
        with patch("providers_fal.motion", return_value={"provider": "fal", "model": endpoint}) as motion:
            info = providers.motion(Path("image.png"), Path("clip.mp4"), Path("out.mp4"), "fal-kling-v3-standard")
        motion.assert_called_once_with(Path("image.png"), Path("clip.mp4"), Path("out.mp4"), "fal-kling-v3-standard",
                                       progress=None, stop_event=None)
        self.assertEqual(info["model"], endpoint)
        self.assertNotIn("resolution", info)

    def test_stages_can_mix_platforms(self):
        tryon_module = SimpleNamespace(tryon=lambda *args: {"model": "seedream-test"})
        motion_module = SimpleNamespace(motion=lambda *args: {"model": "wan-test"})
        modules = {"providers_jimeng": tryon_module, "providers_liblib": motion_module}
        with patch.object(providers.importlib, "import_module", side_effect=modules.__getitem__):
            first = providers.tryon(Path("model"), Path("garment"), Path("image"), "volc-seedream")
            second = providers.motion(Path("image"), Path("clip"), Path("video"), "liblib-motion")
        self.assertEqual(first["model"], "seedream-test")
        self.assertEqual(second["model"], "wan-test")

    def test_all_provider_credentials_and_signed_urls_are_redacted(self):
        with patch.dict(os.environ, {"VOLC_SECRET_ACCESS_KEY": "private-value", "LIBLIB_ACCESS_KEY": "private-liblib"}):
            text = redact_error("private-value private-liblib https://bucket.test/file?signature=secret")
        self.assertNotIn("private-", text)
        self.assertNotIn("signature", text)


if __name__ == "__main__":
    unittest.main()

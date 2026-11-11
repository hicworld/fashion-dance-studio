"""Offline checks for recovering paid composition outputs without resubmission."""
import os
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import providers_jimeng as provider


class CompositionTests(unittest.TestCase):
    def test_clothing_modes_send_each_reference_in_its_correct_role(self):
        for mode, has_pants in [("separate", True), ("separate", False), ("outfit", False)]:
            with self.subTest(mode=mode, has_pants=has_pants), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                refs = {}
                for name, color in [("model", (220, 10, 10)), ("garment", (10, 220, 10)),
                                    ("pants", (10, 10, 220)), ("background", (210, 210, 210))]:
                    refs[name] = root / (name + ".png")
                    Image.new("RGB", (100, 160), color).save(refs[name])
                response = Mock(ok=True, status_code=200, headers={})
                response.json.return_value = {"data": [{"url": "https://example.invalid/generated.png"}]}

                def download(_url, path):
                    Image.new("RGB", (144, 256), "white").save(path, format="PNG")

                with patch.dict(os.environ, {"ARK_API_KEY": "offline-test", "SEEDREAM_MODEL": provider.SEEDREAM_DEFAULT}), \
                     patch.object(provider.requests, "post", return_value=response) as submit, \
                     patch.object(provider, "download_file", side_effect=download):
                    info = provider.compose(refs["model"], refs["garment"], refs["background"],
                                            root / "preview.png", pants=refs["pants"] if has_pants else None,
                                            garment_mode=mode)
                expected_roles = ["model", "garment"] + (["pants"] if has_pants else []) + ["background"]
                self.assertEqual(info["reference_roles"], expected_roles)
                sent = submit.call_args.kwargs["json"]
                self.assertEqual(len(sent["image"]), len(expected_roles))
                for encoded, role in zip(sent["image"], expected_roles):
                    with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as sent_image, \
                         Image.open(refs[role]) as original:
                        self.assertEqual(sent_image.getpixel((0, 0)), original.getpixel((0, 0)))
                self.assertIn(f"图{len(expected_roles)}是背景参考", sent["prompt"])
                self.assertIn("图1是唯一的人物身份参考", sent["prompt"])
                self.assertIn("只提取服装", sent["prompt"])
                if mode == "separate" and not has_pants:
                    self.assertIn("保留图1原有的裤子", sent["prompt"])

    def test_invalid_clothing_mode_never_submits(self):
        with patch.object(provider.requests, "post") as submit:
            for kwargs in ({"garment_mode": "unknown"}, {"pants": Path("pants.png")}):
                with self.assertRaises(ValueError):
                    provider.compose(Path("model.png"), Path("garment.png"), Path("background.png"),
                                     Path("preview.png"), **kwargs)
        submit.assert_not_called()

    def test_download_recovery_uses_same_generated_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            references = []
            for name, color in [('model', 'red'), ('garment', 'blue'), ('background', 'green')]:
                path = root / (name + '.png')
                Image.new('RGB', (100, 160), color).save(path)
                references.append(path)
            destination = root / 'preview.png'
            response = Mock(ok=True, status_code=200, headers={'x-request-id': 'offline-request-id'})
            response.json.return_value = {'data': [{'url': 'https://example.invalid/generated.png?private=token'}]}

            def download(_url, path):
                Image.new('RGB', (144, 256), 'white').save(path, format='PNG')

            with patch.dict(os.environ, {'ARK_API_KEY': 'offline-test', 'SEEDREAM_MODEL': provider.SEEDREAM_DEFAULT}), \
                 patch.object(provider.requests, 'post', return_value=response) as submit, \
                 patch.object(provider, 'download_file', side_effect=RuntimeError('temporary download failure')):
                with self.assertRaisesRegex(RuntimeError, '已生成但下载失败'):
                    provider.compose(*references, destination)
                sent = submit.call_args.kwargs['json']
                self.assertEqual(len(sent['image']), 3)
                self.assertNotIn('sequential_image_generation', sent)
                self.assertNotIn('stream', sent)
                self.assertFalse(destination.exists())
            with patch.dict(os.environ, {'ARK_API_KEY': 'offline-test', 'SEEDREAM_MODEL': provider.SEEDREAM_DEFAULT}), \
                 patch.object(provider.requests, 'post', side_effect=AssertionError('must not submit twice')), \
                 patch.object(provider, 'download_file', side_effect=download):
                info = provider.compose(*references, destination)
            self.assertTrue(destination.exists())
            self.assertEqual(info['request_id'], 'offline-request-id')
            self.assertNotIn('token', str(info))
            self.assertEqual(info['reference_roles'], ['model', 'garment', 'background'])

    def test_bad_reference_fails_before_billable_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tiny = root / 'tiny.png'
            Image.new('RGB', (10, 10)).save(tiny)
            with patch.dict(os.environ, {'ARK_API_KEY': 'offline-test'}), \
                 patch.object(provider.requests, 'post') as submit:
                with self.assertRaises(ValueError):
                    provider.compose(tiny, tiny, tiny, root / 'result.png')
            submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()

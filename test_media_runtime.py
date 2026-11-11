"""Exercise the FFmpeg installer using small synthetic archives, never executable code."""

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from zipfile import ZipFile, ZipInfo

import media_runtime as media


class MediaRuntimeTests(unittest.TestCase):
    version = "1.2.3"
    prefix = "ffmpeg-1.2.3-essentials_build"
    fixtures = {"ffmpeg.exe": b"ffmpeg test fixture", "ffprobe.exe": b"ffprobe test fixture"}

    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp).resolve()
        self.destination = self.root / "installed"
        self.archive = self.root / "upstream.zip"
        self.manifest = self.root / "ffmpeg-win64.json"
        self.enterContext(patch.object(media, "MANIFEST", self.manifest))
        self.no_network = self.enterContext(
            patch.object(media, "_download", side_effect=AssertionError("Offline test attempted a download"))
        )

    @staticmethod
    def lock(payload):
        return {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    def make_archive(self, *, extra=(), wrong_member=False, wrong_archive=False):
        members = {}
        with ZipFile(self.archive, "w") as package:
            for name, payload in self.fixtures.items():
                path = f"{self.prefix}/bin/{name}"
                package.writestr(path, (b"x" + payload[1:]) if wrong_member and name == "ffprobe.exe" else payload)
                members[name] = {"path": path, **self.lock(payload)}
            for name, payload in extra:
                package.writestr(name, payload)
        manifest = {
            "version": self.version,
            "url": f"{media.UPSTREAM}{self.version}/{self.prefix}.zip",
            "archive": self.lock(self.archive.read_bytes()),
            "members": members,
        }
        if wrong_archive:
            manifest["archive"]["sha256"] = "0" * 64
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def test_install_and_repair_only_the_allowlisted_files(self):
        self.make_archive(extra=((f"{self.prefix}/bin/ffplay.exe", b"excluded"),))
        self.assertFalse(media.ffmpeg_ready(self.destination))
        self.assertEqual(media.install_ffmpeg(self.destination, archive=self.archive), self.destination)
        self.assertEqual({p.name: p.read_bytes() for p in self.destination.iterdir()}, self.fixtures)
        self.assertTrue(media.ffmpeg_ready(self.destination))
        unmodified = self.destination / "ffmpeg.exe"
        before = unmodified.stat().st_mtime_ns
        changed = self.destination / "ffprobe.exe"
        changed.write_bytes(b"x" + self.fixtures[changed.name][1:])
        self.assertFalse(media.ffmpeg_ready(self.destination))
        media.install_ffmpeg(self.destination, archive=self.archive)
        self.assertEqual(unmodified.stat().st_mtime_ns, before)
        self.assertEqual(changed.read_bytes(), self.fixtures[changed.name])
        self.assertTrue(media.ffmpeg_ready(self.destination))
        self.archive.unlink()
        self.assertEqual(media.install_ffmpeg(self.destination), self.destination)
        self.no_network.assert_not_called()

    def test_rejects_corrupt_zip_hash_before_writing_any_tools(self):
        self.make_archive(wrong_archive=True)
        with self.assertRaisesRegex(media.MediaInstallError, "SHA-256"):
            media.install_ffmpeg(self.destination, archive=self.archive)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_rejects_member_mismatch_without_installing_other_member(self):
        self.make_archive(wrong_member=True)
        with self.assertRaisesRegex(media.MediaInstallError, "SHA-256"):
            media.install_ffmpeg(self.destination, archive=self.archive)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_rejects_traversal_members_even_when_archive_hash_matches(self):
        for name in ("../out.exe", "/absolute.exe", r"bin\..\out.exe", "bin/out.exe:stream"):
            with self.subTest(name=name):
                self.make_archive(extra=((name, b"unsafe"),))
                with self.assertRaisesRegex(media.MediaInstallError, "Unexpected path"):
                    media.install_ffmpeg(self.destination, archive=self.archive)
                self.assertEqual(list(self.destination.iterdir()), [])
                self.assertFalse((self.root / "out.exe").exists())

    def test_rejects_a_link_and_case_colliding_paths(self):
        link = ZipInfo(f"{self.prefix}/bin/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entry in (link, f"{self.prefix}/bin/FFMPEG.EXE"):
            with self.subTest(entry=entry):
                self.make_archive(extra=((entry, b"unsafe"),))
                with self.assertRaisesRegex(media.MediaInstallError, "Unexpected path"):
                    media.install_ffmpeg(self.destination, archive=self.archive)
                self.assertEqual(list(self.destination.iterdir()), [])

    def test_redirect_must_remain_https_and_without_credentials(self):
        request = Request("https://github.com/")
        for target in ("http://example.invalid/file", "https://user@example.invalid/file"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "HTTPS"):
                    media._HTTPSRedirect().redirect_request(request, None, 302, "", {}, target)
        redirected = media._HTTPSRedirect().redirect_request(
            request, None, 302, "", {}, "https://release-assets.githubusercontent.com/file"
        )
        self.assertEqual(redirected.type, "https")


if __name__ == "__main__":
    unittest.main()

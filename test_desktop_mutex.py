"""Offline process test: named mutex only; no app import, GUI, or cloud API."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from desktop import SingleInstanceLock


@unittest.skipUnless(os.name == "nt", "Windows named mutex test")
class DesktopMutexTest(unittest.TestCase):
    def test_other_process_is_blocked_until_owner_releases(self) -> None:
        script = (
            "from pathlib import Path; import sys; "
            "from desktop import SingleInstanceLock; "
            "lock=SingleInstanceLock(Path(sys.argv[1])); "
            "print('acquired' if lock.acquire() else 'blocked'); lock.release()"
        )
        with tempfile.TemporaryDirectory(prefix="fashion-studio-mutex-") as temporary:
            lock = SingleInstanceLock(Path(temporary))
            self.assertTrue(lock.acquire())
            try:
                child = subprocess.run([sys.executable, "-c", script, temporary],
                    cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
                    timeout=15, check=True, creationflags=0x08000000)
                self.assertEqual(child.stdout.strip(), "blocked")
            finally:
                lock.release()
            child = subprocess.run([sys.executable, "-c", script, temporary],
                cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
                timeout=15, check=True, creationflags=0x08000000)
            self.assertEqual(child.stdout.strip(), "acquired")
            lock.release()  # Releasing twice is harmless.


if __name__ == "__main__":
    unittest.main()

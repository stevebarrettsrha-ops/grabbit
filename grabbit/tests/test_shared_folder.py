"""A grabbit folder on a drive shared by Windows, Linux and macOS.

Each system leaves its own binaries behind (.venv, ./bin/ffmpeg). Those files
exist on the other system too, and trusting them made every run fail with
"[Errno 8] Exec format error" — or, in the launchers, deleted the other
system's environment. Run with:  python -m unittest discover -s tests
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("GRABBIT_NO_BROWSER", "1")

import app  # noqa: E402

MZ = b"MZ\x90\x00" + b"\x00" * 60
ELF = b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 57
MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 60


class ForeignBinary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _file(self, name, data):
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_each_system_recognises_only_its_own(self):
        cases = {"Windows": MZ, "Linux": ELF, "Darwin": MACHO}
        for system in cases:
            with mock.patch.object(app.platform, "system", return_value=system):
                for owner, data in cases.items():
                    p = self._file(f"bin-{owner}", data)
                    self.assertEqual(app.foreign_binary(p), owner != system,
                                     f"{owner} binary seen from {system}")

    def test_scripts_and_missing_files_count_as_native(self):
        script = self._file("tool", b"#!/bin/sh\necho hi\n")
        self.assertFalse(app.foreign_binary(script))
        self.assertFalse(app.foreign_binary(self._file("empty", b"")))
        self.assertFalse(app.foreign_binary(self.tmp / "nope"))


class BundledFfmpeg(unittest.TestCase):
    """macOS and Linux both call theirs bin/ffmpeg."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for target, value in (("APP_DIR", self.tmp), ("BIN_DIR", self.bin),
                              ("FFMPEG_DIR", None)):
            patcher = mock.patch.object(app, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        path = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", path)

    def test_another_systems_ffmpeg_is_not_used_or_overwritten(self):
        (self.bin / "ffmpeg").write_bytes(MACHO)
        (self.bin / "ffprobe").write_bytes(MACHO)
        fetched = []

        def fake_fetch(dest):
            fetched.append(dest)
            (dest / "ffmpeg").write_bytes(ELF)
            (dest / "ffprobe").write_bytes(ELF)

        with mock.patch.object(app.platform, "system", return_value="Linux"), \
             mock.patch.object(app.os, "name", "posix"), \
             mock.patch.object(app.shutil, "which", return_value=None), \
             mock.patch.object(app, "_fetch_ffmpeg", fake_fetch):
            app.ensure_ffmpeg()
            self.assertEqual(fetched, [self.bin / "linux"])
            self.assertEqual(app.FFMPEG_DIR, str(self.bin / "linux"))
            self.assertEqual(app._tool("ffmpeg"), str(self.bin / "linux" / "ffmpeg"))
        # macOS's own copy is still there, untouched.
        self.assertEqual((self.bin / "ffmpeg").read_bytes(), MACHO)

    def test_native_ffmpeg_in_bin_is_used(self):
        (self.bin / "ffmpeg").write_bytes(ELF)
        (self.bin / "ffprobe").write_bytes(ELF)
        with mock.patch.object(app.platform, "system", return_value="Linux"), \
             mock.patch.object(app.os, "name", "posix"), \
             mock.patch.object(app, "_fetch_ffmpeg", side_effect=AssertionError):
            app.ensure_ffmpeg()
            self.assertEqual(app.FFMPEG_DIR, str(self.bin))

    def test_windows_exe_left_in_bin_is_never_run_on_linux(self):
        (self.bin / "ffmpeg.exe").write_bytes(MZ)
        (self.bin / "ffprobe.exe").write_bytes(MZ)
        with mock.patch.object(app.platform, "system", return_value="Linux"), \
             mock.patch.object(app.os, "name", "posix"), \
             mock.patch.object(app.shutil, "which", return_value="/usr/bin/ffmpeg"):
            app.ensure_ffmpeg()
            self.assertIsNone(app.FFMPEG_DIR)
            self.assertEqual(app._tool("ffprobe"), "ffprobe")


@unittest.skipUnless(platform.system() == "Linux" and shutil.which("bash"),
                     "the Linux launcher runs on Linux")
class LinuxLauncher(unittest.TestCase):
    """start-linux.sh must not delete or rebuild a Windows .venv."""

    def test_windows_venv_is_left_alone(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(APP_DIR / "start-linux.sh", tmp / "start-linux.sh")
        # Stop the run before it reaches pip: a fake python3 that can create
        # nothing, so the launcher reports and exits right after its venv step.
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        fake = fakebin / "python3"
        fake.write_text("#!/bin/sh\n"
                        "case \"$*\" in *'-m venv'*) echo \"$3\" > \"$PWD/venv-target\"; exit 1;; esac\n"
                        "exit 0\n")
        fake.chmod(0o755)
        win = tmp / ".venv" / "Scripts"
        win.mkdir(parents=True)
        (win / "python.exe").write_bytes(MZ)
        (tmp / ".venv" / "pyvenv.cfg").write_text("home = C:\\Python313\n")
        env = dict(os.environ, PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
                   GRABBIT_NO_TERMINAL="1")
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        r = subprocess.run(["bash", str(tmp / "start-linux.sh")], cwd=tmp, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=60)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((win / "python.exe").read_bytes(), MZ)
        self.assertTrue((tmp / ".venv" / "pyvenv.cfg").exists())
        self.assertEqual((tmp / "venv-target").read_text().strip(), ".venv-linux")
        self.assertIn("set up on Windows", r.stdout)


if __name__ == "__main__":
    unittest.main()

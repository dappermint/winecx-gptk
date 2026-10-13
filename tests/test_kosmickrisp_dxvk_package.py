import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


class DxvkPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dxvk payload with spaces ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = Path(__file__).resolve().parents[1] / "runtime/kosmickrisp"
        self.script = self.root / "package-dxvk.sh"
        shutil.copyfile(source / self.script.name, self.script)
        shutil.copytree(source / "licenses", self.root / "licenses")
        self.archive = self.root / "release.tar.gz"
        self.output = self.root / "runtime with spaces/DXVK3"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text('''#!/usr/bin/env python3
import os, shutil, sys
shutil.copyfile(os.environ["DXVK_TEST_ARCHIVE"], sys.argv[sys.argv.index("-o") + 1])
''')
        curl.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                        DXVK_TEST_ARCHIVE=str(self.archive))

    def make_archive(self, missing=None):
        with tarfile.open(self.archive, "w:gz") as archive:
            for arch in ("x32", "x64"):
                for dll in ("d3d8", "d3d9", "d3d10core", "d3d11", "dxgi"):
                    name = f"{arch}/{dll}.dll"
                    if name == missing:
                        continue
                    data = name.encode()
                    member = tarfile.TarInfo(f"dxvk-3.1.1/{name}")
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.write_pins(self.digest)

    def write_pins(self, digest):
        (self.root / "pins.env").write_text(
            f"DXVK_KOSMICKRISP_VERSION=3.1.1\nDXVK_KOSMICKRISP_SHA256={digest}\n")

    def package(self):
        return subprocess.run(["bash", str(self.script), str(self.output)],
                              env=self.env, text=True, capture_output=True)

    def test_relocated_payload_and_integrity_manifest(self):
        self.make_archive()
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.output.glob("*/*.dll"))), 10)
        self.assertIn(f"DXVK_ARCHIVE_SHA256={self.digest}",
                      (self.output / "SOURCE.txt").read_text())
        self.assertIn("Copyright", (self.output / "LICENSE.txt").read_text())
        check = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"],
                               cwd=self.output, text=True, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        (self.output / "x32/d3d9.dll").write_bytes(b"corrupted")
        check = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"],
                               cwd=self.output, text=True, capture_output=True)
        self.assertNotEqual(check.returncode, 0)

    def test_hash_mismatch_leaves_no_payload(self):
        self.make_archive()
        self.write_pins("0" * 64)
        self.assertNotEqual(self.package().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_incomplete_release_leaves_no_payload(self):
        self.make_archive(missing="x32/d3d9.dll")
        self.assertNotEqual(self.package().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_missing_license_leaves_no_payload(self):
        self.make_archive()
        (self.root / "licenses/dxvk-3.1.1.txt").unlink()
        self.assertNotEqual(self.package().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_existing_backend_is_preserved(self):
        self.make_archive()
        self.output.mkdir(parents=True)
        marker = self.output / "existing.dll"
        marker.write_bytes(b"existing backend")
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(marker.read_bytes(), b"existing backend")


if __name__ == "__main__":
    unittest.main()

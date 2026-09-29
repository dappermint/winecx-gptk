"""Exercise launcher isolation without starting Wine or requiring a GPU."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kosmickrisp relocated ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "Wine/bin"
        self.bundle = self.root / "Wine/lib/kosmickrisp"
        self.bin.mkdir(parents=True)
        self.bundle.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "runtime/kosmickrisp/wine-kosmickrisp"
        shutil.copy(source, self.bin / "wine-kosmickrisp")
        for name in ("libvulkan.1.dylib", "libvulkan_kosmickrisp.dylib", "kosmickrisp_icd.json"):
            (self.bundle / name).touch()
        # A fake wine64 records the environment and argument boundaries.
        wine = self.bin / "wine64"
        wine.write_text('''#!/usr/bin/env python3
import json, os, sys
print(json.dumps({"args": sys.argv[1:], "env": dict(os.environ)}))
sys.exit(int(os.environ.get("FAKE_WINE_EXIT", "0")))
''')
        wine.chmod(0o755)

    def run_launcher(self, **extra):
        env = dict(os.environ, WINEPREFIX="prefix with spaces",
                   CX_ACTIVE_GRAPHICS_BACKEND="dxvk", VK_DRIVER_FILES="foreign.json",
                   VK_ICD_FILENAMES="old.json", VK_ADD_DRIVER_FILES="additional.json",
                   VK_LOADER_DRIVERS_SELECT="*Molten*", VK_LOADER_DRIVERS_DISABLE="*kosmic*")
        env.update(extra)
        return subprocess.run(["bash", str(self.bin / "wine-kosmickrisp"),
                               "game with spaces.exe", "", "--argument=$literal"],
                              env=env, text=True, capture_output=True)

    def test_relocated_selection_and_argument_preservation(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["args"], ["game with spaces.exe", "", "--argument=$literal"])
        env = report["env"]
        self.assertEqual(Path(env["CX_LIBVULKAN"]).resolve(), self.bundle / "libvulkan.1.dylib")
        self.assertEqual(Path(env["VK_DRIVER_FILES"]).resolve(), self.bundle / "kosmickrisp_icd.json")
        # win32u only honours CX_LIBVULKAN under this backend value
        self.assertEqual(env["CX_ACTIVE_GRAPHICS_BACKEND"], "wined3d")
        self.assertEqual(env["WINEPREFIX"], "prefix with spaces")
        for key in ("VK_ICD_FILENAMES", "VK_ADD_DRIVER_FILES", "VK_LOADER_DRIVERS_SELECT", "VK_LOADER_DRIVERS_DISABLE"):
            self.assertNotIn(key, env)

    def test_missing_component_fails_before_wine(self):
        (self.bundle / "kosmickrisp_icd.json").unlink()
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Missing KosmicKrisp component", result.stderr)

    def test_exit_code_propagates(self):
        self.assertEqual(self.run_launcher(FAKE_WINE_EXIT="17").returncode, 17)


if __name__ == "__main__":
    unittest.main()

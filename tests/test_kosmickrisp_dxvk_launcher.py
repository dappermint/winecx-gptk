import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('prefix_tests', Path(__file__).with_name('test_kosmickrisp_dxvk_prefix.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class DxvkLauncherTests(unittest.TestCase):
    manifest = fixtures.PrefixTests.manifest
    assert_restored = fixtures.PrefixTests.assert_restored

    def setUp(self):
        fixtures.PrefixTests.setUp(self)
        self.runtime = self.root / 'Libraries'
        self.bin = self.runtime / 'Wine/bin'
        self.bin.mkdir(parents=True)
        share = self.runtime / 'Wine/share/kosmickrisp'
        share.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp'
        for name in ('wine-kosmickrisp', 'wine-kosmickrisp-dxvk'):
            shutil.copyfile(source / name, self.bin / name)
            (self.bin / name).chmod(0o755)
        shutil.copyfile(source / 'dxvk-prefix.py', share / 'dxvk-prefix.py')
        shutil.copytree(self.payload, self.runtime / 'DXVK3')
        bundle = self.runtime / 'Wine/lib/kosmickrisp'
        bundle.mkdir(parents=True)
        for name in ('libvulkan.1.dylib', 'libvulkan_kosmickrisp.dylib', 'kosmickrisp_icd.json'):
            (bundle / name).touch()
        wine = self.bin / 'wine64'
        wine.write_text('#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({"env":dict(os.environ),"args":sys.argv[1:]}))\nsys.exit(int(os.environ.get("FAKE_WINE_EXIT","0")))\n')
        wine.chmod(0o755)

    def launch(self, args, **extra):
        env = dict(os.environ, WINEPREFIX=str(self.prefix))
        env.pop('MESA_SHADER_CACHE_DIR', None)
        env.pop('DXVK_SHADER_CACHE_PATH', None)
        env.update(extra)
        return subprocess.run([str(self.bin / 'wine-kosmickrisp-dxvk'), *args], env=env, capture_output=True, text=True)

    def test_launch_requires_valid_setup(self):
        result = self.launch(['game.exe'])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_launch_sets_persistent_wine_accessible_caches(self):
        self.assertEqual(self.launch(['--install-prefix']).returncode, 0)
        result = self.launch(['game with spaces.exe', '', '$literal'], WINE_VULKAN_LIBRARY='foreign', WINEDLLOVERRIDES='d3d11=b;mscoree=d')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['args'], ['game with spaces.exe', '', '$literal'])
        self.assertEqual(report['env']['DXVK_SHADER_CACHE_PATH'], 'C:\\ShaderCaches\\kosmickrisp\\dxvk')
        self.assertEqual(report['env']['MESA_SHADER_CACHE_DIR'], str(self.prefix / 'drive_c/ShaderCaches/kosmickrisp/mesa'))
        self.assertEqual(report['env']['WINEDLLOVERRIDES'], 'd3d11=b;mscoree=d;d3d8,d3d9,d3d10core,d3d11,dxgi=n')
        self.assertNotIn('WINE_VULKAN_LIBRARY', report['env'])
        self.assertTrue((self.prefix / 'drive_c/ShaderCaches/kosmickrisp/dxvk').is_dir())

    def test_explicit_cache_overrides_and_exit_status(self):
        self.launch(['--install-prefix'])
        result = self.launch(['game.exe'], MESA_SHADER_CACHE_DIR='custom mesa', DXVK_SHADER_CACHE_PATH='D:\\custom', FAKE_WINE_EXIT='17')
        self.assertEqual(result.returncode, 17)
        env = json.loads(result.stdout)['env']
        self.assertEqual(env['MESA_SHADER_CACHE_DIR'], 'custom mesa')
        self.assertEqual(env['DXVK_SHADER_CACHE_PATH'], 'D:\\custom')
        self.assertFalse((self.prefix / 'drive_c/ShaderCaches').exists())

    def test_restore_survives_removed_driver_and_payload(self):
        self.launch(['--install-prefix'])
        shutil.rmtree(self.runtime / 'Wine/lib/kosmickrisp')
        shutil.rmtree(self.runtime / 'DXVK3')
        result = self.launch(['--restore-prefix'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_restored()


if __name__ == '__main__':
    unittest.main()

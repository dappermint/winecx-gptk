import importlib.util
import json
from pathlib import Path
import sys
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

module_dir = Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp'
sys.path.insert(0, str(module_dir))
try:
    spec = importlib.util.spec_from_file_location('investigate_transaction_test', module_dir / 'investigate.py')
    investigate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(investigate)
finally:
    sys.path.pop(0)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.original_config = b'Windowed 0\r\nBorderlessWindow 1\r\nFullscreenWindow 1\r\nWindowTop 22\r\nScreenWidth 1024\r\n'
        (self.profile / 'PCCONFIG.TXT').write_bytes(self.original_config)
        (self.profile / 'SAVEDGAMES/nested').mkdir(parents=True)
        (self.profile / 'SAVEDGAMES/nested/save').write_bytes(b'original save')
        self.game = self.root / 'game.exe'
        self.game.write_bytes(b'game')
        self.run = self.root / 'run'
        self.run.mkdir()
        self.plan = {'profile': str(self.profile), 'game_executable': str(self.game)}
        self.appid = self.root / 'steam_appid.txt'

    def configure_dxvk(self):
        source = self.root / 'dxvk'
        source.mkdir()
        for name in ('d3d11.dll', 'dxgi.dll'):
            (source / name).write_bytes(('DXVK ' + name).encode())
        self.plan.update(dxvk_directory=str(source),
                         dxvk_sha256={name: investigate.sha(source / name)
                                      for name in ('d3d11.dll', 'dxgi.dll')})
        return source

    def test_game_local_dlls_restored_without_touching_bottle(self):
        source = self.configure_dxvk()
        (self.root / 'd3d11.dll').write_bytes(b'original local DLL')
        system = self.root / 'bottle/system32'
        system.mkdir(parents=True)
        for name in ('d3d11.dll', 'dxgi.dll'):
            (system / name).write_bytes(b'DXMT global')
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        for name in ('d3d11.dll', 'dxgi.dll'):
            self.assertEqual((self.root / name).read_bytes(), (source / name).read_bytes())
        transaction.restore()
        self.assertEqual((self.root / 'd3d11.dll').read_bytes(), b'original local DLL')
        self.assertFalse((self.root / 'dxgi.dll').exists())
        for name in ('d3d11.dll', 'dxgi.dll'):
            self.assertEqual((system / name).read_bytes(), b'DXMT global')
        self.assertEqual(list(self.root.glob('.kk-dll-*')), [])

    def test_bad_dxvk_hash_fails_before_profile_mutation(self):
        source = self.configure_dxvk()
        (source / 'dxgi.dll').write_bytes(b'changed DLL')
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        with self.assertRaisesRegex(RuntimeError, 'source_hash_mismatch'):
            transaction.prepare()
        self.assertFalse(transaction.journal.exists())
        self.assertFalse((self.root / 'd3d11.dll').exists())
        self.assertEqual((self.profile / 'PCCONFIG.TXT').read_bytes(), self.original_config)

    def test_second_dll_install_failure_restores_first_and_profile(self):
        self.configure_dxvk()
        original = investigate.ProfileTransaction.install_file
        def fail_second(source, target):
            if target.name == 'dxgi.dll':
                raise OSError('injected install failure')
            return original(source, target)
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        with patch.object(investigate.ProfileTransaction, 'install_file', side_effect=fail_second):
            with self.assertRaises(OSError):
                transaction.prepare()
        self.assertTrue((self.root / 'd3d11.dll').exists())
        investigate.ProfileTransaction(self.plan, self.run).restore()
        self.assertFalse((self.root / 'd3d11.dll').exists())
        self.assertFalse((self.root / 'dxgi.dll').exists())
        self.assertEqual((self.profile / 'PCCONFIG.TXT').read_bytes(), self.original_config)

    def test_dll_backup_corruption_and_redirected_target_rejected(self):
        self.configure_dxvk()
        (self.root / 'd3d11.dll').write_bytes(b'original')
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        (self.run / 'backup/d3d11.dll').write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'dll_backup_corrupt'):
            transaction.restore()
        state = json.loads(transaction.journal.read_text())
        state['dlls']['d3d11.dll']['path'] = str(self.root / 'other.dll')
        transaction.journal.write_text(json.dumps(state))
        with self.assertRaisesRegex(RuntimeError, 'dll_transaction_target_mismatch'):
            transaction.restore()

    def test_legacy_journal_without_dlls_restores(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        state = json.loads(transaction.journal.read_text())
        state.pop('dlls')
        transaction.journal.write_text(json.dumps(state))
        transaction.restore()
        self.assertEqual((self.profile / 'PCCONFIG.TXT').read_bytes(), self.original_config)

    def test_restore_existing_appid_and_save_is_exact_and_idempotent(self):
        self.appid.write_bytes(b'previous appid\r\n')
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        self.assertIn('Windowed 1', (self.profile / 'PCCONFIG.TXT').read_text())
        self.assertEqual(self.appid.read_text(), '920210\n')
        (self.profile / 'SAVEDGAMES/nested/save').write_bytes(b'game mutation')
        (self.profile / 'SAVEDGAMES/new').write_bytes(b'new')
        transaction.restore()
        transaction.restore()
        self.assertEqual((self.profile / 'PCCONFIG.TXT').read_bytes(), self.original_config)
        self.assertEqual((self.profile / 'SAVEDGAMES/nested/save').read_bytes(), b'original save')
        self.assertFalse((self.profile / 'SAVEDGAMES/new').exists())
        self.assertEqual(self.appid.read_bytes(), b'previous appid\r\n')
        self.assertEqual(json.loads(transaction.journal.read_text())['state'], 'restored')

    def test_missing_appid_removed_after_recovery(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        investigate.ProfileTransaction(self.plan, self.run).restore()
        self.assertFalse(self.appid.exists())

    def test_restore_after_partial_prepare_failure(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        original_write = Path.write_text
        def fail_appid(path, *args, **kwargs):
            if path == self.appid:
                raise OSError('injected appid write failure')
            return original_write(path, *args, **kwargs)
        with patch.object(Path, 'write_text', fail_appid):
            with self.assertRaises(OSError):
                transaction.prepare()
        transaction.restore()
        self.assertEqual((self.profile / 'PCCONFIG.TXT').read_bytes(), self.original_config)
        self.assertFalse(self.appid.exists())

    def test_backup_failure_leaves_live_profile_unchanged(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        with patch.object(investigate.shutil, 'copytree', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                transaction.prepare()
        transaction.restore()
        self.assertEqual((self.profile / 'PCCONFIG.TXT').read_bytes(), self.original_config)
        self.assertEqual((self.profile / 'SAVEDGAMES/nested/save').read_bytes(), b'original save')
        self.assertFalse(self.appid.exists())

    def test_failed_restore_remains_recoverable(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        with patch.object(investigate.gameplay, 'replace_save', side_effect=OSError('injected restore failure')):
            with self.assertRaises(OSError):
                transaction.restore()
        self.assertNotEqual(json.loads(transaction.journal.read_text())['state'], 'restored')
        investigate.ProfileTransaction(self.plan, self.run).restore()
        self.assertEqual(json.loads(transaction.journal.read_text())['state'], 'restored')

    def test_existing_journal_requires_recovery(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        with self.assertRaisesRegex(RuntimeError, 'recovery'):
            transaction.prepare()
        transaction.restore()

    def test_recovery_rejects_changed_target(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        changed = dict(self.plan, profile=str(self.root / 'other-profile'))
        with self.assertRaisesRegex(RuntimeError, 'target_mismatch'):
            investigate.ProfileTransaction(changed, self.run).restore()
        transaction.restore()

    def test_recovery_rejects_corrupt_save_backup(self):
        transaction = investigate.ProfileTransaction(self.plan, self.run)
        transaction.prepare()
        (self.run / 'backup/SAVEDGAMES/nested/save').write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'backup_corrupt'):
            transaction.restore()
        self.assertEqual((self.profile / 'SAVEDGAMES/nested/save').read_bytes(), b'original save')

    def test_steam_arguments_are_scoped_by_mapped_executable(self):
        listing = ('11 C:\\Steam\\steam.exe -cef-disable-gpu\n'
                   '12 C:\\Steam\\steamwebhelper.exe --disable-gpu -steampid=90\n'
                   '13 C:\\Steam\\steam.exe -cef-force-gpu\n'
                   '14 C:\\Steam\\steamwebhelper.exe --disable-gpu -steampid=99\n')
        mapped = {11: '/target/drive_c/Program Files (x86)/Steam/steam.exe',
                  12: '/target/drive_c/Program Files (x86)/Steam/bin/cef/steamwebhelper.exe',
                  13: '/other/drive_c/Program Files (x86)/Steam/steam.exe',
                  14: '/other/drive_c/Program Files (x86)/Steam/bin/cef/steamwebhelper.exe'}
        def command(args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 5)
            if args[0] == 'ps':
                return CompletedProcess(args, 0, listing)
            return CompletedProcess(args, 0, 'n' + mapped[int(args[args.index('-p') + 1])] + '\n')
        with patch.object(investigate.subprocess, 'run', side_effect=command):
            result = investigate.steam_arguments('/target')
        self.assertEqual(len(result), 2)
        self.assertTrue(any('-steampid=90' in arg for arg in result))
        self.assertFalse(any('force-gpu' in arg for arg in result))

    def test_gpu_off_browser_accepts_utility_and_swiftshader_children(self):
        prefix = 'C:\\Steam\\bin\\cef\\steamwebhelper.exe '
        args = ['C:\\Steam\\steam.exe -silent -cef-disable-gpu',
                prefix + '-steampid=90 --disable-gpu --disable-gpu-compositing',
                prefix + '--type=utility --utility-sub-type=network.mojom.NetworkService --lang=en-US',
                prefix + '--type=renderer --lang=en-US --disable-gpu-compositing',
                prefix + '--type=gpu-process --use-angle=swiftshader-webgl --gpu-preferences=UAAAAAAAAA']
        self.assertTrue(investigate.steam_gpu_disabled(args))
        with patch.object(investigate, 'steam_arguments', return_value=args), patch.object(investigate.stutter, 'steam_start') as launch:
            self.assertEqual(investigate.ensure_steam({'bottle': '/target'}, '/helper', self.run), args)
            launch.assert_not_called()

    def test_gpu_off_requires_browser_and_rejects_hardware_backend(self):
        prefix = 'C:\\Steam\\steamwebhelper.exe '
        browser = prefix + '-steampid=90 --disable-gpu'
        self.assertFalse(investigate.steam_gpu_disabled([prefix + '--type=utility --disable-gpu']))
        self.assertFalse(investigate.steam_gpu_disabled([prefix + '-steampid=90']))
        self.assertFalse(investigate.steam_gpu_disabled([browser, prefix + '--type=gpu-process']))
        with self.assertRaisesRegex(RuntimeError, 'hardware_gpu'):
            investigate.steam_gpu_disabled([browser, prefix + '--type=gpu-process --use-angle=metal'])
        with self.assertRaisesRegex(RuntimeError, 'forced_gpu'):
            investigate.steam_gpu_disabled(['C:\\Steam\\steam.exe -cef-force-gpu', browser])

    def test_cleanup_does_not_touch_preexisting_or_reused_steam_pid(self):
        self.assertEqual(investigate.cleanup_steam({}, '/helper', self.run), {'owned': False})
        (self.run / 'steam-owner.json').write_text(json.dumps({'pid': 123, 'identity': 'old'}))
        with patch.object(investigate, 'same_process', return_value=False), patch.object(investigate.subprocess, 'run') as command:
            self.assertTrue(investigate.cleanup_steam({}, '/helper', self.run)['exited'])
            command.assert_not_called()

    def test_bottle_lease_exclusive_and_released(self):
        directory = self.root / 'locks'
        first = investigate.BottleLease(self.root / 'bottle', directory)
        try:
            with self.assertRaisesRegex(RuntimeError, 'already_running'):
                investigate.BottleLease(self.root / 'bottle', directory)
            independent = investigate.BottleLease(self.root / 'other-bottle', directory)
            independent.close()
        finally:
            first.close()
        replacement = investigate.BottleLease(self.root / 'bottle', directory)
        replacement.close()

    def test_cache_runs_are_isolated_copies_with_matching_seed_hash(self):
        seed = self.root / 'seed'
        (seed / 'mesa-cache').mkdir(parents=True)
        (seed / 'mesa-cache/blob').write_bytes(b'original cache')
        bundle = self.root / 'bundle'
        bundle.mkdir()
        (bundle / 'libvulkan.1.dylib').write_bytes(b'loader')
        bottle = self.root / 'bottle'
        system = bottle / 'drive_c/windows/system32'
        system.mkdir(parents=True)
        for name in ('d3d11.dll', 'dxgi.dll'):
            (system / name).write_bytes(name.encode())
        self.plan.update(bundle=str(bundle), bottle=str(bottle), runtime=str(self.root / 'runtime'), driver_sha256='driver')
        first, second = self.root / 'first', self.root / 'second'
        investigate.prepare_run(self.plan, first, seed, True, [])
        investigate.prepare_run(self.plan, second, seed, False, [])
        a = json.loads((first / 'provenance.json').read_text())
        b = json.loads((second / 'provenance.json').read_text())
        self.assertEqual(a['cache_seed_sha256'], b['cache_seed_sha256'])
        (first / 'cache/mesa-cache/blob').write_bytes(b'compiled')
        self.assertEqual((second / 'cache/mesa-cache/blob').read_bytes(), b'original cache')
        self.assertEqual((seed / 'mesa-cache/blob').read_bytes(), b'original cache')
        fresh = self.root / 'fresh'
        investigate.prepare_run(self.plan, fresh, None, True, [])
        self.assertEqual(investigate.inventory(fresh / 'cache'), {})
        self.assertEqual(json.loads((fresh / 'provenance.json').read_text())['cache_mode'], 'fresh')


if __name__ == '__main__':
    unittest.main()

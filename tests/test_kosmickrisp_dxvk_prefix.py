import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/dxvk-prefix.py'
spec = importlib.util.spec_from_file_location('dxvk_prefix', SCRIPT)
dxvk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dxvk)


class PrefixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='DXVK recovery with spaces ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prefix = self.root / 'prefix'
        self.payload = self.root / 'DXVK3'
        for lane, arch in dxvk.LANES:
            (self.prefix / 'drive_c/windows' / lane).mkdir(parents=True)
            for core in ('kernel32.dll', 'ntdll.dll'):
                (self.prefix / 'drive_c/windows' / lane / core).write_bytes(b'initialized core DLL')
            (self.payload / arch).mkdir(parents=True)
            for name in dxvk.NAMES:
                pe = bytearray(128)
                pe[:2] = b'MZ'
                struct.pack_into('<I', pe, 60, 64)
                pe[64:68] = b'PE\0\0'
                struct.pack_into('<H', pe, 68, 0x8664 if arch == 'x64' else 0x14c)
                pe[96:] = (name.encode() + b'\0' * 32)[:32]
                (self.payload / arch / f'{name}.dll').write_bytes(pe)
        (self.payload / 'SOURCE.txt').write_text('DXVK_VERSION=3.1.1\n')
        (self.payload / 'LICENSE.txt').write_text('test license\n')
        self.manifest()
        self.original = 'WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\DllOverrides] 123\n"d3d9"="builtin"\n"unrelated"="native,builtin"\n\n[Other] 124\n"setting"="original"\n'
        (self.prefix / 'user.reg').write_text(self.original)
        self.original_dll = self.prefix / 'drive_c/windows/system32/d3d11.dll'
        self.original_dll.write_bytes(b'previous renderer')
        self.link = self.prefix / 'drive_c/windows/syswow64/d3d9.dll'
        self.link.symlink_to('/missing/original/wine/d3d9.dll')

    def manifest(self):
        paths = sorted(self.payload.glob('*/*.dll')) + [self.payload / 'LICENSE.txt', self.payload / 'SOURCE.txt']
        (self.payload / 'SHA256SUMS').write_text(''.join(f'{dxvk.digest(p)}  {p.relative_to(self.payload)}\n' for p in paths))

    def assert_restored(self):
        self.assertEqual((self.prefix / 'user.reg').read_text(), self.original)
        self.assertEqual(self.original_dll.read_bytes(), b'previous renderer')
        self.assertEqual(os.readlink(self.link), '/missing/original/wine/d3d9.dll')
        self.assertFalse((self.prefix / dxvk.RECEIPT).exists())
        self.assertFalse((self.prefix / dxvk.BACKUP).exists())
        self.assertFalse((self.prefix / 'drive_c/windows/system32/dxgi.dll').exists())

    def test_round_trip_and_idempotent_install(self):
        self.assertTrue(dxvk.install(self.prefix, self.payload)['valid'])
        receipt = (self.prefix / dxvk.RECEIPT).read_bytes()
        self.assertTrue(dxvk.install(self.prefix, self.payload)['valid'])
        self.assertEqual(receipt, (self.prefix / dxvk.RECEIPT).read_bytes())
        self.assertTrue(dxvk.status(self.prefix)['valid'])
        dxvk.restore(self.prefix)
        self.assert_restored()

    def test_source_bin_install_is_reversible(self):
        game = self.root / 'Portal 2/bin'
        game.mkdir(parents=True)
        original = game / 'd3d9.dll'
        original.write_bytes(b'game renderer')
        dxvk.install(self.prefix, self.payload, game, 'x32', ['d3d9'])
        self.assertEqual(dxvk.digest(original), dxvk.digest(self.payload / 'x32/d3d9.dll'))
        dxvk.restore(self.prefix)
        self.assertEqual(original.read_bytes(), b'game renderer')
        self.assert_restored()

    def test_restore_does_not_need_payload(self):
        dxvk.install(self.prefix, self.payload)
        (self.payload / 'x64/d3d11.dll').unlink()
        dxvk.restore(self.prefix)
        self.assert_restored()

    def test_changed_renderer_is_never_overwritten_by_restore(self):
        dxvk.install(self.prefix, self.payload)
        self.original_dll.write_bytes(b'another installation')
        self.assertFalse(dxvk.status(self.prefix)['valid'])
        with self.assertRaisesRegex(ValueError, 'outside this transaction'):
            dxvk.restore(self.prefix)
        self.assertEqual(self.original_dll.read_bytes(), b'another installation')
        self.assertTrue((self.prefix / dxvk.RECEIPT).is_file())

    def test_registry_edits_outside_managed_overrides_survive(self):
        dxvk.install(self.prefix, self.payload)
        registry = self.prefix / 'user.reg'
        registry.write_text(registry.read_text().replace('"setting"="original"', '"setting"="changed"'))
        dxvk.restore(self.prefix)
        self.assertIn('"setting"="changed"', registry.read_text())
        self.assertEqual(dxvk.registry_values(registry.read_text()), dxvk.registry_values(self.original))

    def test_changed_managed_registry_value_blocks_restore(self):
        dxvk.install(self.prefix, self.payload)
        registry = self.prefix / 'user.reg'
        registry.write_text(registry.read_text().replace('"dxgi"="native"', '"dxgi"="builtin"'))
        with self.assertRaisesRegex(ValueError, 'override changed'):
            dxvk.restore(self.prefix)
        self.assertEqual(dxvk.digest(self.original_dll), dxvk.digest(self.payload / 'x64/d3d11.dll'))

    def test_corrupt_backup_blocks_restore_before_any_write(self):
        dxvk.install(self.prefix, self.payload)
        (self.prefix / dxvk.BACKUP / '3').write_bytes(b'corrupt backup')
        with self.assertRaisesRegex(ValueError, 'backup'):
            dxvk.restore(self.prefix)
        self.assertTrue(dxvk.status(self.prefix)['valid'])

    def test_interrupted_install_can_restore(self):
        real_copy = dxvk.atomic_copy
        installs = 0
        def crash(source, target):
            nonlocal installs
            real_copy(source, target)
            if source.parent == self.prefix / dxvk.BACKUP and source.name.startswith('payload-'):
                installs += 1
                if installs == 4:
                    raise KeyboardInterrupt('simulate abrupt termination')
        with patch.object(dxvk, 'atomic_copy', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                dxvk.install(self.prefix, self.payload)
        self.assertEqual(dxvk.status(self.prefix)['state'], 'installing')
        dxvk.restore(self.prefix)
        self.assert_restored()

    def test_interrupted_backup_can_restore(self):
        real_copy = dxvk.atomic_copy
        def crash(source, target):
            real_copy(source, target)
            raise KeyboardInterrupt()
        with patch.object(dxvk, 'atomic_copy', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                dxvk.install(self.prefix, self.payload)
        self.assertEqual(dxvk.status(self.prefix)['state'], 'preparing')
        dxvk.restore(self.prefix)
        self.assert_restored()

    def test_interrupted_restore_can_resume(self):
        dxvk.install(self.prefix, self.payload)
        real_copy = dxvk.atomic_copy
        def crash(source, target):
            real_copy(source, target)
            raise KeyboardInterrupt()
        with patch.object(dxvk, 'atomic_copy', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                dxvk.restore(self.prefix)
        dxvk.restore(self.prefix)
        self.assert_restored()

    def test_copy_failure_rolls_back(self):
        real_copy = dxvk.atomic_copy
        def fail_once(source, target):
            if source == self.payload / 'x32/d3d11.dll':
                raise OSError('simulated copy failure')
            real_copy(source, target)
        with patch.object(dxvk, 'atomic_copy', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'simulated'):
                dxvk.install(self.prefix, self.payload)
        self.assert_restored()

    def test_external_change_during_backup_is_preserved(self):
        real_copy = dxvk.atomic_copy
        def edit_during_backup(source, target):
            real_copy(source, target)
            if target == self.prefix / dxvk.BACKUP / 'user.reg':
                self.original_dll.write_bytes(b'concurrent backend update')
        with patch.object(dxvk, 'atomic_copy', side_effect=edit_during_backup):
            with self.assertRaisesRegex(ValueError, 'changed during backup'):
                dxvk.install(self.prefix, self.payload)
        self.assertEqual(self.original_dll.read_bytes(), b'concurrent backend update')
        self.assertEqual((self.prefix / 'user.reg').read_text(), self.original)
        self.assertFalse((self.prefix / dxvk.RECEIPT).exists())

    def test_payload_change_during_staging_cannot_reach_prefix(self):
        real_copy = dxvk.atomic_copy
        def corrupt_during_stage(source, target):
            real_copy(source, target)
            if target.name == 'payload-0':
                target.write_bytes(b'corrupted staged DLL')
        with patch.object(dxvk, 'atomic_copy', side_effect=corrupt_during_stage):
            with self.assertRaisesRegex(ValueError, 'payload changed during staging'):
                dxvk.install(self.prefix, self.payload)
        self.assert_restored()

    def test_registry_change_during_install_is_preserved_on_rollback(self):
        real_copy = dxvk.atomic_copy
        registry = self.prefix / 'user.reg'
        def edit_during_install(source, target):
            real_copy(source, target)
            if source.name == 'payload-0' and target == self.prefix / 'drive_c/windows/system32/d3d8.dll':
                registry.write_text(registry.read_text().replace('"setting"="original"', '"setting"="concurrent"'))
        with patch.object(dxvk, 'atomic_copy', side_effect=edit_during_install):
            with self.assertRaisesRegex(ValueError, 'registry changed before replacement'):
                dxvk.install(self.prefix, self.payload)
        self.assertIn('"setting"="concurrent"', registry.read_text())
        self.assertEqual(dxvk.registry_values(registry.read_text()), dxvk.registry_values(self.original))
        self.assertEqual(self.original_dll.read_bytes(), b'previous renderer')
        self.assertFalse((self.prefix / dxvk.RECEIPT).exists())

    def test_bad_hash_or_architecture_rejected_before_backup(self):
        dll = self.payload / 'x32/d3d11.dll'
        dll.write_bytes((self.payload / 'x64/d3d11.dll').read_bytes())
        with self.assertRaisesRegex(ValueError, 'integrity'):
            dxvk.install(self.prefix, self.payload)
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'architecture'):
            dxvk.install(self.prefix, self.payload)
        self.assertFalse((self.prefix / dxvk.RECEIPT).exists())

    def test_symlinked_directory_rejected(self):
        directory = self.prefix / 'drive_c/windows/system32'
        directory.rename(self.root / 'external')
        directory.symlink_to(self.root / 'external', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'non-symlink'):
            dxvk.install(self.prefix, self.payload)

    def test_running_wine_rejected(self):
        original_exists = Path.exists
        def exists(path):
            return path.name == 'socket' or original_exists(path)
        with patch.object(Path, 'exists', exists):
            with self.assertRaisesRegex(ValueError, 'Wine is running'):
                dxvk.install(self.prefix, self.payload)

    def test_incomplete_wow64_prefix_rejected_before_install(self):
        (self.prefix / 'drive_c/windows/syswow64/kernel32.dll').unlink()
        with self.assertRaisesRegex(ValueError, 'initialization is incomplete'):
            dxvk.install(self.prefix, self.payload)
        self.assertFalse((self.prefix / dxvk.RECEIPT).exists())


if __name__ == '__main__':
    unittest.main()

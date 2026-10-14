import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/source-snapshot.py'
spec = importlib.util.spec_from_file_location('snapshot', SCRIPT)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='Mesa committed snapshot ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True)
        driver = self.source / 'src/kosmickrisp/vulkan'
        driver.mkdir(parents=True)
        (driver / 'kk_physical_device.c').write_text('committed source\n')
        (self.source / 'executable').write_text('executable\n')
        (self.source / 'executable').chmod(0o755)
        (self.source / 'link').symlink_to('executable')
        subprocess.run(['git', '-C', str(self.source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.source), '-c', 'user.name=Snapshot test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], check=True)
        self.commit = subprocess.check_output(['git', '-C', str(self.source), 'rev-parse', 'HEAD'], text=True).strip()
        self.output = self.root / 'snapshot'

    def test_export_is_deterministic_and_ignores_uncommitted_edits(self):
        first = snapshot.export(self.source, self.commit, self.output)
        (self.source / 'src/kosmickrisp/vulkan/kk_physical_device.c').write_text('uncommitted\n')
        second = snapshot.export(self.source, self.commit, self.root / 'second')
        self.assertEqual(first, second)
        snapshot.extract(self.output, self.root / 'unpacked')
        self.assertEqual((self.root / 'unpacked/src/kosmickrisp/vulkan/kk_physical_device.c').read_text(), 'committed source\n')
        self.assertEqual((self.root / 'unpacked/executable').stat().st_mode & 0o777, 0o755)
        self.assertTrue((self.root / 'unpacked/link').is_symlink())
        snapshot.check_source(self.output, self.root / 'unpacked')
        (self.root / 'unpacked/src/kosmickrisp/vulkan/kk_physical_device.c').write_text('modified\n')
        with self.assertRaisesRegex(ValueError, 'content changed'):
            snapshot.check_source(self.output, self.root / 'unpacked')

    def test_corruption_rejected_before_extraction(self):
        snapshot.export(self.source, self.commit, self.output)
        archive = self.output / 'mesa-source.tar.gz'
        archive.write_bytes(archive.read_bytes() + b'corruption')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            snapshot.extract(self.output, self.root / 'unpacked')
        self.assertFalse((self.root / 'unpacked').exists())

    def malicious_archive(self, members):
        self.output.mkdir()
        archive = self.output / 'mesa-source.tar.gz'
        with tarfile.open(archive, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': self.commit}) as stream:
            for name, link in members:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = link
                    stream.addfile(member)
                else:
                    member.size = 1
                    stream.addfile(member, io.BytesIO(b'x'))
        (self.output / 'snapshot.json').write_text(json.dumps({'schema': 1, 'commit': self.commit,
            'tree': '0' * 40, 'archive': archive.name, 'archive_sha256': snapshot.sha(archive)}))

    def test_traversal_rejected(self):
        self.malicious_archive([('../outside', None)])
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            snapshot.extract(self.output, self.root / 'unpacked')
        self.assertFalse((self.root / 'outside').exists())

    def test_escaping_symlink_rejected(self):
        self.malicious_archive([('link', '../outside')])
        with self.assertRaisesRegex(ValueError, 'symlink'):
            snapshot.extract(self.output, self.root / 'unpacked')

    def test_existing_source_is_not_overwritten(self):
        snapshot.export(self.source, self.commit, self.output)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            snapshot.extract(self.output, self.source)

    def test_incorrect_commit_identity_is_rejected(self):
        snapshot.export(self.source, self.commit, self.output)
        manifest = self.output / 'snapshot.json'
        data = json.loads(manifest.read_text())
        data['commit'] = '0' * 40
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'commit differs'):
            snapshot.verify(self.output)


if __name__ == '__main__':
    unittest.main()

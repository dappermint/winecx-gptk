#!/usr/bin/env python3
"""Install, inspect and restore upstream DXVK in a stopped Wine prefix."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile

NAMES = ('d3d8', 'd3d9', 'd3d10core', 'd3d11', 'dxgi')
LANES = (('system32', 'x64'), ('syswow64', 'x32'))
RECEIPT = '.winecx-kosmickrisp-dxvk.json'
BACKUP = '.winecx-kosmickrisp-dxvk-backup'
SECTION = r'Software\\Wine\\DllOverrides'


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(stream.read()).hexdigest()


def identity(path):
    if path.is_symlink():
        return {'link': os.readlink(path)}
    if path.is_file():
        return {'sha256': digest(path)}
    if path.exists():
        raise ValueError(f'expected a file or DLL symlink: {path}')
    return None


def atomic_copy(source, target):
    fd, name = tempfile.mkstemp(prefix='.kk-dxvk-', dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        if source.is_symlink():
            temporary.unlink()
            temporary.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, temporary)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
        temporary.replace(target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_text(target, text):
    fd, name = tempfile.mkstemp(prefix='.kk-dxvk-', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_file():
            temporary.chmod(target.stat().st_mode & 0o777)
        temporary.replace(target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def journal(prefix, data):
    atomic_text(prefix / RECEIPT, json.dumps(data, indent=2) + '\n')


def validate_prefix(prefix, stopped=False):
    for path in (prefix, prefix / 'drive_c', prefix / 'drive_c/windows',
                 prefix / 'drive_c/windows/system32', prefix / 'drive_c/windows/syswow64'):
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f'expected an initialized, non-symlink Wine directory: {path}')
    registry = prefix / 'user.reg'
    if registry.is_symlink() or not registry.is_file():
        raise ValueError('expected a regular user.reg in the initialized prefix')
    for lane, _ in LANES:
        for name in ('kernel32.dll', 'ntdll.dll'):
            path = prefix / 'drive_c/windows' / lane / name
            if not path.is_file():
                raise ValueError(f'prefix initialization is incomplete: missing {lane}/{name}')
    for name in (RECEIPT, BACKUP, '.winecx-kosmickrisp-dxvk.lock'):
        if (prefix / name).is_symlink():
            raise ValueError(f'unsafe management path: {prefix / name}')
    if stopped:
        info = prefix.stat()
        socket = Path(f'/tmp/.wine-{os.getuid()}/server-{info.st_dev:x}-{info.st_ino:x}/socket')
        if socket.exists():
            raise ValueError('Wine is running in this prefix; close its applications and wait for wineserver to exit before changing DLLs or registry')


def validate_payload(payload):
    sums = payload / 'SHA256SUMS'
    if sums.is_symlink() or not sums.is_file():
        raise ValueError('DXVK payload needs its packaged SHA256SUMS integrity manifest')
    expected = {f'{arch}/{name}.dll' for _, arch in LANES for name in NAMES}
    expected.update(('LICENSE.txt', 'SOURCE.txt'))
    hashes = {}
    for line in sums.read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64}) [ *](.+)', line)
        if not match or match[2] not in expected or match[2] in hashes:
            raise ValueError('invalid or duplicate DXVK integrity manifest entry')
        hashes[match[2]] = match[1]
    if set(hashes) != expected:
        raise ValueError('incomplete DXVK integrity manifest')
    for relative, sha in hashes.items():
        path = payload / relative
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or digest(path) != sha:
            raise ValueError(f'DXVK payload integrity mismatch: {relative}')
    for _, arch in LANES:
        for name in NAMES:
            data = (payload / arch / f'{name}.dll').read_bytes()
            if len(data) < 64 or data[:2] != b'MZ':
                raise ValueError(f'not a PE DLL: {arch}/{name}.dll')
            offset = struct.unpack_from('<I', data, 60)[0]
            if offset + 24 > len(data) or data[offset:offset + 4] != b'PE\0\0':
                raise ValueError(f'invalid PE header: {arch}/{name}.dll')
            machine = struct.unpack_from('<H', data, offset + 4)[0]
            if machine != (0x8664 if arch == 'x64' else 0x14c):
                raise ValueError(f'wrong PE architecture: {arch}/{name}.dll')
    source = dict(line.split('=', 1) for line in (payload / 'SOURCE.txt').read_text().splitlines() if '=' in line)
    if not source.get('DXVK_VERSION'):
        raise ValueError('missing DXVK release version')
    return source, hashes


def registry_values(text):
    active = False
    values = {}
    for line in text.splitlines(keepends=True):
        section = re.match(r'^\[([^]]+)\]', line)
        if section:
            active = section[1].lower() == SECTION.lower()
        elif active:
            value = re.match(r'^"([^"\\]+)"=', line)
            if value and value[1].lower() in NAMES:
                key = value[1].lower()
                if key in values:
                    raise ValueError(f'duplicate DLL override: {key}')
                values[key] = line
    return {name: values.get(name) for name in NAMES}


def edit_registry(text, values):
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r'^\[' + re.escape(SECTION) + r'\]', line, re.I)), None)
    if start is None:
        if not any(values.values()):
            return text
        return text.rstrip('\n') + '\n\n[' + SECTION + ']\n' + ''.join(v for v in values.values() if v) + '\n'
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('[')), len(lines))
    body = []
    for line in lines[start + 1:end]:
        match = re.match(r'^"([^"\\]+)"=', line)
        if not match or match[1].lower() not in NAMES:
            body.append(line)
    return ''.join(lines[:start + 1] + body + [v for v in values.values() if v] + lines[end:])


def installed_values():
    return {name: f'"{name}"="native"\n' for name in NAMES}


def destinations(prefix, game_dir=None, arch=None, dlls=None):
    targets = [(prefix / 'drive_c/windows' / lane / f'{name}.dll', a, name)
               for lane, a in LANES for name in NAMES]
    if game_dir:
        if not game_dir.is_dir() or game_dir.is_symlink() or game_dir.resolve() != game_dir:
            raise ValueError('game DLL directory must exist and have no symlink components')
        if not arch or not dlls:
            raise ValueError('--game-dir requires --arch and --dll arguments')
        targets += [(game_dir / f'{name}.dll', arch, name) for name in dlls]
    elif arch or dlls:
        raise ValueError('--arch/--dll require --game-dir')
    if len({str(t) for t, _, _ in targets}) != len(targets):
        raise ValueError('duplicate DLL destination')
    return targets


def load_receipt(prefix):
    path = prefix / RECEIPT
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('schema') != 2 or data.get('prefix') != str(prefix) or data.get('state') not in ('preparing', 'installing', 'installed', 'restoring', 'restored'):
        raise ValueError('unsupported, corrupt or relocated DXVK receipt; recover at the original prefix path')
    game = Path(data['game_dir']) if data.get('game_dir') else None
    targets = destinations(prefix, game, data.get('arch'), data.get('dlls'))
    if [str(t) for t, _, _ in targets] != [e['target'] for e in data['files']]:
        raise ValueError('DXVK receipt destinations differ from its installation plan')
    return data


def status(prefix):
    validate_prefix(prefix)
    data = load_receipt(prefix)
    if not data:
        return {'state': 'absent', 'valid': False}
    changed = []
    for entry in data['files']:
        if identity(Path(entry['target'])) != {'sha256': entry['installed_sha256']}:
            changed.append(entry['target'])
    if registry_values((prefix / 'user.reg').read_text()) != installed_values():
        changed.append('DLL overrides in user.reg')
    return {'state': data['state'], 'valid': data['state'] == 'installed' and not changed,
            'version': data['version'], 'changed': changed, 'files': data['files'],
            'game_dir': data.get('game_dir')}


@contextlib.contextmanager
def locked(prefix):
    validate_prefix(prefix, stopped=True)
    with (prefix / '.winecx-kosmickrisp-dxvk.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another DXVK setup transaction is running') from None
        yield


def install(prefix, payload, game_dir=None, arch=None, dlls=None):
    with locked(prefix):
        source, hashes = validate_payload(payload)
        targets = destinations(prefix, game_dir, arch, dlls)
        existing = load_receipt(prefix)
        if existing:
            report = status(prefix)
            matching = [str(t) for t, _, _ in targets] == [e['target'] for e in existing['files']]
            matching &= all(e['installed_sha256'] == hashes[f"{e['arch']}/{e['name']}.dll"] for e in existing['files'])
            if not report['valid'] or not matching:
                raise ValueError('managed installation differs or is interrupted; inspect status and restore before installing')
            return report
        backup = prefix / BACKUP
        if backup.exists():
            raise ValueError('orphaned DXVK backup exists; refusing to overwrite it')
        entries = [{'target': str(t), 'arch': a, 'name': n, 'original': identity(t),
                    'installed_sha256': hashes[f'{a}/{n}.dll']} for t, a, n in targets]
        registry = prefix / 'user.reg'
        before = registry.read_text()
        after = edit_registry(before, installed_values())
        data = {'schema': 2, 'state': 'preparing', 'prefix': str(prefix),
                'version': source['DXVK_VERSION'], 'source': source, 'files': entries,
                'game_dir': str(game_dir) if game_dir else None, 'arch': arch, 'dlls': dlls,
                'registry_original': identity(registry), 'overrides_original': registry_values(before),
                'registry_installed_sha256': hashlib.sha256(after.encode()).hexdigest()}
        journal(prefix, data)
        try:
            backup.mkdir()
            for i, entry in enumerate(entries):
                if entry['original'] is not None:
                    atomic_copy(Path(entry['target']), backup / str(i))
                staged = backup / f'payload-{i}'
                atomic_copy(payload / entry['arch'] / f"{entry['name']}.dll", staged)
                if identity(staged) != {'sha256': entry['installed_sha256']}:
                    raise ValueError('DXVK payload changed during staging')
            atomic_copy(registry, backup / 'user.reg')
            for i, entry in enumerate(entries):
                if identity(Path(entry['target'])) != entry['original']:
                    raise ValueError(f"DLL changed during backup: {entry['target']}")
                if entry['original'] is not None and identity(backup / str(i)) != entry['original']:
                    raise ValueError(f"original DLL changed during backup: {entry['target']}")
            if identity(registry) != data['registry_original'] or identity(backup / 'user.reg') != data['registry_original']:
                raise ValueError('registry changed during backup')
            data['state'] = 'installing'
            journal(prefix, data)
            for i, entry in enumerate(entries):
                if identity(Path(entry['target'])) != entry['original']:
                    raise ValueError(f"DLL changed before replacement: {entry['target']}")
                atomic_copy(backup / f'payload-{i}', Path(entry['target']))
            if identity(registry) != data['registry_original']:
                raise ValueError('registry changed before replacement')
            atomic_text(registry, after)
            data['state'] = 'installed'
            journal(prefix, data)
        except Exception:
            restore_locked(prefix, load_receipt(prefix))
            raise
        return status(prefix)


def restore_locked(prefix, data):
    if not data:
        raise ValueError('no managed DXVK installation to restore')
    backup = prefix / BACKUP
    if data['state'] in ('preparing', 'restored'):
        data['state'] = 'restored'
        journal(prefix, data)
        if backup.exists():
            shutil.rmtree(backup)
        (prefix / RECEIPT).unlink()
        sync_directory(prefix)
        return {'state': 'restored', 'valid': True}
    for i, entry in enumerate(data['files']):
        current = identity(Path(entry['target']))
        allowed = [entry['original'], {'sha256': entry['installed_sha256']}]
        if current not in allowed:
            raise ValueError(f"DLL changed outside this transaction; refusing to overwrite: {entry['target']}")
        if entry['original'] is not None and identity(backup / str(i)) != entry['original']:
            raise ValueError(f"missing or modified original DLL backup: {entry['target']}")
    registry = prefix / 'user.reg'
    current_registry = registry.read_text()
    current_values = registry_values(current_registry)
    for name, value in current_values.items():
        allowed = [data['overrides_original'][name], installed_values()[name]]
        if value not in allowed:
            raise ValueError(f'DLL override changed outside this transaction: {name}')
    if identity(backup / 'user.reg') != data['registry_original']:
        raise ValueError('missing or modified registry backup')
    data['state'] = 'restoring'
    journal(prefix, data)
    for i, entry in enumerate(data['files']):
        target = Path(entry['target'])
        if entry['original'] is None:
            target.unlink(missing_ok=True)
        else:
            atomic_copy(backup / str(i), target)
    if digest(registry) == data['registry_installed_sha256'] or identity(registry) == data['registry_original']:
        atomic_copy(backup / 'user.reg', registry)
    else:
        atomic_text(registry, edit_registry(current_registry, data['overrides_original']))
    data['state'] = 'restored'
    journal(prefix, data)
    if backup.exists():
        shutil.rmtree(backup)
    (prefix / RECEIPT).unlink()
    sync_directory(prefix)
    return {'state': 'restored', 'valid': True}


def restore(prefix):
    with locked(prefix):
        return restore_locked(prefix, load_receipt(prefix))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('install', 'status', 'restore'))
    parser.add_argument('prefix', type=Path)
    parser.add_argument('payload', type=Path, nargs='?')
    parser.add_argument('--game-dir', type=Path)
    parser.add_argument('--arch', choices=('x32', 'x64'))
    parser.add_argument('--dll', choices=NAMES, action='append')
    args = parser.parse_args(argv)
    prefix = Path(os.path.abspath(args.prefix))
    try:
        if args.operation == 'install':
            if not args.payload:
                parser.error('install requires a DXVK payload directory')
            report = install(prefix, args.payload.resolve(),
                             Path(os.path.abspath(args.game_dir)) if args.game_dir else None,
                             args.arch, args.dll)
        elif args.operation == 'restore':
            report = restore(prefix)
        else:
            report = status(prefix)
        print(json.dumps(report, indent=2))
        return 0 if report['valid'] else 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'DXVK setup failed: {error}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

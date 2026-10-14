#!/usr/bin/env python3
"""Freeze a committed Mesa source tree for an isolated experimental build."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def export(source, commit, output):
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', '--verify', commit + '^{commit}'], text=True).strip()
    tree = subprocess.check_output(['git', '-C', str(source), 'rev-parse', revision + '^{tree}'], text=True).strip()
    if output.exists():
        raise ValueError('snapshot output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.kk-source-', dir=output.parent)).resolve()
    try:
        raw = temporary / 'mesa.tar'
        subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', '--output=' + str(raw), revision], check=True)
        archive = temporary / 'mesa-source.tar.gz'
        with raw.open('rb') as incoming, archive.open('wb') as outgoing:
            with gzip.GzipFile(filename='', fileobj=outgoing, mode='wb', mtime=0) as compressed:
                shutil.copyfileobj(incoming, compressed)
        raw.unlink()
        data = {'schema': 1, 'commit': revision, 'tree': tree,
                'archive': archive.name, 'archive_sha256': sha(archive)}
        (temporary / 'snapshot.json').write_text(json.dumps(data, indent=2) + '\n')
        temporary.rename(output)
        return data
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify(snapshot):
    data = json.loads((snapshot / 'snapshot.json').read_text())
    if data.get('schema') != 1 or data.get('archive') != 'mesa-source.tar.gz':
        raise ValueError('unsupported snapshot manifest')
    for name, length in (('commit', 40), ('tree', 40), ('archive_sha256', 64)):
        if not re.fullmatch('[0-9a-f]{' + str(length) + '}', data.get(name, '')):
            raise ValueError(f'invalid snapshot {name}')
    archive = snapshot / data['archive']
    if not archive.is_file() or archive.is_symlink() or sha(archive) != data['archive_sha256']:
        raise ValueError('Mesa source archive checksum mismatch')
    with tarfile.open(archive, 'r:gz') as stream:
        if stream.pax_headers.get('comment') != data['commit']:
            raise ValueError('source archive commit differs from its manifest')
    return data


def extract(snapshot, output):
    data = verify(snapshot)
    if output.exists():
        raise ValueError('source output already exists; use a fresh isolated directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.kk-extract-', dir=output.parent)).resolve()
    try:
        with tarfile.open(snapshot / data['archive'], 'r:gz') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
                    raise ValueError('unsafe source archive path')
                target = temporary / path
                if not target.resolve().is_relative_to(temporary):
                    raise ValueError('source archive path escapes its directory')
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as incoming, target.open('wb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                elif member.issym():
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute() or not (target.parent / link).resolve().is_relative_to(temporary):
                        raise ValueError('source archive symlink escapes its directory')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                else:
                    raise ValueError('unsupported source archive member')
        if not (temporary / 'src/kosmickrisp/vulkan/kk_physical_device.c').is_file():
            raise ValueError('snapshot is not a Mesa tree with KosmicKrisp')
        temporary.rename(output)
        return data
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def check_source(snapshot, source):
    data = verify(snapshot)
    source = source.resolve()
    with tarfile.open(snapshot / data['archive'], 'r:gz') as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
                raise ValueError('unsafe source archive path')
            target = source / path
            if member.issym():
                if not target.is_symlink() or str(target.readlink()) != member.linkname:
                    raise ValueError(f'extracted source symlink changed: {path}')
            elif member.isdir():
                if target.is_symlink() or not target.is_dir():
                    raise ValueError(f'extracted source directory changed: {path}')
            elif member.isfile():
                if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(source):
                    raise ValueError(f'extracted source file changed: {path}')
                digest = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                if sha(target) != digest.hexdigest():
                    raise ValueError(f'extracted source content changed: {path}')
            else:
                raise ValueError('unsupported source archive member')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    create = sub.add_parser('export')
    create.add_argument('--source', type=Path, required=True)
    create.add_argument('--commit', required=True)
    create.add_argument('--out', type=Path, required=True)
    check = sub.add_parser('verify')
    check.add_argument('snapshot', type=Path)
    unpack = sub.add_parser('extract')
    unpack.add_argument('snapshot', type=Path)
    unpack.add_argument('out', type=Path)
    existing = sub.add_parser('check-source')
    existing.add_argument('snapshot', type=Path)
    existing.add_argument('source', type=Path)
    args = parser.parse_args()
    if args.operation == 'export':
        result = export(args.source.resolve(), args.commit, args.out.resolve())
    elif args.operation == 'extract':
        result = extract(args.snapshot.resolve(), args.out.resolve())
    elif args.operation == 'check-source':
        result = check_source(args.snapshot.resolve(), args.source.resolve())
    else:
        result = verify(args.snapshot.resolve())
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

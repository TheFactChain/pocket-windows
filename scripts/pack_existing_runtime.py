"""Stream a private local clone; no guest control, credential edits or network calls."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time
import zipfile

PREFIX = 'Windows-Claude/'
DATA_FILES = (
    'images/workvm-empty.qcow2', 'images/gateway-empty.qcow2',
    'state/workvm-vars.fd', 'state/gateway-network-accepted.json',
    'private/gohost-user1/sing-box.json',
)
CHUNK = 4 * 1024 * 1024


def safe_name(name):
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and '..' not in path.parts
            and '\\' not in name and ':' not in name and not name.startswith('/'))


def file_under(root, relative):
    if not safe_name(relative):
        raise ValueError('Unsafe source member path')
    path = root / relative
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError('Source path escapes its root')
    if any(part.is_symlink() or part.is_junction()
           for part in (path, *path.parents) if part != root.parent):
        raise ValueError('Source reparse path refused')
    if not path.is_file():
        raise ValueError('Required source file is missing: ' + relative)
    return path


@contextmanager
def locked_read(path):
    """Windows FILE_SHARE_READ allows readers but denies concurrent writes/deletes."""
    if os.name != 'nt':
        raise RuntimeError('The approved snapshot lock requires Windows')
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        yield stream


def collect(project, source):
    tracked = subprocess.run(
        ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
         '-c', 'safe.directory=' + source.resolve().as_posix(), '-C', str(source),
         'ls-files', '-z'], check=True, capture_output=True).stdout.decode('utf-8')
    entries = {}
    for name in tracked.rstrip('\0').split('\0'):
        if not safe_name(name) or name.split('/')[0] not in {
                '.gitignore', '.gitattributes', 'README.md', 'Start.cmd', 'scripts', 'tests', 'gateway', 'docs'}:
            raise ValueError('Unexpected tracked source path')
        entries[name] = file_under(source, name)
    for name in DATA_FILES:
        if name in entries:
            raise ValueError('Data/source path collision')
        entries[name] = file_under(project, name)
    runtime = project / 'runtime'
    for path in sorted(runtime.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            name = path.relative_to(project).as_posix()
            entries[name] = file_under(project, name)
    if len(entries) == len(DATA_FILES) or not (runtime / 'python-3.12.14/python.exe').is_file():
        raise ValueError('Runtime/source inventory is incomplete')
    names = [name.casefold() for name in entries]
    if len(names) != len(set(names)):
        raise ValueError('Case-insensitive member collision')
    return entries


def write_archive(entries, destination, commit):
    total = sum(path.stat().st_size for path in entries.values())
    done = 0
    last_report = 0
    manifest = {'schema': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'source_repository': 'https://github.com/nekontritce11123-lab/windows-claude-portable',
                'source_commit': commit, 'saved_credentials_included': True,
                'vm_started_for_packaging': False, 'second_pc_tested': False, 'files': []}
    with ExitStack() as locks:
        # Keep live guest state immutable throughout the complete copy.
        held = {name: locks.enter_context(locked_read(path)) for name, path in entries.items()
                if name in DATA_FILES}
        with destination.open('xb') as output, zipfile.ZipFile(
                output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1,
                allowZip64=True) as archive:
            archive.writestr(PREFIX + 'logs/', b'')
            for name, path in sorted(entries.items()):
                before = path.stat()
                digest = hashlib.sha256()
                count = 0
                info = zipfile.ZipInfo.from_file(path, PREFIX + name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info._compresslevel = 1  # Pinned Python 3.12 ZipInfo setting.
                with ExitStack() as context:
                    stream = held.get(name) or context.enter_context(locked_read(path))
                    with archive.open(info, 'w', force_zip64=True) as member:
                        while chunk := stream.read(CHUNK):
                            member.write(chunk)
                            digest.update(chunk)
                            count += len(chunk)
                            done += len(chunk)
                            if time.monotonic() - last_report >= 10:
                                print(json.dumps({'phase': 'copy', 'bytes': done,
                                                  'total_bytes': total, 'member': name}), flush=True)
                                last_report = time.monotonic()
                after = path.stat()
                if count != before.st_size or (after.st_size, after.st_mtime_ns) != (
                        before.st_size, before.st_mtime_ns):
                    raise RuntimeError('Source changed during snapshot: ' + name)
                manifest['files'].append({'path': name, 'bytes': count, 'sha256': digest.hexdigest()})
            manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=True) + '\n').encode()
            archive.writestr(PREFIX + 'MANIFEST.json', manifest_bytes)
            checksums = ''.join(row['sha256'] + '  ' + row['path'] + '\n' for row in manifest['files'])
            checksums += hashlib.sha256(manifest_bytes).hexdigest() + '  MANIFEST.json\n'
            archive.writestr(PREFIX + 'MANIFEST.sha256', checksums.encode('utf-8'))
    return manifest


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(name.casefold() for name in names)) or any(
                not safe_name(name) or not name.startswith(PREFIX) for name in names):
            raise ValueError('Unsafe or duplicate archive members')
        manifest_bytes = archive.read(PREFIX + 'MANIFEST.json')
        manifest = json.loads(manifest_bytes)
        expected = {PREFIX + row['path'] for row in manifest['files']}
        expected.update({PREFIX + 'logs/', PREFIX + 'MANIFEST.json', PREFIX + 'MANIFEST.sha256'})
        if set(names) != expected or len(manifest['files']) + 3 != len(names):
            raise ValueError('Archive member set differs from its manifest')
        checksums = ''.join(row['sha256'] + '  ' + row['path'] + '\n' for row in manifest['files'])
        checksums += hashlib.sha256(manifest_bytes).hexdigest() + '  MANIFEST.json\n'
        if archive.read(PREFIX + 'MANIFEST.sha256') != checksums.encode('utf-8'):
            raise ValueError('Checksum manifest mismatch')
        done = 0
        last_report = 0
        for row in manifest['files']:
            digest = hashlib.sha256()
            size = 0
            with archive.open(PREFIX + row['path']) as member:
                while chunk := member.read(CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    done += len(chunk)
                    if time.monotonic() - last_report >= 10:
                        print(json.dumps({'phase': 'verify', 'bytes': done,
                                          'member': row['path']}), flush=True)
                        last_report = time.monotonic()
            if digest.hexdigest() != row['sha256'] or size != row['bytes']:
                raise ValueError('Member hash/size mismatch: ' + row['path'])
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--source-repo', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{40}', args.commit):
        raise ValueError('An exact verified source commit is required')
    project = args.project.resolve(strict=True)
    source = args.source_repo.resolve(strict=True)
    destination = args.output.resolve()
    if destination.exists() or destination.with_suffix(destination.suffix + '.partial').exists():
        raise FileExistsError('Output already exists; no file is overwritten')
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = collect(project, source)
    total = sum(path.stat().st_size for path in entries.values())
    if shutil.disk_usage(destination.parent).free < total + 2 * 1024 ** 3:
        raise RuntimeError('Insufficient free space for a conservative local ZIP bound')
    partial = destination.with_suffix(destination.suffix + '.partial')
    manifest = write_archive(entries, partial, args.commit)
    verified = verify_archive(partial)
    if verified != manifest:
        raise ValueError('Archive manifest differs from the generated manifest')
    os.rename(partial, destination)
    with destination.open('rb') as stream:
        archive_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    with destination.with_suffix(destination.suffix + '.sha256').open('x', encoding='utf-8') as stream:
        stream.write(archive_sha256 + '  ' + destination.name + '\n')
    summary = {'zip': str(destination), 'zip_bytes': destination.stat().st_size,
               'zip_sha256': archive_sha256, 'payload_bytes': total,
               'payload_files': len(entries), 'crc_and_sha256_verified': True,
               'source_commit': args.commit, 'saved_credentials_included': True,
               'second_pc_tested': False, 'vm_started_for_packaging': False}
    with destination.with_suffix(destination.suffix + '.manifest.json').open('x', encoding='utf-8') as stream:
        json.dump({'summary': summary, 'manifest': manifest}, stream, indent=2)
        stream.write('\n')
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()

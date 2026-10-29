"""Install approved QEMU executables while preserving the component repair contract.

Candidates must match the pinned engine and qemu-img hashes. Both existing executables,
the manifest and recovery ZIP are backed up before replacing any outdated components.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_runtime as runtime
import prepare_runtime_bundle as bundle
from file_snapshot import QEMU_IMG_SHA256

ENGINE = bundle.QEMU + '/qemu-system-x86_64.exe'
IMG = bundle.QEMU + '/qemu-img.exe'
ENGINE_SHA256 = '93b16a220fdc7b6125ecd63758fa8cd45f0ec8329359ad27aa796325445f7738'
MAX_ENGINE_BYTES = 256 * 1024 * 1024


def update_project(project, backup_root=None, *, engine_source=None, qemu_img_source=None):
    project = bundle._plain(Path(os.path.abspath(project)))
    candidates = {}
    for name, supplied, digest in ((ENGINE, engine_source, ENGINE_SHA256), (IMG, qemu_img_source, QEMU_IMG_SHA256)):
        if supplied is None:
            continue
        source = bundle._plain(Path(supplied))
        if not source.is_file() or not 2 <= source.stat().st_size <= MAX_ENGINE_BYTES:
            raise ValueError('The approved QEMU candidate must be an MZ file no larger than 256 MiB')
        with source.open('rb') as stream:
            candidate = stream.read(MAX_ENGINE_BYTES + 1)
        bundle._plain(source)
        if (len(candidate) > MAX_ENGINE_BYTES or not candidate.startswith(b'MZ')
                or hashlib.sha256(candidate).hexdigest() != digest):
            raise ValueError('The QEMU candidate does not match the approved ' + Path(name).name)
        candidates[name] = candidate
    # Same guard as packaging: a running engine cannot be replaced, and the ZIP/manifest must not
    # change under a launcher operation.
    with runtime.ProjectLock(project) as acquired:
        if not acquired or runtime.engine_processes(project):
            raise RuntimeError('Shut down all portable VMs before patching QEMU')
        return _update(project, backup_root, candidates)


def _update(project, backup_root, candidates, *, approved=None):
    records = bundle._manifest(project)
    manifest_path, recovery_path = project / bundle.MANIFEST, project / bundle.RECOVERY
    approved = {ENGINE: ENGINE_SHA256, IMG: QEMU_IMG_SHA256} if approved is None else dict(approved)
    if set(approved) != {ENGINE, IMG} or any(not isinstance(value, str) or len(value) != 64
            or any(letter not in '0123456789abcdef' for letter in value) for value in approved.values()):
        raise ValueError('Exactly two approved QEMU hashes are required')
    for name in approved:
        record = records.get(name.casefold())
        data = bundle._plain(project / name).read_bytes()
        if (record is None or record['path'] != name or len(data) != record['bytes']
                or hashlib.sha256(data).hexdigest() != record['sha256']):
            raise ValueError('The QEMU component differs from its manifest; repair components first')
    paths = (recovery_path, manifest_path, *(project / name for name in approved))
    staged = {path: path.with_name(path.name + '.patching') for path in paths}
    for path in staged.values():
        if path.exists():
            raise FileExistsError('A previous patch attempt left ' + path.name + '; inspect it first')
    changed = [name for name, digest in approved.items() if records[name.casefold()]['sha256'] != digest]
    if not changed:
        return {'changed': False, 'sha256': approved[ENGINE], 'qemu_img_sha256': approved[IMG]}
    for name in changed:
        if name not in candidates:
            raise ValueError('An approved QEMU candidate is required; supply ' + ('--engine' if name == ENGINE else '--qemu-img'))
    rows = json.loads(manifest_path.read_text(encoding='utf-8-sig'))['files']
    for row in rows:
        if row['path'] in changed:
            row['bytes'] = len(candidates[row['path']])
            row['sha256'] = approved[row['path']]
    expected = {row['path']: row for row in rows}

    bundle._plain(recovery_path)
    try:
        with zipfile.ZipFile(recovery_path) as source, zipfile.ZipFile(staged[recovery_path], 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as target:
            if sorted(source.namelist()) != sorted(expected):
                raise ValueError('Recovery ZIP member set differs from the components manifest')
            for info in source.infolist():
                # A fresh ZipInfo: writing updates offsets in place, which would corrupt reading the source.
                member = zipfile.ZipInfo(info.filename, info.date_time)
                member.compress_type, member.external_attr = zipfile.ZIP_DEFLATED, info.external_attr
                with target.open(member, 'w', force_zip64=True) as stream:
                    if info.filename in changed:
                        stream.write(candidates[info.filename])
                    else:
                        with source.open(info) as content:
                            shutil.copyfileobj(content, stream, 1024 * 1024)
        with zipfile.ZipFile(staged[recovery_path]) as archive:
            for name, row in expected.items():
                with archive.open(name) as stream:
                    if archive.getinfo(name).file_size != row['bytes'] or hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                        raise ValueError('Rebuilt recovery ZIP does not match the new manifest')
        with staged[manifest_path].open('x', encoding='utf-8', newline='\n') as stream:
            json.dump({'schema': 1, 'files': rows}, stream, separators=(',', ':'))
            stream.write('\n')
        for name in changed:
            with staged[project / name].open('xb') as stream:
                stream.write(candidates[name])
        backup = Path(backup_root or project / 'state') / ('qemu-engine-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        backup.mkdir(parents=True)
        for path in paths:
            shutil.copy2(path, backup / path.name)
        # Before the manifest changes, rerun with candidates; afterwards repair restores both EXEs from the ZIP.
        for path in (recovery_path, manifest_path, *(project / name for name in changed)):
            os.replace(staged[path], path)
    except BaseException:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    written = bundle._manifest(project)
    if any(written[name.casefold()]['sha256'] != bundle._sha(project / name) for name in approved):
        raise ValueError('Patched QEMU components do not match the written manifest')
    return {'changed': True, 'sha256': approved[ENGINE], 'qemu_img_sha256': approved[IMG],
            'previous_sha256': records[ENGINE.casefold()]['sha256'],
            'previous_qemu_img_sha256': records[IMG.casefold()]['sha256'], 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--engine', type=Path, help='Approved rebuilt qemu-system-x86_64.exe')
    parser.add_argument('--qemu-img', type=Path, help='Approved rebuilt qemu-img.exe')
    args = parser.parse_args()
    print(json.dumps(update_project(args.project, engine_source=args.engine, qemu_img_source=args.qemu_img), indent=2))


if __name__ == '__main__':
    main()

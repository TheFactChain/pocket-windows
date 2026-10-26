"""Install an approved rebuilt QEMU engine while preserving the component repair contract.

The candidate must match ENGINE_SHA256. The previous engine, manifest and recovery ZIP
are backed up before replacement; an already approved engine needs no candidate.
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

ENGINE = bundle.QEMU + '/qemu-system-x86_64.exe'
ENGINE_SHA256 = '63dc4e2f5fc131afe5ad2416c2d9627f26ae764eec3a03a86acb41eae71b2b2a'
MAX_ENGINE_BYTES = 256 * 1024 * 1024


def update_project(project, backup_root=None, *, engine_source=None):
    project = bundle._plain(Path(os.path.abspath(project)))
    candidate = None
    if engine_source is not None:
        source = bundle._plain(Path(engine_source))
        if not source.is_file() or not 2 <= source.stat().st_size <= MAX_ENGINE_BYTES:
            raise ValueError('The approved QEMU candidate must be an MZ file no larger than 256 MiB')
        with source.open('rb') as stream:
            candidate = stream.read(MAX_ENGINE_BYTES + 1)
        bundle._plain(source)
        if (len(candidate) > MAX_ENGINE_BYTES or not candidate.startswith(b'MZ')
                or hashlib.sha256(candidate).hexdigest() != ENGINE_SHA256):
            raise ValueError('The QEMU candidate does not match the approved engine')
    # Same guard as packaging: a running engine cannot be replaced, and the ZIP/manifest must not
    # change under a launcher operation.
    with runtime.ProjectLock(project) as acquired:
        if not acquired or runtime.engine_processes(project):
            raise RuntimeError('Shut down all portable VMs before patching QEMU')
        return _update(project, backup_root, candidate)


def _update(project, backup_root, candidate):
    records = bundle._manifest(project)
    manifest_path, recovery_path, engine_path = project / bundle.MANIFEST, project / bundle.RECOVERY, project / ENGINE
    record = records.get(ENGINE.casefold())
    data = bundle._plain(engine_path).read_bytes()
    if (record is None or record['path'] != ENGINE or len(data) != record['bytes']
            or hashlib.sha256(data).hexdigest() != record['sha256']):
        raise ValueError('The QEMU engine differs from its manifest; repair components first')
    staged = [path.with_name(path.name + '.patching') for path in (recovery_path, manifest_path, engine_path)]
    for path in staged:
        if path.exists():
            raise FileExistsError('A previous patch attempt left ' + path.name + '; inspect it first')
    if record['sha256'] == ENGINE_SHA256:
        return {'changed': False, 'sha256': record['sha256']}
    if candidate is None:
        raise ValueError('An approved rebuilt QEMU engine is required; supply --engine')
    digest = ENGINE_SHA256
    rows = json.loads(manifest_path.read_text(encoding='utf-8-sig'))['files']
    for row in rows:
        if row['path'] == ENGINE:
            row['bytes'] = len(candidate)
            row['sha256'] = digest
    expected = {row['path']: row['sha256'] for row in rows}

    bundle._plain(recovery_path)
    try:
        with zipfile.ZipFile(recovery_path) as source, zipfile.ZipFile(staged[0], 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as target:
            if sorted(source.namelist()) != sorted(expected):
                raise ValueError('Recovery ZIP member set differs from the components manifest')
            for info in source.infolist():
                # A fresh ZipInfo: writing updates offsets in place, which would corrupt reading the source.
                member = zipfile.ZipInfo(info.filename, info.date_time)
                member.compress_type, member.external_attr = zipfile.ZIP_DEFLATED, info.external_attr
                with target.open(member, 'w', force_zip64=True) as stream:
                    if info.filename == ENGINE:
                        stream.write(candidate)
                    else:
                        with source.open(info) as content:
                            shutil.copyfileobj(content, stream, 1024 * 1024)
        with zipfile.ZipFile(staged[0]) as archive:
            for name, sha in expected.items():
                with archive.open(name) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                        raise ValueError('Rebuilt recovery ZIP does not match the new manifest')
        with staged[1].open('x', encoding='utf-8', newline='\n') as stream:
            json.dump({'schema': 1, 'files': rows}, stream, separators=(',', ':'))
            stream.write('\n')
        with staged[2].open('xb') as stream:
            stream.write(candidate)
        backup = Path(backup_root or project / 'state') / ('qemu-engine-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(parents=True)
        for path in (recovery_path, manifest_path, engine_path):
            shutil.copy2(path, backup / path.name)
        # Interrupted after the ZIP: manifest and engine still agree, a rerun completes the patch.
        # Interrupted after the manifest: component repair restores the patched engine from the ZIP.
        for new, path in zip(staged, (recovery_path, manifest_path, engine_path)):
            os.replace(new, path)
    except BaseException:
        for path in staged:
            path.unlink(missing_ok=True)
        raise
    if bundle._manifest(project)[ENGINE.casefold()]['sha256'] != bundle._sha(engine_path):
        raise ValueError('Patched engine does not match the written manifest')
    return {'changed': True, 'sha256': digest, 'previous_sha256': record['sha256'], 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--engine', type=Path, help='Approved rebuilt qemu-system-x86_64.exe')
    args = parser.parse_args()
    print(json.dumps(update_project(args.project, engine_source=args.engine), indent=2))


if __name__ == '__main__':
    main()

"""Patch qemu-system-x86_64.exe 11.1.0 in place and keep the component repair contract intact.

1. Fixed window title. SDL formats its caption as "QEMU (<name>-<n>)<status>" and appends
   " - Press Ctrl-Alt-G to exit grab" on every mouse grab; the format becomes the literal "Windows Claude".
2. Boot freeze. When the guest enables the USB tablet, QEMU's mouse-mode notifier runs on a vCPU
   thread holding the big QEMU lock and grabs the mouse; the grab ends in SDL_SetWindowTitle, a
   SendMessage to the window's main thread, which is itself waiting for that lock (stacks in Beads
   wcp-5nb.3). With a fixed title the caption update is unnecessary, so it is skipped on both
   notifier paths: the tail jump in sdl_grab_start and the inlined sdl_grab_end call.

These three byte replacements are the only changes to the GPL binary; the source is upstream
QEMU 11.1.0 (runtime/qemu-11.1.0/COPYING). Each original sequence must occur exactly once.
The engine, its manifest record and its copy in the recovery ZIP are written together; the
previous three files are kept in a backup directory.
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
PATCHES = (
    ('title format', b'\0QEMU (%s-%d)%s\0', b'\0Windows Claude\0'),
    # sdl_grab_start: add rsp,20; pop rbx; jmp sdl_update_caption  ->  ...; ret
    ('grab start caption', bytes.fromhex('48 83 c4 20 5b e9 6b f8 ff ff'), bytes.fromhex('48 83 c4 20 5b c3 cc cc cc cc')),
    # sdl_mouse_mode_change (inlined sdl_grab_end): mov rcx,rbx; call sdl_update_caption; jmp  ->  5-byte nop
    ('grab end caption', bytes.fromhex('48 89 d9 e8 dc f0 ff ff eb 90'), bytes.fromhex('48 89 d9 0f 1f 44 00 00 eb 90')),
)


def patch(data):
    """Return the patched engine; already applied patches are kept, anything ambiguous is refused."""
    for name, original, replacement in PATCHES:
        found, done = data.count(original), data.count(replacement)
        if found == 1 and done == 0:
            data = data.replace(original, replacement)
        elif not (found == 0 and done == 1):
            raise ValueError('QEMU patch site "' + name + '" not found exactly once; unknown engine build')
    return data


def update_project(project, backup_root=None):
    project = bundle._plain(Path(os.path.abspath(project)))
    # Same guard as packaging: a running engine cannot be replaced, and the ZIP/manifest must not
    # change under a launcher operation.
    with runtime.ProjectLock(project) as acquired:
        if not acquired or runtime.engine_processes(project):
            raise RuntimeError('Shut down all portable VMs before patching QEMU')
        return _update(project, backup_root)


def _update(project, backup_root):
    records = bundle._manifest(project)
    manifest_path, recovery_path, engine_path = project / bundle.MANIFEST, project / bundle.RECOVERY, project / ENGINE
    record = records.get(ENGINE.casefold())
    data = engine_path.read_bytes()
    if record is None or len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('The QEMU engine differs from its manifest; repair components first')
    patched = patch(data)
    if patched == data:
        return {'changed': False, 'sha256': record['sha256']}
    digest = hashlib.sha256(patched).hexdigest()
    rows = json.loads(manifest_path.read_text(encoding='utf-8-sig'))['files']
    for row in rows:
        if row['path'] == ENGINE:
            row['sha256'] = digest
    expected = {row['path']: row['sha256'] for row in rows}

    staged = [path.with_name(path.name + '.patching') for path in (recovery_path, manifest_path, engine_path)]
    for path in staged:
        if path.exists():
            raise FileExistsError('A previous patch attempt left ' + path.name + '; inspect it first')
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
                        stream.write(patched)
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
            stream.write(patched)
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
    args = parser.parse_args()
    print(json.dumps(update_project(args.project), indent=2))


if __name__ == '__main__':
    main()

"""Prepare and adopt this project's two UTF-8 QEMU EXEs with matching source pins.

prepare writes only a new, separate work directory. adopt holds the project's runtime
lock through replacement, validation and any rollback. An uncatchable interruption
requires the explicit recover command; this is not a filesystem-wide atomic commit.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_runtime as runtime
import patch_qemu_engine as patch
import prepare_runtime_bundle as bundle
from pack_existing_runtime import _validate_component_recovery
from profile_store import _unique_keys

OLD_ENGINE_SHA256 = '63dc4e2f5fc131afe5ad2416c2d9627f26ae764eec3a03a86acb41eae71b2b2a'
NEW_ENGINE_SHA256 = '93b16a220fdc7b6125ecd63758fa8cd45f0ec8329359ad27aa796325445f7738'
OLD_IMG_SHA256 = '9d930582d0d574bb106d89982c547688e10b8f356192d302cc3d0ae4e8674534'
NEW_IMG_SHA256 = 'e28ccc80467af96433a044fcae7541d797904bc2e3afd3eb64aabe578896e73b'
ENGINE_SOURCES = ('scripts/portable_app.py', 'scripts/control_guest_install.py',
                  'scripts/reference_vm.py', 'scripts/patch_qemu_engine.py')
IMG_SOURCE = 'scripts/file_snapshot.py'
RUNTIME_FILES = (bundle.RECOVERY, bundle.MANIFEST, patch.ENGINE, patch.IMG)
FILES = (*RUNTIME_FILES, *ENGINE_SOURCES, IMG_SOURCE)
JOURNAL = 'journal.json'
SERVICE_RESERVE_BYTES = 16 * 1024 * 1024  # Journal updates and directory metadata, beyond both full copies.


def _project_processes(project):
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                    ('name', wintypes.WCHAR * 260)]
    kernel = runtime.kernel_api()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        function.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    names = {'qemu-system-x86_64.exe', 'qemu-img.exe', 'python.exe', 'launcher.exe', 'hostfiles.exe', 'hostfilesguest.exe'}
    found = []
    try:
        entry = Entry(); entry.size = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while success:
            if entry.pid != os.getpid() and entry.name.casefold() in names:
                identity = runtime.process_identity(entry.pid)
                if identity is None:
                    raise RuntimeError('Cannot verify that a possible project process is stopped')
                if Path(identity['image']).resolve().is_relative_to(project):
                    found.append(identity)
            success = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    return found


def _paths(project, work):
    project = bundle._plain(Path(project).absolute()).resolve(strict=True)
    work = bundle._plain(Path(work).absolute(), missing=True)
    work = bundle._plain(work.resolve(strict=False), missing=True)
    if not project.is_dir() or work.is_relative_to(project) or project.is_relative_to(work):
        raise ValueError('Use a separate work directory outside the source project')
    parent = bundle._plain(work.parent)
    if parent.stat().st_dev != project.stat().st_dev:
        raise ValueError('Project and work must be on the same volume')
    return project, work


def _sha(path):
    return bundle._sha(bundle._plain(path))


def _copy(source, destination):
    source = bundle._plain(source)
    bundle._plain(destination.parent, missing=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bundle._plain(destination, missing=True)
    shutil.copy2(source, destination)


def _snapshot(root):
    return {name: {'sha256': _sha(root / name), 'bytes': (root / name).stat().st_size} for name in FILES}


def _candidate(path, expected):
    path = bundle._plain(Path(path))
    if not path.is_file() or not 2 <= path.stat().st_size <= patch.MAX_ENGINE_BYTES:
        raise ValueError('A bounded MZ candidate is required')
    with path.open('rb') as stream:
        data = stream.read(patch.MAX_ENGINE_BYTES + 1)
    bundle._plain(path)
    if (len(data) > patch.MAX_ENGINE_BYTES or not data.startswith(b'MZ')
            or hashlib.sha256(data).hexdigest() != expected):
        raise ValueError('QEMU candidate does not match its fixed UTF-8 hash')
    return data


def _source_updates(project, snapshot):
    updates, current = {}, {}
    for name in (*ENGINE_SOURCES, IMG_SOURCE):
        path = bundle._plain(project / name)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Source pin file exceeds its size limit')
        data = path.read_bytes()
        if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != snapshot[name]['sha256']:
            raise ValueError('Source changed during preparation')
        symbol, old, new = ('QEMU_IMG_SHA256', OLD_IMG_SHA256, NEW_IMG_SHA256) if name == IMG_SOURCE else ('ENGINE_SHA256', OLD_ENGINE_SHA256, NEW_ENGINE_SHA256)
        matches = list(re.finditer(rb'(?m)^[ \t]*' + symbol.encode('ascii') + rb'[ \t]*=[ \t]*([\'\"])([0-9a-f]{64})\1[ \t]*(?:#[^\r\n]*)?\r?$', data))
        assignments = re.findall(rb'(?m)^[ \t]*' + symbol.encode('ascii') + rb'[ \t]*=', data)
        if len(matches) != 1 or len(assignments) != 1 or matches[0][2].decode('ascii') not in (old, new):
            raise ValueError('Expected one known strict source pin in ' + name)
        match = matches[0]
        current[name] = match[2].decode('ascii')
        updates[name] = data[:match.start(2)] + new.encode('ascii') + data[match.end(2):]
    return updates, current


def _runtime_contract(root, expected=None):
    records = bundle._manifest(root)
    for name, allowed in ((patch.ENGINE, (OLD_ENGINE_SHA256, NEW_ENGINE_SHA256)),
                          (patch.IMG, (OLD_IMG_SHA256, NEW_IMG_SHA256))):
        record = records.get(name.casefold())
        digest = _sha(root / name)
        if (record is None or record['path'] != name or record['sha256'] != digest
                or record['bytes'] != (root / name).stat().st_size
                or digest not in allowed or (expected is not None and digest != expected[name])):
            raise ValueError('Runtime does not match a known coherent QEMU manifest')
    with bundle._plain(root / bundle.RECOVERY).open('rb') as stream:
        _validate_component_recovery(stream, list(records.values()))
    return records


def _journal(work, plan, state, replaced=()):
    value = dict(plan, state=state, replaced=list(replaced))
    temporary = work / ('journal-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, work / JOURNAL)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def prepare(project, work, *, engine_source, qemu_img_source):
    """Read the project and prepare all nine replacements only under a new work path."""
    project, work = _paths(project, work)
    if work.exists():
        raise FileExistsError('Use a new work directory; preserve previous journals and backups')
    candidates = {patch.ENGINE: _candidate(engine_source, NEW_ENGINE_SHA256),
                  patch.IMG: _candidate(qemu_img_source, NEW_IMG_SHA256)}
    before = _snapshot(project)
    sources, pins = _source_updates(project, before)
    records = _runtime_contract(project)
    if (any(pins[name] != records[patch.ENGINE.casefold()]['sha256'] for name in ENGINE_SOURCES)
            or pins[IMG_SOURCE] != records[patch.IMG.casefold()]['sha256']):
        raise ValueError('Source pins and current QEMU must agree before preparing adoption')
    if _project_processes(project):
        raise RuntimeError('Stop project VMs before preparing QEMU adoption')
    work.mkdir()
    stage = work / 'stage'
    for name in RUNTIME_FILES:
        destination = stage / name
        _copy(project / name, destination)
    approved = {patch.ENGINE: NEW_ENGINE_SHA256, patch.IMG: NEW_IMG_SHA256}
    patch._update(stage, work / 'stage-backup', candidates, approved=approved)
    for name, data in sources.items():
        destination = stage / name
        bundle._plain(destination.parent, missing=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        bundle._plain(destination, missing=True)
        destination.write_bytes(data)
    if _snapshot(project) != before:
        raise ValueError('Project changed during preparation; prepare again in a new work directory')
    after = _snapshot(stage)
    plan = {'schema': 1, 'operation': 'adopt-qemu-utf8', 'project': str(project),
            'before': before, 'after': after}
    _check_stage(project, work, plan)
    return _journal(work, plan, 'prepared')


def _load(project, work):
    project, work = _paths(project, work)
    path = bundle._plain(work / JOURNAL)
    if path.stat().st_size > 65536:
        raise ValueError('Invalid adoption journal')
    plan = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
    if (type(plan) is not dict or set(plan) != {'schema', 'operation', 'project', 'before', 'after', 'state', 'replaced'}
            or type(plan['schema']) is not int or plan['schema'] != 1 or plan['operation'] != 'adopt-qemu-utf8'
            or type(plan['project']) is not str or os.path.normcase(plan['project']) != os.path.normcase(str(project))
            or type(plan['replaced']) is not list or any(name not in FILES for name in plan['replaced'])):
        raise ValueError('Invalid adoption journal')
    for phase in ('before', 'after'):
        rows = plan[phase]
        if type(rows) is not dict or set(rows) != set(FILES):
            raise ValueError('Invalid adoption journal file set')
        for row in rows.values():
            if (type(row) is not dict or set(row) != {'sha256', 'bytes'} or type(row['bytes']) is not int or row['bytes'] < 0
                    or type(row['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', row['sha256'])):
                raise ValueError('Invalid adoption journal hashes')
    return project, work, plan


def _check_stage(project, work, plan):
    if _snapshot(project) != plan['before']:
        raise ValueError('Project changed since preparation; no files were adopted')
    stage = work / 'stage'
    if _snapshot(stage) != plan['after']:
        raise ValueError('Prepared files changed; no files were adopted')
    sources, unused = _source_updates(project, plan['before'])
    for name, data in sources.items():
        if (stage / name).read_bytes() != data:
            raise ValueError('Prepared source change exceeds the pin literals')
    approved = {patch.ENGINE: NEW_ENGINE_SHA256, patch.IMG: NEW_IMG_SHA256}
    staged = _runtime_contract(stage, approved)
    expected = bundle._manifest(project)
    for name, digest in approved.items():
        expected[name.casefold()] = {'path': name, 'sha256': digest, 'bytes': (stage / name).stat().st_size}
    if staged != expected:
        raise ValueError('Prepared runtime manifest changes unrelated components')


def _restore(project, work, plan):
    backup = work / 'backup'
    if _snapshot(backup) != plan['before']:
        raise ValueError('Backup changed; explicit investigation is required')
    rollback = work / 'rollback'
    for name in FILES:
        replacement = rollback / name
        _copy(backup / name, replacement)
    for name in FILES:
        bundle._plain(project / name)
        os.replace(bundle._plain(rollback / name), project / name)
    if _snapshot(project) != plan['before']:
        raise RuntimeError('Rollback could not be verified; keep the project stopped')


def adopt(project, work):
    """Replace the nine prepared files, rolling all nine back on a caught failure."""
    project, work, plan = _load(project, work)
    if plan['state'] != 'prepared':
        raise ValueError('Only an untouched prepared operation can be adopted')
    with runtime.ProjectLock(project) as acquired:
        if not acquired or _project_processes(project):
            raise RuntimeError('Stop project applications and VMs before adopting QEMU')
        _check_stage(project, work, plan)
        required = 2 * sum(row['bytes'] for row in plan['before'].values()) + SERVICE_RESERVE_BYTES
        if shutil.disk_usage(work).free < required:
            raise OSError('Not enough free space for complete QEMU backup and rollback: ' + str(required) + ' bytes required')
        backup = work / 'backup'
        backup.mkdir()
        for name in FILES:
            destination = backup / name
            _copy(project / name, destination)
        if _snapshot(backup) != plan['before'] or _snapshot(project) != plan['before']:
            raise ValueError('Project changed before replacement; no files were adopted')
        _journal(work, plan, 'committing')
        replaced = []
        try:
            for name in FILES:
                bundle._plain(project / name)
                os.replace(bundle._plain(work / 'stage' / name), project / name)
                replaced.append(name)
                _journal(work, plan, 'committing', replaced)
            if _snapshot(project) != plan['after']:
                raise ValueError('Installed files do not match the prepared operation')
            _runtime_contract(project, {patch.ENGINE: NEW_ENGINE_SHA256, patch.IMG: NEW_IMG_SHA256})
            return _journal(work, plan, 'committed', replaced)
        except BaseException:
            try:
                _restore(project, work, plan)
                _journal(work, plan, 'rolled_back', replaced)
            except BaseException as error:
                _journal(work, plan, 'rollback_failed', replaced)
                raise RuntimeError('Rollback was not confirmed; keep the project stopped and use explicit recovery') from error
            raise


def recover(project, work):
    """Explicitly restore an interrupted operation from its preserved full backup."""
    project, work, plan = _load(project, work)
    if plan['state'] not in ('committing', 'rollback_failed'):
        raise ValueError('This journal does not describe an interrupted adoption')
    with runtime.ProjectLock(project) as acquired:
        if not acquired or _project_processes(project):
            raise RuntimeError('Stop project applications and VMs before recovering QEMU adoption')
        current = _snapshot(project)
        if any(current[name] not in (plan['before'][name], plan['after'][name]) for name in FILES):
            raise ValueError('Files changed outside this operation; recovery refused')
        backup = work / 'backup'
        original = _runtime_contract(backup)
        present = bundle._manifest(project)
        if {key: row for key, row in original.items() if key not in (patch.ENGINE.casefold(), patch.IMG.casefold())} != {
                key: row for key, row in present.items() if key not in (patch.ENGINE.casefold(), patch.IMG.casefold())}:
            raise ValueError('Recovery backup changes unrelated runtime components')
        sources, unused = _source_updates(backup, plan['before'])
        for name, updated in sources.items():
            if (project / name).read_bytes() not in ((backup / name).read_bytes(), updated):
                raise ValueError('Recovery backup changes more than the source pin literals')
        _restore(project, work, plan)
        return _journal(work, plan, 'recovered', plan['replaced'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare', 'adopt', 'recover'):
        command = commands.add_parser(action)
        command.add_argument('--project', type=Path, required=True)
        command.add_argument('--work', type=Path, required=True)
        if action == 'prepare':
            command.add_argument('--engine', type=Path, required=True)
            command.add_argument('--qemu-img', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.project, args.work, engine_source=args.engine, qemu_img_source=args.qemu_img)
    elif args.action == 'adopt':
        result = adopt(args.project, args.work)
    else:
        result = recover(args.project, args.work)
    print(json.dumps({'state': result['state'], 'work': str(args.work)}, ensure_ascii=False))


if __name__ == '__main__':
    main()

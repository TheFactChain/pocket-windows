"""Build one committed release and update application files without replacing user data."""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pack_existing_runtime as pack
import prepare_runtime_bundle as bundle
import launch_runtime as runtime
from file_snapshot import locked_read, remove_owned_file
from profile_store import _plain, _pinned_directory, _exclusive_file

INFO = 'release-info.json'
JOURNAL = 'state/app-update.json'
INVENTORY = 'state/application-files.json'
JSON_LIMIT = 16 * 1024 * 1024
REQUIRED_RUNTIME = ('runtime/python-3.12.14/python.exe',
                    'runtime/qemu-11.1.0/qemu-system-x86_64.exe',
                    'runtime/qemu-11.1.0/qemu-img.exe',
                    'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd')
MASTER_HASHES = {'work_sha256': 'fc5f18a8342ed7575dd028c36208c06a803ea0100fe44ee37e5bbdd4393f65b5',
                 'vars_sha256': '73696892959e012e0a9b74073bdafa6a1cfbe4dc1d871195447cca6d6ce11214'}
validate_release_info = pack.validate_release_info


def absolute(path):
    return Path(os.path.abspath(path))


def directory(path):
    path = absolute(path)
    for item in (*reversed(path.parents), path):
        _plain(item)
        if not item.exists():
            with _pinned_directory(item.parent, create=False):
                item.mkdir()
        if not item.is_dir():
            raise ValueError('Expected a plain directory')
    return path


def read_json(path, limit=16 * 1024 * 1024):
    with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
        return pack._json(stream, limit)


def identity(path):
    info = path.stat()
    return {'device': info.st_dev, 'file_id': info.st_ino}


def json_bytes(value):
    data = (json.dumps(value, ensure_ascii=True, indent=2) + '\n').encode('utf-8')
    if len(data) > JSON_LIMIT:
        raise ValueError('Release/update JSON exceeds its readable size limit')
    return data


def atomic_json(path, value):
    data = json_bytes(value)
    with _pinned_directory(path.parent, create=False):
        _plain(path)
        temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        owned = identity(temporary)
        try:
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                remove_owned_file(temporary, owned)


def file_record(path, name):
    _plain(path)
    with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
        info = os.fstat(stream.fileno())
        return {'path': name, 'bytes': info.st_size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def copy_record(source, destination, expected):
    directory(destination.parent)
    with _pinned_directory(source.parent, create=False), _pinned_directory(destination.parent, create=False):
        _plain(source)
        _plain(destination)
        digest, size = hashlib.sha256(), 0
        with locked_read(source) as incoming, destination.open('xb') as outgoing:
            while chunk := incoming.read(4 * 1024 * 1024):
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if size != expected['bytes'] or digest.hexdigest() != expected['sha256']:
            raise ValueError('Source changed or differs from release: ' + expected['path'])


def common_name(name):
    return pack.application_path(name)


def transaction_name(name):
    return common_name(name) or (pack.safe_name(name) and (name in (INVENTORY,
        'gateway/packages/current.json', 'gateway/packages/update.json')
        or bool(re.fullmatch(r'gateway/packages/releases/[a-z0-9][a-z0-9._-]{0,63}/(manifest.json|gateway.qcow2)', name))))


def validate_common_records(records, transaction=False):
    if type(records) is not list or not 1 <= len(records) <= 40000:
        raise ValueError('Invalid application inventory')
    seen = set()
    for row in records:
        if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'}
                or not (transaction_name(row['path']) if transaction else common_name(row['path']))
                or row['path'].casefold() in seen or type(row['bytes']) is not int or row['bytes'] < 0
                or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise ValueError('Unsafe or invalid application record')
        seen.add(row['path'].casefold())
    return records


def common_records(root, declared=None):
    root = absolute(root)
    manifest = read_json(pack.file_under(root, pack.COMPONENT_FILES[0]))
    if type(manifest) is not dict or set(manifest) != {'schema', 'files'} or type(manifest['schema']) is not int or manifest['schema'] != 1:
        raise ValueError('Invalid runtime manifest')
    component_records = validate_common_records(manifest['files'])
    if any(not pack.runtime_path(row['path']) for row in component_records):
        raise ValueError('Runtime manifest includes non-runtime data')
    names = set(declared) if declared is not None else {'Launcher.exe', INFO, *pack.APP_FILES, *pack.COMPONENT_FILES}
    required = {'Launcher.exe', INFO, 'scripts/portable_app.py', 'scripts/launch_runtime.py',
                'scripts/release_portable.py', 'scripts/pack_existing_runtime.py', *pack.COMPONENT_FILES}
    if not required <= names:
        raise ValueError('Incomplete application payload')
    names.update(row['path'] for row in component_records)
    suffixes = ('/python.exe', '/qemu-system-x86_64.exe', '/qemu-img.exe', '/share/edk2-x86_64-code.fd')
    if any(not any(row['path'].endswith(suffix) for row in component_records) for suffix in suffixes):
        raise ValueError('Incomplete runtime')
    for group in (pack.GATEWAY_RECOVERY, pack.FILE_COMPONENT_FILES):
        present = [pack._optional_file(root, name) for name in group]
        if any(present) and not all(present):
            raise ValueError('Incomplete optional component group')
        if all(present):
            names.update(group)
    records = []
    expected = {row['path']: row for row in component_records}
    for name in sorted(names):
        path = pack.file_under(root, name)
        with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
            pack.validate_public_file(name, stream)
            if name == pack.COMPONENT_FILES[1]:
                pack._validate_component_recovery(stream, component_records)
        row = file_record(path, name)
        if name in expected and row != expected[name]:
            raise ValueError('Runtime differs from its component manifest')
        records.append(row)
    return validate_common_records(records)


def verify_release(release):
    release = absolute(release)
    manifest = read_json(pack.file_under(release, 'RELEASE.json'))
    if (type(manifest) is not dict or type(manifest.get('schema')) is not int or manifest['schema'] != 1
            or not isinstance(manifest.get('artifacts'), dict)):
        raise ValueError('Invalid release manifest')
    info = validate_release_info({key: manifest.get(key) for key in ('schema', 'release_id', 'source_commit')})
    app = release / 'app'
    if read_json(pack.file_under(app, INFO), 4096) != info:
        raise ValueError('Application and release provenance differ')
    rows = validate_common_records(manifest.get('common_files'))
    omitted = manifest.get('omitted')
    if omitted is None or omitted == []:
        if common_records(app, [row['path'] for row in rows]) != rows:
            raise ValueError('Application payload differs from release inventory')
    else:
        # A light package leaves out files the previous release already shipped unchanged.
        if (type(omitted) is not list or len(set(omitted)) != len(omitted)
                or not set(omitted) <= {row['path'] for row in rows}):
            raise ValueError('Invalid list of omitted files')
        if not {'Launcher.exe', INFO, *pack.COMPONENT_FILES} <= {row['path'] for row in rows}:
            raise ValueError('Incomplete application inventory')
        for row in rows:
            if row['path'] not in omitted and file_record(pack.file_under(app, row['path']), row['path']) != row:
                raise ValueError('Application payload differs from release inventory: ' + row['path'])
    archive_name = 'Pocket-Windows-' + info['release_id'] + '.zip'
    artifacts = {'Setup.exe', 'Windows-Claude.zip', 'Windows-Claude.zip.sha256',
                 'Windows-Claude.zip.manifest.json', archive_name, archive_name + '.sha256',
                 archive_name + '.manifest.json', 'НАЧАТЬ.txt', 'third-party.md'}
    for name, row in manifest['artifacts'].items():
        if name not in artifacts or row.get('path') != name or file_record(pack.file_under(release, name), name) != row:
            raise ValueError('Release artifact differs from manifest: ' + name)
    return manifest


def project_processes(root):
    """Inspect executable paths only; do not read arguments or terminate anything."""
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                    ('name', wintypes.WCHAR * 260)]
    kernel = runtime.kernel_api()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        getattr(kernel, name).restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = {}
    root = os.path.normcase(str(absolute(root)))
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            name = entry.name.casefold()
            if name in ('launcher.exe', 'python.exe', 'pythonw.exe', 'qemu-system-x86_64.exe',
                        'qemu-img.exe', 'hostfiles.exe', 'hostfilesguest.exe') or name.startswith('launcher.updated-'):
                process = runtime.process_identity(entry.pid)
                if process:
                    image = os.path.normcase(os.path.abspath(process['image']))
                    if image.startswith(root + os.sep):
                        relative = Path(image).relative_to(Path(root)).as_posix()
                        if not (process['pid'] == os.getpid() and re.fullmatch(r'state/app-updater-[a-f0-9]{32}/py/python.exe', relative)):
                            result[process['pid']] = process
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    return result


def _journal(target):
    value = read_json(pack.file_under(target, JOURNAL), JSON_LIMIT)
    if (type(value) is not dict or type(value.get('schema')) is not int or value['schema'] not in (1, 2)
            or not isinstance(value.get('target'), str) or not os.path.isabs(value['target'])
            or not isinstance(value.get('transaction'), str)
            or not re.fullmatch('app-update-[a-f0-9]{32}', value['transaction'])
            or type(value.get('files')) is not list or not 1 <= len(value['files']) <= 40000):
        raise ValueError('Invalid update journal; retain it for inspection')
    if value['schema'] == 2:
        owned = value.get('target_id')
        if (type(owned) is not dict or set(owned) != {'device', 'file_id'}
                or any(type(owned[key]) is not int for key in owned) or owned != identity(target)):
            raise ValueError('Update target directory identity changed; retain it for inspection')
    seen = set()
    for row in value['files']:
        if (type(row) is not dict or not {'path', 'new', 'old', 'new_id', 'phase'} <= row.keys()
                or not isinstance(row['path'], str) or not transaction_name(row['path']) or row['path'].casefold() in seen
                or row.get('phase') not in ('pending', 'replacing', 'installed', 'restored')):
            raise ValueError('Invalid update journal row')
        seen.add(row['path'].casefold())
        if row['new'] is None and row['old'] is None:
            raise ValueError('Empty update journal row')
        for record in (row['old'], row['new']):
            if record is not None:
                validate_common_records([record], transaction=True)
                if record['path'] != row['path']:
                    raise ValueError('Invalid journal destination')
        owned = row.get('new_id')
        if row['new'] is None and owned is None:
            continue
        if (type(owned) is not dict or set(owned) != {'device', 'file_id'}
                or any(type(owned[key]) is not int for key in owned) or owned['device'] < 0 or owned['file_id'] <= 0):
            raise ValueError('Invalid staged file identity')
    if value['schema'] == 1 and os.path.normcase(value['target']) != os.path.normcase(str(target)):
        transaction = target / 'state' / value['transaction']
        for parent in (*reversed(transaction.parents), transaction):
            _plain(parent)
        if not transaction.is_dir():
            raise ValueError('Moved legacy update transaction is missing')
        anchored, restored = False, True
        for row in value['files']:
            name = row['path']
            path = target / name
            _plain(path)
            current = file_record(pack.file_under(target, name), name) if path.exists() else None
            if current != row['old']:
                restored = False
                if (row['phase'] in ('pending', 'restored') or current != row['new']
                        or (current is not None and identity(path) != row['new_id'])):
                    raise ValueError('Moved legacy update destination changed; retain it for inspection')
            if row['old'] is not None:
                backup = pack.file_under(transaction / 'old', name)
                if file_record(backup, name) != row['old']:
                    raise ValueError('Moved legacy update backup changed; retain it for inspection')
            if row['new'] is not None:
                staged = transaction / 'new' / name
                _plain(staged)
                if staged.exists():
                    if file_record(pack.file_under(transaction / 'new', name), name) != row['new']:
                        raise ValueError('Moved legacy staged update changed; retain it for inspection')
                    anchored |= identity(staged) == row['new_id']
                anchored |= current == row['new'] and identity(path) == row['new_id']
        if not restored and not anchored:
            raise ValueError('Moved legacy update has no matching file identity; retain it for inspection')
    return value


def _rollback(target, journal):
    transaction = target / 'state' / journal['transaction']
    for row in reversed(journal['files']):
        path = target / row['path']
        if row['phase'] in ('pending', 'restored'):
            continue
        current = file_record(pack.file_under(target, row['path']), row['path']) if path.exists() else None
        if current == row['old']:
            row['phase'] = 'restored'
            continue
        if current != row['new']:
            raise RuntimeError('Installed file changed; rollback retained for inspection: ' + row['path'])
        if current is not None and identity(path) != row['new_id']:
            raise RuntimeError('Installed file identity changed; rollback retained for inspection: ' + row['path'])
        with _pinned_directory(path.parent, create=False):
            if row['old'] is None:
                remove_owned_file(path, row['new_id'])
            else:
                backup = pack.file_under(transaction / 'old', row['path'])
                # A partial copy from an interrupted attempt must not block recovery.
                replacement = transaction / 'restore' / (uuid.uuid4().hex + '.tmp')
                copy_record(backup, replacement, row['old'])
                os.replace(replacement, path)
        row['phase'] = 'restored'
        atomic_json(target / JOURNAL, journal)
    journal['phase'] = 'rolled_back'
    atomic_json(transaction / 'journal.json', journal)
    with _pinned_directory((target / JOURNAL).parent, create=False):
        remove_owned_file(target / JOURNAL, identity(target / JOURNAL))


def rollback_update(target):
    target = absolute(target)
    with _pinned_directory(target, create=False), runtime.ProjectLock(target) as acquired:
        if not acquired or project_processes(target):
            raise RuntimeError('Close Launcher and stop this project before recovering its update')
        journal = _journal(target)
        _rollback(target, journal)
    return {'rolled_back': True, 'target': str(target)}


@contextmanager
def unpublished_transaction(target, transaction):
    owned = identity(transaction)
    try:
        yield
    finally:
        if not (target / JOURNAL).exists() and not (transaction / 'journal.json').exists():
            with _pinned_directory(transaction.parent, create=False):
                _plain(transaction)
                if transaction.parent != target / 'state' or identity(transaction) != owned:
                    raise ValueError('Unpublished transaction identity changed; preserve it for inspection')
                shutil.rmtree(transaction)


def stage_gateway(app, target, generated, reserved=0):
    """Prepare gateway files for the same transaction; never retire an existing disk."""
    catalog_path = app / pack.GATEWAY_RECOVERY[0]
    if not catalog_path.exists():
        return []
    gateway = pack.gateway_package
    catalog = read_json(catalog_path, 8192)
    if (type(catalog) is not dict or set(catalog) != {'schema', 'file', 'sha256'}
            or type(catalog['schema']) is not int or catalog['schema'] != 1
            or catalog['file'] != 'gateway-recovery.zip'):
        raise ValueError('Invalid gateway recovery catalog')
    gateway._digest(catalog['sha256'])
    base = directory(target / 'gateway/packages')
    current_path = base / 'current.json'
    _plain(current_path)
    current = gateway._pointer(read_json(current_path, 65536)) if current_path.exists() else None
    for name in ('update.json', 'repair.json'):
        path = base / name
        _plain(path)
        if path.exists() and read_json(path, 65536).get('phase') == 'prepared':
            raise RuntimeError('Finish the interrupted gateway operation before updating the application')
    if current:
        with gateway._release(base, current['current']):
            pass
    source = pack.file_under(app, pack.GATEWAY_RECOVERY[1])
    with locked_read(source) as reader:
        if hashlib.file_digest(reader, 'sha256').hexdigest() != catalog['sha256']:
            raise ValueError('Gateway recovery checksum mismatch')
        reader.seek(0)
        with zipfile.ZipFile(reader) as archive:
            manifest = gateway._archive_manifest(archive)
            version = manifest['version']
            prefix = 'gateway/packages/releases/' + version + '/'
            existing = base / 'releases' / version
            _plain(existing)
            existing_payload = existing.exists() and any(existing.iterdir())
            if existing_payload:
                with gateway._release(base, version):
                    if read_json(existing / 'manifest.json', 65536) != manifest:
                        raise ValueError('Gateway version already exists with different content')
                with archive.open('gateway.qcow2') as incoming:
                    gateway._check_disk(incoming, manifest)
            else:
                if shutil.disk_usage(target).free < 2 * manifest['bytes'] + reserved + 64 * 1024**2:
                    raise RuntimeError('Insufficient space to stage and install the gateway')
                destination = directory(generated / prefix)
                with archive.open('gateway.qcow2') as incoming, (destination / 'gateway.qcow2').open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1 << 20)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                with locked_read(destination / 'gateway.qcow2') as disk:
                    gateway._check_disk(disk, manifest)
                atomic_json(destination / 'manifest.json', manifest)
    if current and current['current'] == version:
        return []
    directory(generated / 'gateway/packages')
    pointer = {'schema': 1, 'current': version, 'previous': current['current'] if current else None}
    atomic_json(generated / 'gateway/packages/current.json', pointer)
    atomic_json(generated / 'gateway/packages/update.json',
                {'schema': 1, 'phase': 'committed', 'old': current, 'new': pointer})
    names = ([] if existing_payload else [prefix + 'gateway.qcow2', prefix + 'manifest.json'])
    names += ['gateway/packages/current.json', 'gateway/packages/update.json']
    return [file_record(generated / name, name) for name in names]


def apply_update(release, target):
    release, target = absolute(release), absolute(target)
    if release == target or release.is_relative_to(target) or target.is_relative_to(release):
        raise ValueError('Release and installed application must be separate directories')
    manifest = verify_release(release)
    with _pinned_directory(target, create=False):
        if project_processes(target):
            raise RuntimeError('Close Launcher and shut down Windows and gateway before updating')
        from package_integrity import verify_initial
        integrity = verify_initial(target)
        if not integrity['ok']:
            raise RuntimeError('Initial package verification is busy; stop this copy before updating')
        legacy_without_manifest = integrity['code'] == 'legacy' and not (target / 'MANIFEST.json').exists()
        directory(target / 'state')
        with runtime.ProjectLock(target) as acquired, ExitStack() as pins:
            if not acquired or project_processes(target):
                raise RuntimeError('Close Launcher and shut down Windows and gateway before updating')
            if (target / JOURNAL).exists():
                raise RuntimeError('An unfinished update requires explicit rollback first')
            if legacy_without_manifest:
                legacy_marker = target / 'state/package-legacy-format.json'
                expected_legacy = {'schema': 1, 'legacy_format': True}
                _plain(legacy_marker)
                if legacy_marker.exists() and read_json(legacy_marker, 4096) != expected_legacy:
                    raise ValueError('Invalid legacy format declaration; retain it for inspection')
                if not legacy_marker.exists():
                    atomic_json(legacy_marker, expected_legacy)
            omitted = set(manifest.get('omitted') or ())
            for row in manifest['common_files']:
                if row['path'] in omitted and (not (target / row['path']).exists()
                        or file_record(pack.file_under(target, row['path']), row['path']) != row):
                    raise RuntimeError('This copy is too old for the light update; install the full package: ' + row['path'])
            rows = [row for row in manifest['common_files'] if row['path'] not in omitted]
            rows = [row for row in rows if not (target / row['path']).exists()
                    or file_record(pack.file_under(target, row['path']), row['path']) != row]
            obsolete = []
            inventory_path = target / INVENTORY
            _plain(inventory_path)
            if inventory_path.exists():
                previous = read_json(inventory_path)
                if type(previous) is not dict or previous.get('schema') != 1:
                    raise ValueError('Invalid installed application inventory')
                names = {row['path'].casefold() for row in manifest['common_files']}
                for row in validate_common_records(previous.get('files')):
                    path = target / row['path']
                    if row['path'].casefold() not in names and path.exists():
                        if file_record(pack.file_under(target, row['path']), row['path']) != row:
                            raise RuntimeError('Obsolete application file changed; preserve it before updating: ' + row['path'])
                        obsolete.append(row)
            previous_bytes = 0
            for row in rows + obsolete:
                path = target / row['path']
                directory(path.parent)
                pins.enter_context(_pinned_directory(path.parent, create=False))
                _plain(path)
                if path.exists():
                    with _exclusive_file(path, create=False):
                        pass
                    previous_bytes += path.stat().st_size
            needed = sum(row['bytes'] for row in rows) + 2 * previous_bytes + 16 * 1024**2
            if shutil.disk_usage(target).free < needed:
                raise RuntimeError('Insufficient space for update staging, backups and rollback')
            transaction = directory(target / 'state' / ('app-update-' + uuid.uuid4().hex))
            pins.enter_context(unpublished_transaction(target, transaction))
            pins.enter_context(_pinned_directory(transaction, create=False))
            journal = {'schema': 2, 'target': str(target), 'target_id': identity(target), 'transaction': transaction.name,
                       'release_id': manifest['release_id'], 'phase': 'prepared', 'files': []}
            generated = transaction / 'generated'
            directory(generated / 'state')
            atomic_json(generated / INVENTORY, {'schema': 1, 'files': manifest['common_files']})
            inventory = file_record(generated / INVENTORY, INVENTORY)
            # Delta releases must carry the pair when the gateway changes.
            gateway_rows = stage_gateway(release / 'app', target, generated, needed)
            for row in rows:
                path = target / row['path']
                staged = transaction / 'new' / row['path']
                copy_record(release / 'app' / row['path'], staged, row)
                old = file_record(path, row['path']) if path.exists() else None
                if old is not None:
                    copy_record(path, transaction / 'old' / row['path'], old)
                journal['files'].append({'path': row['path'], 'new': row, 'old': old,
                                         'new_id': identity(staged), 'phase': 'pending'})
                pins.enter_context(_pinned_directory(staged.parent, create=False))
            for row in obsolete:
                copy_record(target / row['path'], transaction / 'old' / row['path'], row)
                journal['files'].append({'path': row['path'], 'new': None, 'old': row,
                                         'new_id': None, 'phase': 'pending'})
            if shutil.disk_usage(target).free < sum(row['bytes'] for row in gateway_rows) + 16 * 1024**2:
                raise RuntimeError('Insufficient space for gateway staging')
            for row in [*gateway_rows, inventory]:
                path = target / row['path']
                directory(path.parent)
                pins.enter_context(_pinned_directory(path.parent, create=False))
                _plain(path)
                if path.exists():
                    with _exclusive_file(path, create=False):
                        pass
                staged = transaction / 'new' / row['path']
                copy_record(generated / row['path'], staged, row)
                pins.enter_context(_pinned_directory(staged.parent, create=False))
                old = file_record(path, row['path']) if path.exists() else None
                if old:
                    copy_record(path, transaction / 'old' / row['path'], old)
                journal['files'].append({'path': row['path'], 'new': row, 'old': old,
                                         'new_id': identity(staged), 'phase': 'pending'})
            # Reserve the longest phase names before replacing any application file.
            json_bytes(dict(journal, phase='rolled_back',
                            files=[dict(row, phase='replacing') for row in journal['files']]))
            atomic_json(target / JOURNAL, journal)
            try:
                if project_processes(target):
                    raise RuntimeError('Another process opened this copy while the update was being prepared')
                for row in journal['files']:
                    path = target / row['path']
                    actual = file_record(path, row['path']) if path.exists() else None
                    if actual != row['old']:
                        raise RuntimeError('Target changed after backup: ' + row['path'])
                    if row['new'] is not None:
                        staged = pack.file_under(transaction / 'new', row['path'])
                        if identity(staged) != row['new_id'] or file_record(staged, row['path']) != row['new']:
                            raise ValueError('Staged update changed before installation')
                    row['phase'] = 'replacing'
                    atomic_json(target / JOURNAL, journal)
                    if row['new'] is None:
                        remove_owned_file(path, identity(path))
                    else:
                        os.replace(staged, path)
                    row['phase'] = 'installed'
                    atomic_json(target / JOURNAL, journal)
                for row in journal['files']:
                    path = target / row['path']
                    if (file_record(path, row['path']) if path.exists() else None) != row['new']:
                        raise ValueError('Installed file verification failed')
                journal['phase'] = 'committed'
                atomic_json(transaction / 'journal.json', journal)
                remove_owned_file(target / JOURNAL, identity(target / JOURNAL))
            except BaseException:
                try:
                    _rollback(target, journal)
                except BaseException as error:
                    raise RuntimeError('Update recovery is required; journal and backups retained') from error
                raise
    return {'release_id': manifest['release_id'], 'source_commit': manifest['source_commit'], 'backup': str(transaction)}


def _git(source, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + source.as_posix(), '-C', str(source), *args],
                                   text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW).strip()


def source_commit(source):
    commit = _git(source, 'rev-parse', 'HEAD')
    if not re.fullmatch('[a-f0-9]{40}', commit) or _git(source, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise RuntimeError('Commit all source changes before building a release')
    return commit


def validate_build_inputs(source, commit):
    paths = sorted((source / 'ui').glob('*.cs'))
    paths += sorted(path for pattern in ('*.cs', '*.xaml', '*.csproj', 'app.manifest')
                    for path in (source / 'launcher-wpf').glob(pattern))
    paths += [source / name for name in ('ui/app.manifest', 'ui/AppIcon.ico',
              'scripts/Build-Launcher.ps1', 'docs/third-party.md', *pack.APP_FILES)]
    for path in paths:
        name = path.relative_to(source).as_posix()
        _plain(path)
        try:
            expected = _git(source, 'rev-parse', commit + ':' + name)
            actual = _git(source, 'hash-object', '--path=' + name, str(path))
        except subprocess.CalledProcessError as error:
            raise RuntimeError('Build input is not in the release commit: ' + name) from error
        if expected != actual:
            raise RuntimeError('Build input differs from the release commit: ' + name)


def run_logged(args, source, log):
    with log.open('wb') as stream:
        result = subprocess.run([str(arg) for arg in args], cwd=source, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=7200, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Release command failed; read ' + str(log))


def master_files(master):
    master = absolute(master)
    rows = []
    for name, key in (('candidate.qcow2', 'work_sha256'), ('vars.fd', 'vars_sha256')):
        path = pack.file_under(master, name)
        row = file_record(path, name)
        if row['sha256'] != MASTER_HASHES[key]:
            raise ValueError('Accepted clean master hash differs: ' + name)
        rows.append(row)
    return rows


def prepare_release(source, output, master, release_id):
    source, output = absolute(source), absolute(output)
    commit = source_commit(source)
    validate_build_inputs(source, commit)
    info = validate_release_info({'schema': 1, 'release_id': release_id, 'source_commit': commit})
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Release output must be a new directory outside the source checkout')
    master_files(master)
    inventory = bundle.inventory(source)
    engine = next(row for row in inventory['files'] if row['path'] == REQUIRED_RUNTIME[1])
    for name in ('portable_app.py', 'control_guest_install.py', 'reference_vm.py', 'patch_qemu_engine.py'):
        tree = ast.parse((source / 'scripts' / name).read_text(encoding='utf-8-sig'))
        pins = [ast.literal_eval(item.value) for item in tree.body if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'ENGINE_SHA256' for target in item.targets)]
        if pins != [engine['sha256']]:
            raise ValueError('QEMU engine source pins differ: ' + name)
    directory(output)
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    for script, destination in (('Build-Launcher.ps1', output / 'app'),):
        run_logged([powershell, '-NoProfile', '-NonInteractive', '-File', source / 'scripts' / script,
                    '-OutputDirectory', destination], source, output / (script + '.log'))
    app = output / 'app'
    names = [*pack.APP_FILES, *pack.COMPONENT_FILES]
    for group in (pack.GATEWAY_RECOVERY, pack.FILE_COMPONENT_FILES):
        present = [(source / name).is_file() for name in group]
        if not all(present):
            raise ValueError('Complete recovery and file-sharing components are required for this release')
        names.extend(group)
    names.extend(row['path'] for row in inventory['files'])
    for name in names:
        path = pack.file_under(source, name)
        copy_record(path, app / name, file_record(path, name))
    atomic_json(app / INFO, info)
    if source_commit(source) != commit:
        raise RuntimeError('Source commit changed while preparing release')
    validate_build_inputs(source, commit)
    manifest = dict(info, created_utc=datetime.now(timezone.utc).isoformat(), common_files=common_records(app),
                    artifacts={},
                    master=dict(MASTER_HASHES), manual_acceptance=None)
    atomic_json(output / 'RELEASE.json', manifest)
    verify_release(output)
    return manifest


def package_release(release, source, master):
    release, source = absolute(release), absolute(source)
    manifest = verify_release(release)
    if source_commit(source) != manifest['source_commit']:
        raise ValueError('Package sources must match the release commit')
    stage = release / 'public'
    archive_name = 'Pocket-Windows-' + manifest['release_id'] + '.zip'
    if stage.exists() or (release / archive_name).exists():
        raise FileExistsError('Public staging or archive already exists')
    needed = (absolute(master) / 'candidate.qcow2').stat().st_size * 3 + 4 * 1024**3
    if shutil.disk_usage(release).free < needed:
        raise RuntimeError('Insufficient space for public staging, archive and installation check')
    directory(stage)
    for row in manifest['common_files']:
        copy_record(release / 'app' / row['path'], stage / row['path'], row)
    master_rows = master_files(master)
    for row, name in zip(master_rows, pack.DATA_FILES):
        copy_record(absolute(master) / row['path'], stage / name, row)
    from runtime_config import DEFAULTS, VIEWER_DEFAULTS
    from windows_guest_settings import DEFAULTS as WINDOWS_DEFAULTS
    directory(stage / 'config')
    for name, value in (('runtime.json', DEFAULTS), ('windows.json', WINDOWS_DEFAULTS), ('viewer.json', VIEWER_DEFAULTS)):
        atomic_json(stage / 'config' / name, value)
    from portable_app import repair_gateway
    if not repair_gateway(stage)['ok']:
        raise RuntimeError('Could not prepare the approved gateway package')
    instructions = ('Pocket Windows — ' + manifest['release_id'] + '\n\n'
                    'Распакуй ' + archive_name + ' Проводником в новую папку.\n'
                    'Открой Pocket-Windows\\Launcher.exe. Ярлык на рабочий стол создаётся на главной странице.\n'
                    'Файл .sha256 служит для проверки целостности архива.\n'
                    'Личные подключения в этот комплект не входят.\n')
    (stage / 'НАЧАТЬ.txt').write_text(instructions, encoding='utf-8-sig')
    shutil.copyfile(source / 'docs/third-party.md', stage / 'third-party.md')
    python = source / 'runtime/python-3.12.14/python.exe'
    run_logged([python, '-X', 'utf8', '-I', '-S', '-B', source / 'scripts/pack_existing_runtime.py',
                '--mode', 'public', '--project', stage, '--output', release / archive_name],
               source, release / 'package.log')
    for name in (archive_name, archive_name + '.sha256', archive_name + '.manifest.json'):
        manifest['artifacts'][name] = file_record(release / name, name)
    for name in ('НАЧАТЬ.txt', 'third-party.md'):
        shutil.copyfile(stage / name, release / name)
        manifest['artifacts'][name] = file_record(release / name, name)
    atomic_json(release / 'RELEASE.json', manifest)
    return verify_release(release)


def publish_release(release, destination):
    release, destination = absolute(release), absolute(destination)
    if release == destination or release.is_relative_to(destination) or destination.is_relative_to(release):
        raise ValueError('Release and publication directories must be separate')
    manifest = verify_release(release)
    acceptance = manifest.get('manual_acceptance')
    if (not isinstance(acceptance, dict) or acceptance.get('source_commit') != manifest['source_commit']
            or acceptance.get('release_id') != manifest['release_id']
            or acceptance.get('launcher') is not True or acceptance.get('vm') is not True):
        raise RuntimeError('Owner acceptance of Launcher and VM is required before publication')
    archive_name = 'Pocket-Windows-' + manifest['release_id'] + '.zip'
    required = {archive_name, archive_name + '.sha256', archive_name + '.manifest.json',
                'НАЧАТЬ.txt', 'third-party.md'}
    if set(manifest['artifacts']) != required:
        raise ValueError('Public release is incomplete')
    stage = destination.with_name('.' + destination.name + '-release-' + uuid.uuid4().hex)
    directory(stage)
    for name in (archive_name, archive_name + '.sha256'):
        copy_record(release / name, stage / name, manifest['artifacts'][name])
    backup = release.parent / ('previous-installer-' + manifest['release_id'])
    if backup.exists():
        raise FileExistsError('Previous installer backup already exists')
    with _pinned_directory(destination.parent, create=False), _pinned_directory(backup.parent, create=False):
        _plain(destination)
        existed = destination.exists()
        if existed:
            os.rename(destination, backup)
        try:
            os.rename(stage, destination)
        except BaseException:
            if existed:
                os.rename(backup, destination)
            raise
    return {'published': str(destination), 'previous': str(backup) if existed else None}


def package_app(release, base=None):
    """Light update package: the application files, minus those the base release shipped unchanged."""
    release = absolute(release)
    manifest = verify_release(release)
    known = {row['path']: row for row in read_json(absolute(base))['common_files']} if base else {}
    omitted = sorted(row['path'] for row in manifest['common_files'] if known.get(row['path']) == row)
    omitted = [name for name in omitted if name != INFO]
    if any(name not in omitted for name in pack.GATEWAY_RECOVERY):
        omitted = [name for name in omitted if name not in pack.GATEWAY_RECOVERY]
    light = dict(manifest, artifacts={}, omitted=omitted, manual_acceptance=None)
    name = 'Pocket-Windows-' + manifest['release_id'] + '-app.zip'
    archive = release / name
    if archive.exists():
        raise FileExistsError('Light package already exists')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.writestr('RELEASE.json', json_bytes(light))
        for row in manifest['common_files']:
            if row['path'] not in omitted:
                bundle.write(pack.file_under(release / 'app', row['path']), 'app/' + row['path'])
    digest = _sha256_file(archive)
    (release / (name + '.sha256')).write_text(digest + '  ' + name + '\n', encoding='ascii')
    # Old launchers need a bridge release before packages that introduce new paths.
    added = sorted(row['path'] for row in manifest['common_files'] if known and row['path'] not in known and not row['path'].startswith(pack.RUNTIME_PREFIXES))
    return {'package': str(archive), 'bytes': archive.stat().st_size, 'sha256': digest, 'omitted': len(omitted), 'new_application_files': added}


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def install_app(archive, sha256, target):
    """Verify a downloaded light package, unpack it outside the target and apply it as a normal update."""
    stage = Path(tempfile.mkdtemp(prefix='pocket-windows-update-', dir=absolute(archive).parent))
    try:
        unpack_app(archive, sha256, stage)
        return apply_update(stage, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def unpack_app(archive, sha256, stage):
    """Hash and unpack the same locked file; accept only the exact declared payload."""
    archive = absolute(archive)
    with _pinned_directory(archive.parent, create=False), locked_read(archive) as incoming:
        if (not re.fullmatch('[a-f0-9]{64}', sha256 or '') or os.fstat(incoming.fileno()).st_size > 8 * 1024**3
                or hashlib.file_digest(incoming, 'sha256').hexdigest() != sha256):
            raise ValueError('The downloaded update does not match its checksum or size limit')
        incoming.seek(0)
        with zipfile.ZipFile(incoming) as bundle:
            entries = bundle.infolist()
            names = [entry.filename for entry in entries]
            if (not 2 <= len(names) <= 40001 or len(names) != len(set(name.casefold() for name in names))
                    or 'RELEASE.json' not in names or bundle.getinfo('RELEASE.json').file_size > JSON_LIMIT):
                raise ValueError('Invalid update archive inventory')
            with bundle.open('RELEASE.json') as reader:
                manifest = pack._json(reader)
            rows = validate_common_records(manifest.get('common_files'))
            omitted = manifest.get('omitted', [])
            if type(omitted) is not list or not all(type(name) is str for name in omitted):
                raise ValueError('Invalid omitted file list')
            expected = {row['path']: row for row in rows if row['path'] not in omitted}
            if set(names) != {'RELEASE.json', *('app/' + name for name in expected)}:
                raise ValueError('Update ZIP differs from its application inventory')
            if sum(entry.file_size for entry in entries) > 12 * 1024**3:
                raise ValueError('Update expands beyond its size limit')
            if shutil.disk_usage(stage).free < sum(entry.file_size for entry in entries) + 64 * 1024**2:
                raise RuntimeError('Insufficient space to unpack the application update')
            for entry in entries:
                name = entry.filename
                if (not pack.safe_name(name) or entry.is_dir() or entry.flag_bits & 1 or entry.external_attr & 0x400
                        or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError('Unsafe update ZIP member')
                if name != 'RELEASE.json' and entry.file_size != expected[name[4:]]['bytes']:
                    raise ValueError('Update ZIP member size differs from inventory')
                destination = stage / name
                directory(destination.parent)
                with bundle.open(entry) as source, destination.open('xb') as outgoing:
                    shutil.copyfileobj(source, outgoing, 1 << 20)
    return verify_release(stage)


def check_app(archive, sha256, release_id):
    stage = Path(tempfile.mkdtemp(prefix='pocket-windows-check-', dir=absolute(archive).parent))
    try:
        manifest = unpack_app(archive, sha256, stage)
        if manifest['release_id'] != release_id:
            raise ValueError('Downloaded release differs from the offered version')
        if manifest.get('omitted'):
            raise ValueError('Automatic updates require a complete application package')
        return {'release_id': manifest['release_id'], 'checked': True}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def wait_for_launcher(target, pid, created):
    process = runtime.process_identity(pid)
    if process is None:
        return
    if (process['created_filetime'] != created
            or os.path.normcase(process['image']) != os.path.normcase(str(target / 'Launcher.exe'))):
        raise RuntimeError('The launcher process identity changed; update cancelled')
    kernel = runtime.kernel_api()
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if runtime.process_identity(pid) is None:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if runtime.process_identity(pid) not in (None, process):
            raise RuntimeError('The launcher process changed while waiting')
        if kernel.WaitForSingleObject(handle, 120000) != 0:
            raise RuntimeError('The launcher did not exit; application files were not changed')
    finally:
        kernel.CloseHandle(handle)


def run_app_update(target, pid, created, archive=None, sha256=None, release_id=None, recover=False):
    target = absolute(target)
    directory(target / 'logs')
    result = {'schema': 1, 'status': 'failed'}
    try:
        wait_for_launcher(target, pid, created)
        if recover:
            rollback_update(target)
            result.update(status='rolled_back', message='Предыдущая версия восстановлена. Можно повторить обновление.')
        else:
            check_app(archive, sha256, release_id)
            installed = install_app(archive, sha256, target)
            if installed['release_id'] != release_id:
                raise RuntimeError('Installed version differs from the offered release')
            result.update(status='committed', release_id=release_id, message='Обновлено до версии ' + release_id)
    except Exception:
        with (target / 'logs/update-details.txt').open('w', encoding='utf-8') as log:
            traceback.print_exc(file=log)
        if (target / JOURNAL).exists():
            result.update(status='recovery_required', message='Обновление прервано. Нужен откат из сохранённой копии.')
        else:
            result['message'] = 'Обновление не установлено. Прежние файлы сохранены. Подробности: logs/update-details.txt.'
    atomic_json(target / 'logs/update-result.json', result)
    if archive is not None:
        downloaded = absolute(archive)
        folder = downloaded.parent
        if (downloaded.name == 'app.zip' and folder.parent == Path(tempfile.gettempdir())
                and re.fullmatch(r'pocket-windows-updater-[a-f0-9]{32}', folder.name)):
            try:
                with _pinned_directory(folder, create=False):
                    remove_owned_file(downloaded, identity(downloaded))
                folder.rmdir()
            except (OSError, ValueError):
                pass
    # A pending journal opens only the recovery prompt, never the normal application.
    if not project_processes(target):
        subprocess.Popen([str(target / 'Launcher.exe')], cwd=target, creationflags=subprocess.CREATE_NO_WINDOW)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    for name in ('source', 'output', 'master'):
        prepare.add_argument('--' + name, type=Path, required=True)
    prepare.add_argument('--release-id', required=True)
    package = commands.add_parser('package')
    for name in ('release', 'source', 'master'):
        package.add_argument('--' + name, type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--release', type=Path, required=True)
    update = commands.add_parser('update')
    update.add_argument('--release', type=Path, required=True)
    update.add_argument('--target', type=Path, required=True)
    light = commands.add_parser('app-package')
    light.add_argument('--release', type=Path, required=True)
    light.add_argument('--base', type=Path, help='RELEASE.json of the previous release: files it shipped unchanged are left out')
    install = commands.add_parser('install-app')
    install.add_argument('--zip', type=Path, required=True)
    install.add_argument('--sha256', required=True)
    install.add_argument('--target', type=Path, required=True)
    check = commands.add_parser('check-app')
    check.add_argument('--zip', type=Path, required=True)
    check.add_argument('--sha256', required=True)
    check.add_argument('--release-id', required=True)
    runner = commands.add_parser('run-app-update')
    runner.add_argument('--target', type=Path, required=True)
    runner.add_argument('--wait-pid', type=int, required=True)
    runner.add_argument('--wait-created', type=int, required=True)
    runner.add_argument('--zip', type=Path)
    runner.add_argument('--sha256')
    runner.add_argument('--release-id')
    runner.add_argument('--recover', action='store_true')
    rollback = commands.add_parser('rollback')
    rollback.add_argument('--target', type=Path, required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--release', type=Path, required=True)
    publish.add_argument('--target', type=Path, required=True)
    publish.add_argument('--owner-accepted', action='store_true', help='Use only after explicit human Launcher and VM acceptance')
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare_release(args.source, args.output, args.master, args.release_id)
    elif args.command == 'package':
        result = package_release(args.release, args.source, args.master)
    elif args.command == 'update':
        result = apply_update(args.release, args.target)
    elif args.command == 'app-package':
        result = package_app(args.release, args.base)
    elif args.command == 'install-app':
        result = install_app(args.zip, args.sha256, args.target)
    elif args.command == 'check-app':
        result = check_app(args.zip, args.sha256, args.release_id)
    elif args.command == 'run-app-update':
        result = run_app_update(args.target, args.wait_pid, args.wait_created,
                                args.zip, args.sha256, args.release_id, args.recover)
    elif args.command == 'rollback':
        result = rollback_update(args.target)
    elif args.command == 'publish':
        if args.owner_accepted:
            manifest = verify_release(args.release)
            manifest['manual_acceptance'] = {'release_id': manifest['release_id'], 'source_commit': manifest['source_commit'],
                                             'launcher': True, 'vm': True, 'recorded_utc': datetime.now(timezone.utc).isoformat()}
            atomic_json(absolute(args.release) / 'RELEASE.json', manifest)
        result = publish_release(args.release, args.target)
    else:
        result = verify_release(args.release)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()

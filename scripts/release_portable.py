"""Build one committed release and update application files without replacing user data."""
import argparse
import ast
import base64
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
import time
import traceback
import urllib.parse
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_config import POWERSHELL, PYTHON, QEMU
import pack_existing_runtime as pack
import prepare_runtime_bundle as bundle
import launch_runtime as runtime
from file_snapshot import locked_read, remove_owned_file
from profile_store import _plain, _pinned_directory, _exclusive_file

INFO = 'release-info.json'
JOURNAL = 'state/app-update.json'
INVENTORY = 'state/application-files.json'
JSON_LIMIT = 16 * 1024 * 1024
REQUIRED_RUNTIME = (PYTHON + '/python.exe',
                    QEMU + '/qemu-system-x86_64.exe',
                    QEMU + '/qemu-img.exe',
                    QEMU + '/share/edk2-x86_64-code.fd')
MASTER_HASHES = {'work_sha256': 'fc5f18a8342ed7575dd028c36208c06a803ea0100fe44ee37e5bbdd4393f65b5',
                 'vars_sha256': '73696892959e012e0a9b74073bdafa6a1cfbe4dc1d871195447cca6d6ce11214'}
validate_release_info = pack.validate_release_info


def absolute(path):
    return Path(os.path.abspath(path))


def _replace(source, target, seconds=10):
    """Antivirus and indexers hold freshly written files briefly; a sharing violation must not force a rollback."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return os.replace(source, target)
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in (5, 32) or time.monotonic() >= deadline:
                raise
            time.sleep(.25)


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
        'gateway/packages/current.json', 'gateway/packages/update.json', 'state/components/current.json')
        or bool(re.fullmatch(r'state/components/catalogs/[a-f0-9]{64}\.json', name))
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
    if (type(value) is not dict or type(value.get('schema')) is not int or value['schema'] not in (1, 2, 3)
            or not isinstance(value.get('target'), str) or not os.path.isabs(value['target'])
            or not isinstance(value.get('transaction'), str)
            or not re.fullmatch('app-update-[a-f0-9]{32}', value['transaction'])
            or type(value.get('files')) is not list or not 1 <= len(value['files']) <= 40000):
        raise ValueError('Invalid update journal; retain it for inspection')
    if value['schema'] >= 2:
        owned = value.get('target_id')
        if (type(owned) is not dict or set(owned) != {'device', 'file_id'}
                or any(type(owned[key]) is not int for key in owned) or owned != identity(target)):
            raise ValueError('Update target directory identity changed; retain it for inspection')
    if value['schema'] == 3:
        validate_release_info(value.get('release'))
        probe = value.get('probe')
        if (type(probe) is not dict or set(probe) != {'nonce', 'pid', 'created'}
                or not isinstance(probe['nonce'], str) or not re.fullmatch('[a-f0-9]{32}', probe['nonce'])
                or type(probe['pid']) is not int or probe['pid'] < 0
                or not isinstance(probe['created'], str) or not re.fullmatch('[0-9]{1,20}', probe['created'])):
            raise ValueError('Invalid update probe identity')
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
                _replace(replacement, path)
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


def _stop_probe(process, target, journal):
    if process is None or process.poll() is not None:
        return
    actual = runtime.process_identity(process.pid)
    expected = journal['probe']
    if (not actual or actual['pid'] != expected['pid']
            or str(actual['created_filetime']) != expected['created']
            or os.path.normcase(actual['image']) != os.path.normcase(str(target / 'Launcher.exe'))):
        raise RuntimeError('Probe process identity changed; recovery retained')
    process.terminate()
    process.wait(timeout=15)


def confirm_probe(target, journal):
    """Only this launched panel may acknowledge the provisional installation."""
    receipt = target / 'state/update-probe.json'
    _plain(receipt)
    if receipt.exists():
        remove_owned_file(receipt, identity(receipt))
    process = subprocess.Popen([str(target / 'Launcher.exe'), '--update-probe', journal['probe']['nonce']],
                               cwd=target, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        actual = runtime.process_identity(process.pid)
        if not actual or os.path.normcase(actual['image']) != os.path.normcase(str(target / 'Launcher.exe')):
            raise RuntimeError('New launcher did not start')
        journal['probe'].update(pid=process.pid, created=str(actual['created_filetime']))
        atomic_json(target / JOURNAL, journal)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('New launcher exited before confirming the update')
            if receipt.exists():
                value = read_json(receipt, 4096)
                expected = dict(schema=1, nonce=journal['probe']['nonce'], ok=True, root=str(target),
                                pid=process.pid, created=journal['probe']['created'],
                                release_id=journal['release']['release_id'],
                                source_commit=journal['release']['source_commit'])
                if value != expected or runtime.process_identity(process.pid) != actual:
                    raise ValueError('New launcher confirmation identity differs')
                return process
            time.sleep(.1)
        raise TimeoutError('New launcher did not confirm within 120 seconds')
    except BaseException:
        _stop_probe(process, target, journal)
        raise


def probe_app(target, nonce):
    target = absolute(target)
    journal = _journal(target)
    probe = journal.get('probe', {})
    if journal['schema'] != 3 or journal.get('phase') != 'awaiting_probe' or probe.get('nonce') != nonce:
        raise ValueError('No matching provisional update')
    current = runtime.process_identity(probe['pid'])
    if (not current or str(current['created_filetime']) != probe['created']
            or os.path.normcase(current['image']) != os.path.normcase(str(target / 'Launcher.exe'))):
        raise ValueError('Provisional launcher identity differs')
    info = validate_release_info(read_json(pack.file_under(target, INFO), 4096))
    if info != journal['release']:
        raise ValueError('Provisional application version differs')
    inventory = read_json(pack.file_under(target, INVENTORY))
    records = validate_common_records(inventory.get('files'), transaction=inventory.get('schema') == 2)
    names = {row['path'] for row in records}
    if not {'Launcher.exe', INFO, *REQUIRED_RUNTIME} <= names:
        raise ValueError('Provisional runtime is incomplete')
    # Rehash only what this transaction wrote: the full inventory (gateway disk included) does not fit the probe window on slow disks.
    written = {row['path'] for row in journal['files']}
    for row in records:
        path = pack.file_under(target, row['path'])
        if (file_record(path, row['path']) != row) if row['path'] in written else path.stat().st_size != row['bytes']:
            raise ValueError('Provisional file differs: ' + row['path'])
    for name in REQUIRED_RUNTIME[1:3]:
        check = subprocess.run([str(pack.file_under(target, name)), '--version'], cwd=target,
                               capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if check.returncode:
            raise RuntimeError('Provisional runtime check failed: ' + name)
    return dict(info, ok=True)


def prepared_release(stage, digest, target):
    import app_update_v2 as parts
    stage, target = absolute(stage), absolute(target)
    # Prepared payloads are disposable files in this copy, never a release directory.
    if stage.parent != target / 'state' or not re.fullmatch('[a-f0-9]{32}', stage.name):
        raise ValueError('Prepared files must belong to this copy disposable update stage')
    index = parts.read_index(stage / 'index.json', digest)
    prepared = read_json(pack.file_under(stage, 'prepared.json'))
    names = {row['path'] for row in index['files']}
    if (type(prepared) is not dict or set(prepared) != {'schema', 'index_sha256', 'omitted'}
            or type(prepared['schema']) is not int or prepared['schema'] != 2
            or prepared['index_sha256'] != digest or type(prepared['omitted']) is not list
            or any(type(name) is not str for name in prepared['omitted'])
            or len(set(prepared['omitted'])) != len(prepared['omitted']) or not set(prepared['omitted']) <= names):
        raise ValueError('Invalid prepared update identity')
    required = {'Launcher.exe', INFO, pack.COMPONENT_FILES[0], *REQUIRED_RUNTIME,
                'scripts/release_portable.py', 'scripts/portable_app.py', 'scripts/launch_runtime.py'}
    if not required <= names:
        raise ValueError('Incomplete prepared application/runtime')
    omitted = set(prepared['omitted'])
    rows = [{key: row[key] for key in ('path', 'bytes', 'sha256')} for row in index['files']]
    validate_common_records(rows, transaction=True)
    for row in rows:
        root = target if row['path'] in omitted else stage / 'app'
        if file_record(pack.file_under(root, row['path']), row['path']) != row:
            raise ValueError('Prepared or reused file differs: ' + row['path'])
    info_root = target if INFO in omitted else stage / 'app'
    if read_json(pack.file_under(info_root, INFO), 4096) != index['release']:
        raise ValueError('Prepared provenance differs')
    parts.verify_cache(index, target)
    return dict(index['release'], common_files=rows, omitted=prepared['omitted'], artifacts={},
                payload_schema=2, index_sha256=digest)


def consume_prepared(stage, target, destination, row):
    """Move only an owned disposable stage, never files from a release artifact."""
    stage, target, destination = map(absolute, (stage, target, destination))
    if (stage.parent != target / 'state' or not re.fullmatch('[a-f0-9]{32}', stage.name)
            or not destination.is_relative_to(target / 'state')
            or not re.fullmatch('app-update-[a-f0-9]{32}', destination.relative_to(target / 'state').parts[0])):
        raise ValueError('Prepared file move is outside this disposable transaction')
    with _pinned_directory(stage, create=False), _pinned_directory(target, create=False):
        source = pack.file_under(stage / 'app', row['path'])
        if file_record(source, row['path']) != row:
            raise ValueError('Prepared file changed before handoff')
        directory(destination.parent)
        _plain(destination)
        if destination.exists():
            raise ValueError('Prepared destination already exists')
        with _pinned_directory(source.parent, create=False), _pinned_directory(destination.parent, create=False):
            os.replace(source, destination)


def _update_operations(target, records, same, prepared):
    """The files that will be installed or removed, shared by planning and commit."""
    rows = [row for row in records if row['path'] not in same]
    obsolete = []
    inventory_path = target / INVENTORY
    _plain(inventory_path)
    if inventory_path.exists():
        previous = read_json(inventory_path)
        if type(previous) is not dict or type(previous.get('schema')) is not int or previous['schema'] not in (1, 2):
            raise ValueError('Invalid installed application inventory')
        names = {row['path'].casefold() for row in records}
        for row in validate_common_records(previous.get('files'), transaction=previous['schema'] == 2):
            path = target / row['path']
            if row['path'].startswith('gateway/packages/releases/'):
                continue  # Versioned gateway bases remain available to the retained rollback.
            if row['path'].casefold() not in names and path.exists():
                if file_record(pack.file_under(target, row['path']), row['path']) != row:
                    raise RuntimeError('Obsolete application file changed; preserve it before updating: ' + row['path'])
                obsolete.append(row)
    if not prepared:
        catalog_pointer = target / 'state/components/current.json'
        _plain(catalog_pointer)
        if catalog_pointer.exists():
            obsolete.append(file_record(catalog_pointer, 'state/components/current.json'))
    return rows, obsolete


def _update_space(rows, previous_bytes):
    return sum(row['bytes'] for row in rows) + 2 * previous_bytes + 16 * 1024**2


def apply_update(release, target, probe=False, prepared_sha256=None, _local_stage=False):
    release, target = absolute(release), absolute(target)
    local_stage = (_local_stage and release.parent == target / 'state' and bool(re.fullmatch('[a-f0-9]{32}', release.name)))
    if release == target or (prepared_sha256 is None and not local_stage and release.is_relative_to(target)) or target.is_relative_to(release):
        raise ValueError('Release and installed application must be separate directories')
    manifest = prepared_release(release, prepared_sha256, target) if prepared_sha256 else verify_release(release)
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
            same = {row['path'] for row in manifest['common_files']} - {row['path'] for row in rows}
            rows, obsolete = _update_operations(target, manifest['common_files'], same, bool(prepared_sha256))
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
            needed = _update_space(rows, previous_bytes)
            if shutil.disk_usage(target).free < needed:
                raise RuntimeError('Insufficient space for update staging, backups and rollback')
            transaction = directory(target / 'state' / ('app-update-' + uuid.uuid4().hex))
            pins.enter_context(unpublished_transaction(target, transaction))
            pins.enter_context(_pinned_directory(transaction, create=False))
            journal = {'schema': 2, 'target': str(target), 'target_id': identity(target), 'transaction': transaction.name,
                       'release_id': manifest['release_id'], 'phase': 'prepared', 'files': []}
            if probe:
                journal.update(schema=3, release={key: manifest[key] for key in ('schema', 'release_id', 'source_commit')},
                               probe={'nonce': uuid.uuid4().hex, 'pid': 0, 'created': '0'})
                if prepared_sha256:
                    journal['prepared'] = release.name
                tools = absolute(sys.executable).parent.parent
                if tools.parent == target / 'state' and re.fullmatch('app-updater-[a-f0-9]{32}', tools.name):
                    journal['tools'] = tools.name
            generated = transaction / 'generated'
            directory(generated / 'state')
            atomic_json(generated / INVENTORY, {'schema': 2 if prepared_sha256 else 1, 'files': manifest['common_files']})
            inventory = file_record(generated / INVENTORY, INVENTORY)
            # Delta releases must carry the pair when the gateway changes.
            if prepared_sha256:
                import app_update_v2 as parts
                import update_storage as storage
                index = parts.read_index(release / 'index.json', prepared_sha256)
                gateway_rows = storage.build_catalog(release / 'index.json', prepared_sha256, target, generated)
                if index['gateway']:
                    gateway = pack.gateway_package
                    version = index['gateway']['version']
                    current_path = target / 'gateway/packages/current.json'
                    current = gateway._pointer(read_json(current_path, 65536)) if current_path.exists() else None
                    for operation in ('update.json', 'repair.json'):
                        path = current_path.parent / operation
                        if path.exists() and read_json(path, 65536).get('phase') == 'prepared':
                            raise RuntimeError('Finish the interrupted gateway operation first')
                    prefix = 'gateway/packages/releases/' + version + '/'
                    disk_root = target if prefix + 'gateway.qcow2' in omitted else release / 'app'
                    with locked_read(pack.file_under(disk_root, prefix + 'gateway.qcow2')) as stream:
                        gateway._check_disk(stream, index['gateway'])
                    pointer = dict(schema=1, current=version, previous=current['current'] if current and current['current'] != version else (current or {}).get('previous'))
                    for name, value in (('current.json', pointer), ('update.json', dict(schema=1, phase='committed', old=current, new=pointer))):
                        path = generated / 'gateway/packages' / name
                        directory(path.parent)
                        atomic_json(path, value)
                        gateway_rows.append(file_record(path, 'gateway/packages/' + name))
            else:
                gateway_rows = stage_gateway(release / 'app', target, generated, needed)
            for row in rows:
                path = target / row['path']
                staged = transaction / 'new' / row['path']
                if prepared_sha256:
                    consume_prepared(release, target, staged, row)
                else:
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
            probe_process = None
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
                        _replace(staged, path)
                    row['phase'] = 'installed'
                    atomic_json(target / JOURNAL, journal)
                for row in journal['files']:
                    path = target / row['path']
                    if (file_record(path, row['path']) if path.exists() else None) != row['new']:
                        raise ValueError('Installed file verification failed')
                if probe:
                    journal['phase'] = 'awaiting_probe'
                    atomic_json(target / JOURNAL, journal)
                    probe_process = confirm_probe(target, journal)
                    atomic_json(target / 'state/update-accepted.json', dict(schema=1, committed=True,
                                nonce=journal['probe']['nonce'], root=str(target),
                                transaction=transaction.name, pid=journal['probe']['pid'], created=journal['probe']['created'],
                                release_id=manifest['release_id'], source_commit=manifest['source_commit']))
                journal['phase'] = 'committed'
                atomic_json(transaction / 'journal.json', journal)
                remove_owned_file(target / JOURNAL, identity(target / JOURNAL))
            except BaseException:
                try:
                    _stop_probe(probe_process, target, journal) if probe else None
                    _rollback(target, journal)
                except BaseException as error:
                    raise RuntimeError('Update recovery is required; journal and backups retained') from error
                raise
    return {'release_id': manifest['release_id'], 'source_commit': manifest['source_commit'], 'backup': str(transaction),
            'probe': journal.get('probe')}


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
              'scripts/Build-Launcher.ps1', 'scripts/Build-GuestFiles.ps1', 'scripts/Build-HostFiles.ps1',
              'scripts/update_toolkit.py', 'docs/third-party.md', *pack.APP_FILES)]
    paths += sorted((source / 'guest').glob('*.cs')) + sorted((source / 'guest/files').glob('*.cs'))
    paths += sorted((source / 'host/files').glob('*.cs')) + sorted((source / 'host/files/dependency-patches').glob('*.cs'))
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


def prepare_release(source, output, master, release_id, components=None, bridge=None):
    import update_toolkit
    source, output = absolute(source), absolute(output)
    pinned_keys()
    commit = source_commit(source)
    validate_build_inputs(source, commit)
    info = validate_release_info({'schema': 1, 'release_id': release_id, 'source_commit': commit})
    if bridge and components:
        raise ValueError('A bridge must preserve its base components')
    frozen = verify_release(bridge) if bridge else None
    component_source = absolute(bridge) / 'app' if bridge else source
    components = absolute(components) if components else component_source / 'runtime/host-files'
    if not frozen:
        update_toolkit.validate_host_build(source, components)
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Release output must be a new directory outside the source checkout')
    master_files(master)
    inventory = bundle.inventory(component_source)
    engine = next(row for row in inventory['files'] if row['path'] == REQUIRED_RUNTIME[1])
    for name in ('portable_app.py', 'control_guest_install.py', 'reference_vm.py', 'patch_qemu_engine.py'):
        tree = ast.parse((source / 'scripts' / name).read_text(encoding='utf-8-sig'))
        pins = [ast.literal_eval(item.value) for item in tree.body if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'ENGINE_SHA256' for target in item.targets)]
        if pins != [engine['sha256']]:
            raise ValueError('QEMU engine source pins differ: ' + name)
    directory(output)
    powershell = POWERSHELL
    for script, destination in (('Build-Launcher.ps1', output / 'app'),):
        run_logged([powershell, '-NoProfile', '-NonInteractive', '-File', source / 'scripts' / script,
                    '-OutputDirectory', destination], source, output / (script + '.log'))
    app = output / 'app'
    names = [*pack.APP_FILES, *pack.COMPONENT_FILES]
    for group in (pack.GATEWAY_RECOVERY, pack.FILE_COMPONENT_FILES):
        present = [(components / Path(name).name).is_file() if group == pack.FILE_COMPONENT_FILES
                   else (component_source / name).is_file() for name in group]
        if not all(present):
            raise ValueError('Complete recovery and file-sharing components are required for this release')
        names.extend(group)
    names.extend(row['path'] for row in inventory['files'])
    for name in names:
        if name in pack.FILE_COMPONENT_FILES:
            path = pack.file_under(components, name.removeprefix('runtime/host-files/'))
        else:
            root = component_source if pack.runtime_path(name) or name in (*pack.COMPONENT_FILES, *pack.GATEWAY_RECOVERY) else source
            path = pack.file_under(root, name)
        copy_record(path, app / name, file_record(path, name))
    atomic_json(app / INFO, info)
    if source_commit(source) != commit:
        raise RuntimeError('Source commit changed while preparing release')
    validate_build_inputs(source, commit)
    manifest = dict(info, created_utc=datetime.now(timezone.utc).isoformat(), common_files=common_records(app),
                    artifacts={},
                    master=dict(MASTER_HASHES), manual_acceptance=None)
    if frozen:
        manifest['bridge_base'] = {key: frozen[key] for key in ('release_id', 'source_commit')}
        manifest['bridge_base']['components'] = [row for row in frozen['common_files'] if pack.runtime_path(row['path'])
            or row['path'] in (*pack.COMPONENT_FILES, *pack.GATEWAY_RECOVERY, *pack.FILE_COMPONENT_FILES)]
        actual = {row['path']: row for row in manifest['common_files']}
        if any(actual.get(row['path']) != row for row in manifest['bridge_base']['components']):
            raise ValueError('Bridge components differ from the verified base')
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
    python = source / PYTHON / 'python.exe'
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
    index = release / ('Pocket-Windows-' + manifest['release_id'] + '-update.json')
    if archive.exists() or (base is None and any(path.exists() for path in (index, index.with_suffix('.json.sha256')))):
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
    result = {'package': str(archive), 'bytes': archive.stat().st_size, 'sha256': digest, 'omitted': len(omitted), 'new_application_files': added}
    if base is None:
        atomic_json(index, app_update_index(archive, light, digest))
        index_digest = _sha256_file(index)
        index.with_suffix('.json.sha256').write_text(index_digest + '  ' + index.name + '\n', encoding='ascii')
        result.update(index=str(index), index_sha256=index_digest, index_bytes=index.stat().st_size)
    return result


def app_update_index(archive, manifest, digest):
    """Locate raw compressed members so clients can fetch only files they need."""
    archive = absolute(archive)
    rows = validate_common_records(manifest['common_files'])
    if manifest.get('omitted'):
        raise ValueError('An update index requires a complete application package')
    files = []
    with _pinned_directory(archive.parent, create=False), locked_read(archive) as incoming:
        size = os.fstat(incoming.fileno()).st_size
        with zipfile.ZipFile(incoming) as bundle:
            entries = bundle.infolist()
            expected = {'RELEASE.json', *('app/' + row['path'] for row in rows)}
            if len(entries) != len(expected) or {entry.filename for entry in entries} != expected:
                raise ValueError('Update ZIP differs from its application inventory')
            for row in rows:
                entry = bundle.getinfo('app/' + row['path'])
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or entry.flag_bits & 1:
                    raise ValueError('Unsupported indexed ZIP member')
                incoming.seek(entry.header_offset)
                header = incoming.read(30)
                if len(header) != 30 or header[:4] != b'PK\x03\x04':
                    raise ValueError('Invalid indexed ZIP local header')
                # ZIP64 changes the extra data, not the fixed name/extra length fields.
                offset = entry.header_offset + 30 + int.from_bytes(header[26:28], 'little') + int.from_bytes(header[28:30], 'little')
                if entry.file_size != row['bytes'] or offset + entry.compress_size > size:
                    raise ValueError('Indexed ZIP member differs from inventory')
                files.append({'path': row['path'], 'offset': offset,
                              'compressed_bytes': entry.compress_size, 'method': entry.compress_type})
    release_id = manifest['release_id']
    return {'schema': 1, 'release': manifest,
            'archive': {'url': release_url(release_id, 'Pocket-Windows-' + release_id + '-app.zip'),
                        'bytes': size, 'sha256': digest}, 'files': files}


SOURCES = Path(__file__).resolve().with_name('update_sources.json')
MANIFEST = 'update-manifest.json'
_CNG = '$ErrorActionPreference="Stop"; $c="System.Security.Cryptography."; '
KEYGEN = (_CNG + '$p=New-Object ($c+"CngKeyCreationParameters"); $p.KeyUsage="Signing"; '
          '$p.ExportPolicy=$(if($env:WCP_EXPORTABLE -eq "1"){"AllowPlaintextExport"}else{"None"}); '
          '$k=[System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::ECDsaP256,$env:WCP_KEY,$p); '
          '[Console]::Out.Write([Convert]::ToBase64String($k.Export([System.Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)))')
SIGN = (_CNG + '$d=[Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()); '
        '$k=[System.Security.Cryptography.CngKey]::Open($env:WCP_KEY); $e=New-Object ($c+"ECDsaCng") $k; $s=$e.SignData($d); '
        'if(-not $e.VerifyData($d,$s)){throw "signature self-check failed"}; '
        '[Console]::Out.Write([Convert]::ToBase64String($s)+" "+[Convert]::ToBase64String($k.Export([System.Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)))')
DELETE_KEY = _CNG + '[System.Security.Cryptography.CngKey]::Open($env:WCP_KEY).Delete()'


def update_sources():
    """Publisher hosts and pinned keys live in data, not code: a move edits update_sources.json only."""
    value = json.loads(SOURCES.read_text(encoding='utf-8'))
    if (type(value) is not dict or value.get('schema') != 1 or '{release_id}' not in value.get('release_base', '')
            or not all(asset_url(url) for url in [value['release_base'], *value['feeds']])
            or not value['keys'] or type(value.get('signing_key')) is not str):
        raise ValueError('Invalid update_sources.json')
    return value


def pinned_keys():
    """A launcher shipped with placeholder keys could never verify an update again."""
    keys = update_sources()['keys']
    for key in keys:
        try:
            blob = base64.b64decode(key, validate=True)
        except ValueError:
            blob = b''
        if len(blob) != 72 or not blob.startswith(b'ECS1'):
            raise ValueError('update_sources.json must pin real ECDSA P-256 keys; run keygen first')
    return keys


def release_url(release_id, name):
    return update_sources()['release_base'].format(release_id=release_id) + name


def asset_url(url):
    """Any HTTPS host is transport only: the signed manifest's sizes and SHA-256 authenticate the bytes."""
    if type(url) is not str:
        return False
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return False
    if parsed.scheme == 'https' and parsed.hostname:
        return True
    api = urllib.parse.urlsplit(os.environ.get('POCKET_WINDOWS_UPDATE_API', ''))
    return api.scheme == parsed.scheme == 'http' and api.hostname == parsed.hostname == '127.0.0.1' and api.port == parsed.port


def _cng(script, key, data=b'', exportable=False):
    result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-Command', script], input=data,
                            capture_output=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW,
                            env=os.environ | {'WCP_KEY': key, 'WCP_EXPORTABLE': '1' if exportable else '0'})
    if result.returncode:
        raise RuntimeError('CNG key operation failed: ' + result.stderr.decode(errors='replace')[-800:])
    return result.stdout.decode('ascii').strip()


def keygen(name, exportable=False):
    """Create a signing key in the current user's CNG store; only the public blob leaves it."""
    return {'key': name, 'exportable': exportable, 'public': _cng(KEYGEN, name, exportable=exportable)}


def sign_manifest(release, key=None):
    """Write update-manifest.json: the launcher trusts this signature, never the host it came from."""
    release = absolute(release)
    info = verify_release(release)
    sources = update_sources()
    stem = 'Pocket-Windows-' + info['release_id']

    def asset(name):
        record = file_record(pack.file_under(release, name), name)
        return {'url': release_url(info['release_id'], name), 'bytes': record['bytes'], 'sha256': record['sha256']}
    assets = {'app': asset(stem + '-app.zip')}
    if (release / (stem + '-update-v2.json')).exists():
        assets |= {'update_v2': asset(stem + '-update-v2.json'), 'updater': asset(stem + '-updater.zip')}
    data, public = envelope({'schema': 1, 'release_id': info['release_id'], 'source_commit': info['source_commit'],
                             'feeds': sources['feeds'], 'assets': assets}, key or sources['signing_key'])
    if public not in sources['keys']:
        raise ValueError('Signing key is not pinned in update_sources.json')
    path = release / MANIFEST
    with _pinned_directory(release, create=False), path.open('xb') as stream:
        stream.write(data)
    return {'manifest': str(path), 'release_id': info['release_id'], 'assets': sorted(assets), 'key': public}


def envelope(manifest, key):
    """The exact signed bytes travel with their signature, so no host or second request can mix them up."""
    body = json_bytes(manifest)
    signature, public = _cng(SIGN, key, base64.b64encode(body)).split(' ')
    return json_bytes({'schema': 1, 'manifest': base64.b64encode(body).decode('ascii'), 'signature': signature}), public


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def install_app(archive, sha256, target, probe=False, catalog=None, index_sha256=None):
    """Verify a downloaded light package, unpack it outside the target and apply it as a normal update."""
    target = absolute(target)
    local_stage = absolute(archive).is_relative_to(target)
    stage = directory(target / 'state' / uuid.uuid4().hex) if local_stage else Path(tempfile.mkdtemp(prefix='pocket-windows-update-', dir=absolute(archive).parent))
    try:
        manifest = unpack_app(archive, sha256, stage)
        if catalog is not None:
            import app_update_v2 as parts
            index = parts.read_index(absolute(catalog), index_sha256)
            if any(manifest[key] != index['release'][key] for key in ('schema', 'release_id', 'source_commit')):
                raise ValueError('Full package and chunk catalog provenance differ')
            parts.seed_cache(index, stage / 'app', absolute(target))
            prepared = absolute(target) / 'state' / uuid.uuid4().hex
            parts.prepare(catalog, index_sha256, target, prepared, offline=True)
            return apply_update(prepared, target, probe=True, prepared_sha256=index_sha256)
        return apply_update(stage, target, probe=probe, _local_stage=local_stage)
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


def check_app(archive, sha256, release_id, target=None):
    stage = Path(tempfile.mkdtemp(prefix='pocket-windows-check-', dir=absolute(archive).parent))
    try:
        manifest = unpack_app(archive, sha256, stage)
        if manifest['release_id'] != release_id:
            raise ValueError('Downloaded release differs from the offered version')
        if manifest.get('omitted'):
            if target is None:
                raise ValueError('Automatic updates require a complete application package or a verified target')
            target = absolute(target)
            omitted = set(manifest['omitted'])
            with _pinned_directory(target, create=False):
                for row in manifest['common_files']:
                    if row['path'] not in omitted:
                        continue
                    try:
                        actual = file_record(pack.file_under(target, row['path']), row['path'])
                    except (OSError, ValueError):
                        raise RuntimeError('An omitted update file is missing or unsafe: ' + row['path']) from None
                    if actual != row:
                        raise RuntimeError('An omitted update file differs from the new release: ' + row['path'])
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


def cleanup_download(target, archive, digest, release_id):
    """Remove a completed download only with its matching ownership receipt."""
    target, downloaded = absolute(target), absolute(archive)
    folder = downloaded.parent
    if (downloaded.name != 'app.zip' or folder.parent != target / 'state'
            or not re.fullmatch('app-updater-[a-f0-9]{32}', folder.name) or (target / JOURNAL).exists()):
        return
    with _pinned_directory(folder, create=False):
        marker = read_json(pack.file_under(folder, 'download.json'), 4096)
        if marker != dict(schema=1, release_id=release_id, sha256=digest):
            return
        if {path.name for path in folder.iterdir()} - {'app.zip', 'download.json', 'update.json'}:
            return
        if file_record(pack.file_under(folder, 'app.zip'), 'app.zip')['sha256'] != digest:
            return
        owned = [(path, identity(pack.file_under(folder, path.name))) for path in folder.iterdir()]
        for path, expected in owned:
            remove_owned_file(path, expected)
    folder.rmdir()


def run_app_update(target, pid, created, archive=None, sha256=None, release_id=None, recover=False,
                   prepared=None, index_sha256=None, probe=False, catalog=None):
    target = absolute(target)
    result = {'schema': 1, 'status': 'failed'}
    try:
        wait_for_launcher(target, pid, created)
        if recover:
            rollback_update(target)
            result.update(status='rolled_back', message='Предыдущая версия восстановлена. Можно повторить обновление.')
        else:
            if prepared is not None:
                manifest = prepared_release(absolute(prepared), index_sha256, target)
                if manifest['release_id'] != release_id:
                    raise ValueError('Prepared release differs from the offer')
                installed = apply_update(prepared, target, probe=True, prepared_sha256=index_sha256)
            else:
                check_app(archive, sha256, release_id, target=target)
                installed = install_app(archive, sha256, target, probe=probe, catalog=catalog, index_sha256=index_sha256)
            if installed['release_id'] != release_id:
                raise RuntimeError('Installed version differs from the offered release')
            result.update(status='committed', release_id=release_id, source_commit=installed['source_commit'],
                          message='Обновлено до версии ' + release_id)
            if probe or prepared is not None:
                try:
                    import update_storage
                    result['cleanup'] = update_storage.cleanup(target, installed['backup'], allowed_probe=installed.get('probe'))
                except (OSError, ValueError, RuntimeError):
                    result['cleanup_pending'] = True
    except Exception:
        try:
            directory(target / 'logs')
            with (target / 'logs/update-details.txt').open('w', encoding='utf-8') as log:
                traceback.print_exc(file=log)
        except (OSError, ValueError):
            pass
        if (target / JOURNAL).exists():
            result.update(status='recovery_required', message='Обновление прервано. Нужен откат из сохранённой копии.')
        else:
            result['message'] = 'Обновление не установлено. Прежние файлы сохранены. Подробности: logs/update-details.txt.'
    try:
        directory(target / 'logs')
        atomic_json(target / 'logs/update-result.json', result)
    except (OSError, ValueError):
        pass  # Diagnostic output must never prevent recovery or restart.
    if archive is not None:
        try:
            cleanup_download(target, archive, sha256, release_id)
        except (OSError, ValueError):
            result['download_cleanup_pending'] = True
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
    prepare.add_argument('--components', type=Path, help='Fresh source-accounted Build-HostFiles output')
    prepare.add_argument('--bridge', type=Path, help='Verified release whose immutable components this bridge preserves')
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
    v2 = commands.add_parser('v2-package')
    v2.add_argument('--release', type=Path, required=True)
    v2.add_argument('--source', type=Path, required=True)
    signer = commands.add_parser('sign-manifest')
    signer.add_argument('--release', type=Path, required=True)
    signer.add_argument('--key', help='CNG key name (default: signing_key in update_sources.json)')
    generator = commands.add_parser('keygen')
    generator.add_argument('--key', required=True)
    generator.add_argument('--exportable', action='store_true', help='Backup key the owner exports and keeps offline')
    install = commands.add_parser('install-app')
    install.add_argument('--zip', type=Path, required=True)
    install.add_argument('--sha256', required=True)
    install.add_argument('--target', type=Path, required=True)
    check = commands.add_parser('check-app')
    check.add_argument('--zip', type=Path, required=True)
    check.add_argument('--sha256', required=True)
    check.add_argument('--release-id', required=True)
    check.add_argument('--target', type=Path)
    runner = commands.add_parser('run-app-update')
    runner.add_argument('--target', type=Path, required=True)
    runner.add_argument('--wait-pid', type=int, required=True)
    runner.add_argument('--wait-created', type=int, required=True)
    runner.add_argument('--zip', type=Path)
    runner.add_argument('--sha256')
    runner.add_argument('--release-id')
    runner.add_argument('--recover', action='store_true')
    runner.add_argument('--prepared', type=Path)
    runner.add_argument('--catalog', type=Path)
    runner.add_argument('--index-sha256')
    runner.add_argument('--probe', action='store_true')
    health = commands.add_parser('probe-app')
    health.add_argument('--target', type=Path, required=True)
    health.add_argument('--nonce', required=True)
    rollback = commands.add_parser('rollback')
    rollback.add_argument('--target', type=Path, required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--release', type=Path, required=True)
    publish.add_argument('--target', type=Path, required=True)
    publish.add_argument('--owner-accepted', action='store_true', help='Use only after explicit human Launcher and VM acceptance')
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare_release(args.source, args.output, args.master, args.release_id, args.components, args.bridge)
    elif args.command == 'package':
        result = package_release(args.release, args.source, args.master)
    elif args.command == 'update':
        result = apply_update(args.release, args.target)
    elif args.command == 'app-package':
        result = package_app(args.release, args.base)
    elif args.command == 'v2-package':
        import app_update_v2
        import update_toolkit
        manifest = verify_release(args.release)
        if source_commit(absolute(args.source)) != manifest['source_commit']:
            raise ValueError('Toolkit source differs from this release')
        toolkit = update_toolkit.build(args.source, args.release, manifest['release_id'])
        result = app_update_v2.build(args.release, toolkit['path'])
    elif args.command == 'sign-manifest':
        result = sign_manifest(args.release, args.key)
    elif args.command == 'keygen':
        result = keygen(args.key, args.exportable)
    elif args.command == 'install-app':
        result = install_app(args.zip, args.sha256, args.target)
    elif args.command == 'check-app':
        result = check_app(args.zip, args.sha256, args.release_id, target=args.target)
    elif args.command == 'probe-app':
        result = probe_app(args.target, args.nonce)
    elif args.command == 'run-app-update':
        result = run_app_update(args.target, args.wait_pid, args.wait_created,
                                args.zip, args.sha256, args.release_id, args.recover,
                                args.prepared, args.index_sha256, args.probe, args.catalog)
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
    print(json.dumps(result, ensure_ascii=True, indent=2, default=str))


if __name__ == '__main__':
    main()

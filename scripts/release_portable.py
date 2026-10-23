"""Build one committed release and update application files without replacing user data."""
import argparse
import ast
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pack_existing_runtime as pack
import prepare_runtime_bundle as bundle
import launch_runtime as runtime
from file_snapshot import locked_read, remove_owned_file
from profile_store import _plain, _pinned_directory, _exclusive_file

INFO = 'release-info.json'
JOURNAL = 'state/app-update.json'
JSON_LIMIT = 16 * 1024 * 1024
REQUIRED_RUNTIME = ('runtime/python-3.12.14/python.exe',
                    'runtime/qemu-11.1.0/qemu-system-x86_64.exe',
                    'runtime/qemu-11.1.0/qemu-img.exe',
                    'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd')
MASTER_HASHES = {'work_sha256': '0187e93fce19856d6cd3a72d19b4fae787ce84a28e377ac5daf7f15a520a0143',
                 'vars_sha256': 'eb38666b9ce80badf83fdb773d67d8ce5e8bc662925e71ac9477be51860bf36b'}
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
    return pack.safe_name(name) and (name in ('Launcher.exe', INFO, *pack.APP_FILES,
        *pack.COMPONENT_FILES, *pack.GATEWAY_RECOVERY, *pack.FILE_COMPONENT_FILES)
        or name.startswith(pack.RUNTIME_PREFIXES))


def validate_common_records(records):
    if type(records) is not list or not 1 <= len(records) <= 40000:
        raise ValueError('Invalid application inventory')
    seen = set()
    for row in records:
        if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'} or not common_name(row['path'])
                or row['path'].casefold() in seen or type(row['bytes']) is not int or row['bytes'] < 0
                or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise ValueError('Unsafe or invalid application record')
        seen.add(row['path'].casefold())
    return records


def common_records(root):
    root = absolute(root)
    manifest = read_json(pack.file_under(root, pack.COMPONENT_FILES[0]))
    if type(manifest) is not dict or set(manifest) != {'schema', 'files'} or type(manifest['schema']) is not int or manifest['schema'] != 1:
        raise ValueError('Invalid runtime manifest')
    component_records = validate_common_records(manifest['files'])
    if any(not row['path'].startswith(pack.RUNTIME_PREFIXES) for row in component_records):
        raise ValueError('Runtime manifest includes non-runtime data')
    names = {'Launcher.exe', INFO, *pack.APP_FILES, *pack.COMPONENT_FILES}
    names.update(row['path'] for row in component_records)
    if not set(REQUIRED_RUNTIME) <= names:
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
    if common_records(app) != validate_common_records(manifest.get('common_files')):
        raise ValueError('Application payload differs from release inventory')
    archive_name = 'Windows-Claude-' + info['release_id'] + '.zip'
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
                        result[process['pid']] = process
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    return result


def _journal(target):
    value = read_json(pack.file_under(target, JOURNAL), JSON_LIMIT)
    if (type(value) is not dict or value.get('schema') != 1 or value.get('target') != str(target)
            or not re.fullmatch('app-update-[a-f0-9]{32}', value.get('transaction', ''))
            or type(value.get('files')) is not list or not 1 <= len(value['files']) <= 40000):
        raise ValueError('Invalid update journal; retain it for inspection')
    seen = set()
    for row in value['files']:
        validate_common_records([row['new']])
        if (row.get('path') != row['new']['path'] or row['path'].casefold() in seen
                or row.get('phase') not in ('pending', 'replacing', 'installed', 'restored')):
            raise ValueError('Invalid update journal row')
        seen.add(row['path'].casefold())
        if row['old'] is not None:
            validate_common_records([row['old']])
            if row['old']['path'] != row['path']:
                raise ValueError('Invalid backup destination')
        owned = row.get('new_id')
        if (type(owned) is not dict or set(owned) != {'device', 'file_id'}
                or any(type(owned[key]) is not int for key in owned) or owned['device'] < 0 or owned['file_id'] <= 0):
            raise ValueError('Invalid staged file identity')
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
            previous_bytes = 0
            for row in manifest['common_files']:
                path = target / row['path']
                directory(path.parent)
                pins.enter_context(_pinned_directory(path.parent, create=False))
                _plain(path)
                if path.exists():
                    with _exclusive_file(path, create=False):
                        pass
                    previous_bytes += path.stat().st_size
            needed = sum(row['bytes'] for row in manifest['common_files']) + 2 * previous_bytes + 16 * 1024**2
            if shutil.disk_usage(target).free < needed:
                raise RuntimeError('Insufficient space for update staging, backups and rollback')
            transaction = directory(target / 'state' / ('app-update-' + uuid.uuid4().hex))
            pins.enter_context(_pinned_directory(transaction, create=False))
            journal = {'schema': 1, 'target': str(target), 'transaction': transaction.name,
                       'release_id': manifest['release_id'], 'phase': 'prepared', 'files': []}
            for row in manifest['common_files']:
                path = target / row['path']
                staged = transaction / 'new' / row['path']
                copy_record(release / 'app' / row['path'], staged, row)
                old = file_record(path, row['path']) if path.exists() else None
                if old is not None:
                    copy_record(path, transaction / 'old' / row['path'], old)
                journal['files'].append({'path': row['path'], 'new': row, 'old': old,
                                         'new_id': identity(staged), 'phase': 'pending'})
                pins.enter_context(_pinned_directory(staged.parent, create=False))
            # Reserve the longest phase names before replacing any application file.
            json_bytes(dict(journal, phase='rolled_back',
                            files=[dict(row, phase='replacing') for row in journal['files']]))
            atomic_json(target / JOURNAL, journal)
            try:
                for row in journal['files']:
                    path = target / row['path']
                    actual = file_record(path, row['path']) if path.exists() else None
                    if actual != row['old']:
                        raise RuntimeError('Target changed after backup: ' + row['path'])
                    staged = pack.file_under(transaction / 'new', row['path'])
                    if identity(staged) != row['new_id'] or file_record(staged, row['path']) != row['new']:
                        raise ValueError('Staged update changed before installation')
                    row['phase'] = 'replacing'
                    atomic_json(target / JOURNAL, journal)
                    os.replace(staged, path)
                    row['phase'] = 'installed'
                    atomic_json(target / JOURNAL, journal)
                for row in journal['files']:
                    if file_record(target / row['path'], row['path']) != row['new']:
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
    archive_name = 'Windows-Claude-' + manifest['release_id'] + '.zip'
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
    instructions = ('Windows Claude Portable — ' + manifest['release_id'] + '\n\n'
                    'Распакуй ' + archive_name + ' Проводником в новую папку.\n'
                    'Открой Windows-Claude\\Launcher.exe. Ярлык на рабочий стол создаётся на главной странице.\n'
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
    archive_name = 'Windows-Claude-' + manifest['release_id'] + '.zip'
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

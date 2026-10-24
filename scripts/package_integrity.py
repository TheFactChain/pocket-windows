"""One-time extracted-package integrity check, not a publisher signature."""
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import re
import stat

from file_snapshot import locked_read
from profile_store import ProfileStore, _plain, _pinned_directory
import pack_existing_runtime as pack
import launch_runtime as runtime

MARKER = 'state/package-initial-check.json'
RUNTIME_FILES = ('runtime/python-3.12.14/python.exe',
    'runtime/qemu-11.1.0/qemu-system-x86_64.exe', 'runtime/qemu-11.1.0/qemu-img.exe',
    'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd')


@contextmanager
def _reader(root, name):
    path = root / name
    with _pinned_directory(path.parent, create=False):
        with locked_read(pack.file_under(root, name)) as reader:
            before = os.fstat(reader.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or pack._identity(before) != pack._identity(path.lstat())):
                raise ValueError('Package file identity changed: ' + name)
            _plain(path)
            yield reader
            after = os.fstat(reader.fileno())
            _plain(path)
            if (after.st_nlink != 1 or pack._identity(before) != pack._identity(after)
                    or pack._identity(after) != pack._identity(path.lstat())):
                raise ValueError('Package file changed during verification: ' + name)


def _bounded(reader, maximum=pack.MAX_METADATA):
    raw = reader.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Package metadata is oversized')
    return raw


def _manifest(raw):
    value = pack._json(io.BytesIO(raw))
    if (type(value) is not dict or set(value) != {'schema', 'kind', 'created_utc',
            'encrypted_profiles_included', 'gateway_recovery_included', 'credentials_portability',
            'vm_started_for_packaging', 'second_pc_tested', 'files'}
            or type(value['schema']) is not int or value['schema'] != 2
            or value['kind'] not in ('personal_copy', 'public_distribution')
            or type(value['files']) is not list or not 1 <= len(value['files']) <= 40000
            or type(value['created_utc']) is not str or type(value['credentials_portability']) is not str
            or value['vm_started_for_packaging'] is not False or value['second_pc_tested'] is not False):
        raise ValueError('Invalid package manifest')
    names = set()
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'}
                or not pack.allowed(row['path']) or row['path'].casefold() in names
                or type(row['bytes']) is not int or row['bytes'] < 0
                or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise ValueError('Unsafe, duplicate or invalid package file')
        names.add(row['path'].casefold())
    payload = {row['path'] for row in value['files']}
    required_apps = set(pack.APP_FILES)
    if 'scripts/package_integrity.py' not in payload:
        required_apps -= {'scripts/package_integrity.py', 'scripts/pack_existing_runtime.py'}
    if 'scripts/release_portable.py' not in payload:  # packages from before the light updater
        required_apps -= {'scripts/release_portable.py', 'scripts/prepare_runtime_bundle.py'}
    required = {*pack.DATA_FILES, *pack.PUBLIC_FILES, *required_apps,
                *pack.COMPONENT_FILES, *RUNTIME_FILES, 'gateway/packages/current.json'}
    if (not required <= payload or value['encrypted_profiles_included'] is not (pack.PROFILE_FILE in payload)
            or any(name in payload for name in pack.GATEWAY_RECOVERY) != all(name in payload for name in pack.GATEWAY_RECOVERY)
            or value['gateway_recovery_included'] is not all(name in payload for name in pack.GATEWAY_RECOVERY)):
        raise ValueError('Package manifest is incomplete')
    if value['kind'] == 'public_distribution' and (pack.PROFILE_FILE in payload
            or 'config/files.json' in payload or value['encrypted_profiles_included'] is not False
            or value['credentials_portability'] != 'No connection profiles included.'):
        raise ValueError('Public package manifest includes private data')
    return value['files']


def _cached(root, digest, count):
    path = root / MARKER
    _plain(root / 'state')
    _plain(path)
    if not path.exists():
        return False
    with _reader(root, MARKER) as reader:
        value = pack._json(reader, 4096)
    if (type(value) is not dict or set(value) != {'schema', 'manifest_sha256', 'checked_files'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['checked_files']) is not int or value['checked_files'] < 1
            or type(value['manifest_sha256']) is not str
            or not re.fullmatch('[a-f0-9]{64}', value['manifest_sha256'])):
        raise ValueError('Invalid initial package check marker')
    return value['manifest_sha256'] == digest and value['checked_files'] == count


def _editable(name):
    return name.startswith(('config/', 'profiles/'))


def _legacy_without_manifest(root):
    declaration = root / 'state/package-legacy-format.json'
    _plain(root / 'state')
    _plain(declaration)
    declared = declaration.exists()
    if declared:
        with _reader(root, 'state/package-legacy-format.json') as reader:
            value = pack._json(reader, 4096)
        if (type(value) is not dict or set(value) != {'schema', 'legacy_format'}
                or type(value['schema']) is not int or value['schema'] != 1
                or value['legacy_format'] is not True):
            raise ValueError('Invalid legacy package format declaration')
    _plain(root / 'release-info.json')
    if (root / 'release-info.json').exists():
        with _reader(root, 'release-info.json') as reader:
            release = pack.validate_release_info(pack._json(reader, 4096))
        # 2026.10.23.2 is the first ZIP that requires extracted checksum manifests.
        if tuple(map(int, release['release_id'].split('.'))) >= (2026, 10, 23, 2) and not declared:
            raise ValueError('New release package manifests are missing; extract the complete ZIP')
    return {'ok': True, 'code': 'legacy', 'message': 'У этой прежней копии нет манифеста первичной проверки. Для проверки распакуй ZIP нового выпуска.'}


def verify_initial(root):
    """Verify before the first start; a matching marker deliberately skips later VM changes."""
    root = Path(os.path.abspath(root))
    with _pinned_directory(root, create=False):
        paths = [root / name for name in ('MANIFEST.json', 'MANIFEST.sha256')]
        for path in paths:
            _plain(path)
        present = [path.exists() for path in paths]
        if not any(present):
            return _legacy_without_manifest(root)
        if not all(present):
            raise ValueError('Package manifest pair is incomplete')
        # Keep both metadata files locked until the marker is committed.
        with _reader(root, 'MANIFEST.json') as manifest_reader, _reader(root, 'MANIFEST.sha256') as sums_reader:
            raw = _bounded(manifest_reader)
            rows = _manifest(raw)
            digest = hashlib.sha256(raw).hexdigest()
            sums = ''.join(row['sha256'] + '  ' + row['path'] + '\n' for row in rows)
            expected = (sums + digest + '  MANIFEST.json\n').encode()
            if _bounded(sums_reader) != expected:
                raise ValueError('Package checksum metadata differs from its manifest')
            if not any(row['path'] == 'scripts/package_integrity.py' for row in rows):
                return {'ok': True, 'code': 'legacy', 'message': 'Для первичной проверки этой прежней копии распакуй ZIP нового выпуска.'}
            count = sum(not _editable(row['path']) for row in rows)
            if _cached(root, digest, count):
                return {'ok': True, 'code': 'cached', 'message': 'Первичная проверка целостности комплекта выполнена. Изменения Windows после неё не перепроверяются.'}
            with runtime.ProjectLock(root) as acquired:
                if not acquired or runtime.engine_processes(root):
                    return {'ok': False, 'code': 'busy', 'message': 'Для первичной проверки выключи Windows и дождись завершения служебных процессов.'}
                names = {row['path'] for row in rows}
                for row in rows:
                    name = row['path']
                    if name.startswith('profiles/'):
                        continue
                    with _reader(root, name) as reader:
                        if name.startswith('config/'):
                            pack.validate_public_file(name, reader)
                            continue
                        if name == 'gateway/packages/current.json':
                            pointer = pack.gateway_package._pointer(pack._json(reader, 65536))
                            for version in (pointer['current'], pointer['previous']):
                                if version is not None and not {
                                        'gateway/packages/releases/' + version + '/manifest.json',
                                        'gateway/packages/releases/' + version + '/gateway.qcow2'} <= names:
                                    raise ValueError('Package gateway release is incomplete')
                            reader.seek(0)
                        if os.fstat(reader.fileno()).st_size != row['bytes']:
                            raise ValueError('Package file size mismatch: ' + name)
                        if hashlib.file_digest(reader, 'sha256').hexdigest() != row['sha256']:
                            raise ValueError('Package file checksum mismatch: ' + name)
                # User imports and settings may be created before the first start.
                for name in ('config/windows.json', 'config/viewer.json', 'config/files.json'):
                    _plain(root / name)
                    if name not in names and (root / name).exists():
                        with _reader(root, name) as reader:
                            pack.validate_public_file(name, reader)
                _plain(root / 'profiles')
                _plain(root / pack.PROFILE_FILE)
                if (root / pack.PROFILE_FILE).exists():
                    with _pinned_directory(root / 'profiles', create=False):
                        ProfileStore(root / 'profiles')._read()
                runtime.atomic_json(root / MARKER, {'schema': 1,
                    'manifest_sha256': digest, 'checked_files': count})
                return {'ok': True, 'code': 'verified', 'message': 'Первичная проверка целостности комплекта выполнена. Это проверка файлов по манифесту, а не подпись издателя.'}

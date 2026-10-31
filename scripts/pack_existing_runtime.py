"""Create a personal or profile-free public portable archive without Git."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_config import PYTHON, QEMU
from file_snapshot import locked_read, remove_owned_file
from profile_store import ProfileStore, _pinned_directory, _plain
from runtime_config import validate_settings
import windows_guest_settings
from runtime_config import validate_viewer_settings
import host_file_sharing
import gateway_package
import launch_runtime as runtime

PREFIX = 'Pocket-Windows/'
DATA_FILES = ('images/workvm-empty.qcow2', 'state/workvm-vars.fd')
PUBLIC_FILES = ('Launcher.exe', 'config/runtime.json')
DOCUMENT_FILES = ('НАЧАТЬ.txt', 'third-party.md')
APP_FILES = tuple('scripts/' + name for name in (
    'portable_app.py', 'package_integrity.py', 'pack_existing_runtime.py', 'launch_runtime.py', 'control_guest_install.py',
    'runtime_network_plan.py', 'runtime_config.py', 'windows_hardware.py', 'connection_profile.py',
    'profile_store.py', 'gateway_controller.py', 'gateway_package.py', 'host_network.py', 'app_download.py',
    'gateway_profile.py', 'session_carrier.py', 'file_snapshot.py', 'runtime_logs.py', 'windows_guest_settings.py',
    'host_file_sharing.py', 'file_share_carrier.py', 'clipboard_bridge.py', 'clipboard_files.py', 'clipboard_stage.py', 'window_branding.py',
    'release_portable.py', 'prepare_runtime_bundle.py', 'app_update_v2.py', 'update_storage.py',
    'Start-Runtime.ps1', 'Stop-Runtime.ps1', 'Prepare-Dependencies.ps1', 'Prepare-Host.ps1'))
COMPONENT_FILES = ('runtime/components-manifest.json', 'runtime/components-recovery.zip')
GATEWAY_RECOVERY = ('runtime/gateway-recovery.json', 'runtime/gateway-recovery.zip')
FILE_COMPONENT_FILES = tuple('runtime/host-files/' + name for name in (
    'HostFiles.exe', 'HostFilesGuest.exe', 'SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll',
    'SMBLibrary.LICENSE.txt', 'SMBLibrary-source.zip', 'NOTICE.txt', 'build-manifest.json'))
PROFILE_FILE = 'profiles/profiles.json'
RUNTIME_PREFIXES = (PYTHON + '/', QEMU + '/')
CHUNK = 4 * 1024 * 1024
MAX_METADATA = 16 * 1024 * 1024


def safe_name(name):
    return (type(name) is str and bool(name) and not PurePosixPath(name).is_absolute()
        and all(part and part not in ('.', '..') and not part.endswith(('.', ' '))
                and not re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                and not re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', part, re.I)
                for part in name.split('/')))


def allowed(name):
    return safe_name(name) and (name in (*DATA_FILES, *PUBLIC_FILES, *APP_FILES, *COMPONENT_FILES,
        *GATEWAY_RECOVERY, *FILE_COMPONENT_FILES, *DOCUMENT_FILES, PROFILE_FILE, 'gateway/packages/current.json',
        'config/windows.json', 'config/viewer.json', 'config/files.json', 'release-info.json')
        or application_path(name)
        or re.fullmatch(r'gateway/packages/releases/[a-z0-9][a-z0-9._-]{0,63}/(manifest.json|gateway.qcow2)', name))


def runtime_path(name):
    return safe_name(name) and bool(re.fullmatch(r'runtime/(python|qemu)-[0-9]+(?:\.[0-9]+){1,3}/.+', name))


def application_path(name):
    # Stable update boundary: future code/runtime may change; personal directories never belong here.
    return safe_name(name) and (name in ('Launcher.exe', 'release-info.json', *COMPONENT_FILES,
        *GATEWAY_RECOVERY, *FILE_COMPONENT_FILES)
        or bool(re.fullmatch(r'scripts/[A-Za-z0-9_-]+\.(py|ps1)', name)) or runtime_path(name))


def file_under(root, relative):
    if not safe_name(relative):
        raise ValueError('Unsafe source member path')
    path = root / relative
    for parent in (*reversed(path.parents), path):
        _plain(parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Source must be a regular file without hard links')
    return path


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _optional_file(root, name):
    path = root / name
    for item in (*reversed(path.parents), path):
        _plain(item)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('An optional payload must be a regular file')
    return True


def _json(reader, maximum=MAX_METADATA):
    raw = reader.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Packaging metadata is oversized')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate metadata field')
            result[key] = item
        return result
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)


def validate_release_info(value):
    if (type(value) is not dict or set(value) != {'schema', 'release_id', 'source_commit'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['release_id']) is not str or not re.fullmatch(r'[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,8}', value['release_id'])
            or type(value['source_commit']) is not str or not re.fullmatch('[a-f0-9]{40}', value['source_commit'])):
        raise ValueError('Invalid release provenance')
    return value


def validate_public_file(name, reader):
    if name == 'config/runtime.json':
        validate_settings(_json(reader, 65536))
    elif name == 'config/windows.json':
        windows_guest_settings.validate_settings(_json(reader, 8192))
    elif name == 'config/viewer.json':
        validate_viewer_settings(_json(reader, 16384))
    elif name == 'config/files.json':
        host_file_sharing.validate_settings(_json(reader, 65536))
    elif name == 'release-info.json':
        validate_release_info(_json(reader, 4096))
    elif name == 'Launcher.exe':
        header = reader.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError('Launcher.exe must be a built x64 PE executable')
        offset = int.from_bytes(header[60:64], 'little')
        reader.seek(offset)
        if offset < 64 or reader.read(6) != b'PE\0\0\x64\x86':
            raise ValueError('Launcher.exe must be a built x64 PE executable')
    elif name.endswith('.qcow2'):
        header = reader.read(112)
        if len(header) < 104 or header[:4] != b'QFI\xfb':
            raise ValueError('A standalone qcow2 disk is required')
        version = struct.unpack_from('>I', header, 4)[0]
        backing, length = struct.unpack_from('>QI', header, 8)
        virtual = struct.unpack_from('>Q', header, 24)[0]
        encryption = struct.unpack_from('>I', header, 32)[0]
        incompatible = struct.unpack_from('>Q', header, 72)[0] if version == 3 else 0
        zstd = (incompatible == 8 and len(header) >= 112
                and struct.unpack_from('>I', header, 100)[0] >= 112 and header[104] == 1)
        invalid_compression = (version == 3 and len(header) >= 112
                               and struct.unpack_from('>I', header, 100)[0] >= 112
                               and bool(header[104]) != bool(incompatible & 8))
        if (version not in (2, 3) or backing or length or not virtual or encryption
                or (incompatible and not zstd) or invalid_compression):
            raise ValueError('Disk is dirty, encrypted, unsupported or depends on another file')
    reader.seek(0)


def _validate_component_recovery(reader, records):
    expected = {row['path']: row for row in records}
    try:
        with zipfile.ZipFile(reader) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if (archive.comment or len(names) != len(set(name.casefold() for name in names))
                    or set(names) != set(expected)):
                raise ValueError('Recovery ZIP member set differs from the components manifest')
            for member in members:
                name = member.filename
                kind = stat.S_IFMT(member.external_attr >> 16)
                if (not runtime_path(name) or member.is_dir()
                        or kind not in (0, stat.S_IFREG) or member.external_attr & 0x400
                        or member.flag_bits & 1 or member.comment
                        or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or member.file_size != expected[name]['bytes']):
                    raise ValueError('Invalid components recovery ZIP member')
                with archive.open(member) as content:
                    if hashlib.file_digest(content, 'sha256').hexdigest() != expected[name]['sha256']:
                        raise ValueError('Components recovery checksum mismatch')
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError):
        raise ValueError('Invalid components recovery ZIP') from None
    finally:
        reader.seek(0)


class Snapshot(dict):
    def __init__(self, project, mode='personal'):
        super().__init__()
        self.project, self.expected, self.mode = project, {}, mode


def collect(project, source=None, mode='personal'):
    if mode not in ('personal', 'public'):
        raise ValueError('Unknown archive mode')
    project = Path(os.path.abspath(project))
    if source is not None and not runtime.same_path(source, project):
        raise ValueError('Use --project itself; a separate source repository is no longer supported')
    entries = Snapshot(project, mode)
    def add(name, expected=None, recovery_records=None):
        if not allowed(name) or name.casefold() in {item.casefold() for item in entries}:
            raise ValueError('Unexpected or duplicated payload path')
        path = file_under(project, name)
        with _pinned_directory(path.parent, create=False), locked_read(path) as reader:
            validate_public_file(name, reader)
            if recovery_records is not None:
                _validate_component_recovery(reader, recovery_records)
            info = os.fstat(reader.fileno())
            if _identity(info) != _identity(path.lstat()):
                raise ValueError('Source identity changed during inventory')
            if expected is None:
                expected = {'bytes': info.st_size, 'sha256': None if name.endswith('.qcow2') else
                            hashlib.file_digest(reader, 'sha256').hexdigest()}
            if info.st_size != expected['bytes']:
                raise ValueError('Component size differs from its manifest')
            entries[name] = path
            entries.expected[name] = dict(expected, identity=_identity(info))
    with _pinned_directory(project, create=False):
        for name in (*DATA_FILES, *PUBLIC_FILES, *APP_FILES, COMPONENT_FILES[0]):
            add(name)
        if _optional_file(project, 'release-info.json'):
            add('release-info.json')
        for name in DOCUMENT_FILES:
            if _optional_file(project, name):
                add(name)
        if _optional_file(project, 'config/windows.json'):
            add('config/windows.json')
        if _optional_file(project, 'config/viewer.json'):
            add('config/viewer.json')
        if mode == 'personal' and _optional_file(project, 'config/files.json'):
            add('config/files.json')
        file_components = [_optional_file(project, name) for name in FILE_COMPONENT_FILES]
        if any(file_components) and not all(file_components):
            raise ValueError('File-sharing components must be complete')
        if all(file_components):
            for name in FILE_COMPONENT_FILES:
                add(name)
        with locked_read(entries[COMPONENT_FILES[0]]) as reader:
            components = _json(reader)
        if (type(components) is not dict or set(components) != {'schema', 'files'}
                or type(components['schema']) is not int or components['schema'] != 1
                or type(components['files']) is not list or not 1 <= len(components['files']) <= 30000):
            raise ValueError('Invalid components manifest')
        for row in components['files']:
            if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'}
                    or type(row['path']) is not str or not row['path'].startswith(RUNTIME_PREFIXES)
                    or type(row['bytes']) is not int or row['bytes'] < 0
                    or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
                raise ValueError('Invalid component record')
            add(row['path'], {'bytes': row['bytes'], 'sha256': row['sha256']})
        for name in (PYTHON + '/python.exe', QEMU + '/qemu-system-x86_64.exe',
                     QEMU + '/qemu-img.exe', QEMU + '/share/edk2-x86_64-code.fd'):
            if name not in entries:
                raise ValueError('Required runtime component is absent from the manifest')
        add(COMPONENT_FILES[1], recovery_records=components['files'])
        pointer_name = 'gateway/packages/current.json'
        add(pointer_name)
        with locked_read(entries[pointer_name]) as reader:
            pointer = gateway_package._pointer(_json(reader, 65536))
        for version in (pointer['current'], pointer['previous']):
            if version is None:
                continue
            prefix = 'gateway/packages/releases/' + version + '/'
            add(prefix + 'manifest.json')
            with locked_read(entries[prefix + 'manifest.json']) as reader:
                manifest = gateway_package._manifest(_json(reader, 65536))
            if manifest['version'] != version:
                raise ValueError('Gateway directory and manifest versions differ')
            add(prefix + 'gateway.qcow2', {'bytes': manifest['bytes'], 'sha256': manifest['sha256']})
        if mode == 'personal' and _optional_file(project, PROFILE_FILE):
            # _read validates the entire revision history and never decrypts DPAPI blobs.
            profiles = ProfileStore(project / 'profiles')._read()
            add(PROFILE_FILE)
            with locked_read(entries[PROFILE_FILE]) as reader:
                if _json(reader, 8 * 1024 * 1024) != profiles:
                    raise ValueError('Profiles changed during inventory')
        recovery = [_optional_file(project, name) for name in GATEWAY_RECOVERY] if mode == 'personal' else [False, False]
        if any(recovery) and not all(recovery):
            raise ValueError('Gateway recovery catalog and package must both be present')
        if all(recovery):
            add(GATEWAY_RECOVERY[0])
            with locked_read(entries[GATEWAY_RECOVERY[0]]) as reader:
                catalog = _json(reader, 8192)
            if (type(catalog) is not dict or set(catalog) != {'schema', 'file', 'sha256'}
                    or type(catalog['schema']) is not int or catalog['schema'] != 1
                    or catalog['file'] != 'gateway-recovery.zip'):
                raise ValueError('Invalid gateway recovery catalog')
            gateway_package._digest(catalog['sha256'])
            add(GATEWAY_RECOVERY[1], {'bytes': file_under(project, GATEWAY_RECOVERY[1]).stat().st_size,
                                      'sha256': catalog['sha256']})
    return entries


def write_archive(entries, destination, commit=None):
    if not isinstance(entries, Snapshot):
        raise ValueError('Use a validated project inventory for a personal archive')
    destination = Path(os.path.abspath(destination))
    if not safe_name(destination.name):
        raise ValueError('Unsafe archive filename')
    identity = None
    with runtime.ProjectLock(entries.project) as acquired, _pinned_directory(destination.parent):
        if not acquired or runtime.engine_processes(entries.project):
            raise RuntimeError('Shut down all portable VMs before creating a personal copy')
        try:
            with ExitStack() as locks:
                held = {}
                for name, path in entries.items():
                    if not name.startswith(('scripts/', *RUNTIME_PREFIXES)):
                        locks.enter_context(_pinned_directory(path.parent, create=False))
                        held[name] = locks.enter_context(locked_read(file_under(entries.project, name)))
                manifest = {'schema': 2, 'kind': 'personal_copy' if entries.mode == 'personal' else 'public_distribution',
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'encrypted_profiles_included': PROFILE_FILE in entries,
                    'gateway_recovery_included': all(name in entries for name in GATEWAY_RECOVERY),
                    'credentials_portability': ('Windows DPAPI credentials may need re-entry on another PC or user.'
                        if entries.mode == 'personal' else 'No connection profiles included.'),
                    'vm_started_for_packaging': False, 'second_pc_tested': False, 'files': []}
                with destination.open('xb') as output:
                    info = os.fstat(output.fileno())
                    identity = {'device': info.st_dev, 'file_id': info.st_ino}
                    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                        for name, path in sorted(entries.items()):
                            if not allowed(name):
                                raise ValueError('Unexpected payload path')
                            if entries.mode == 'public' and name in (PROFILE_FILE, 'config/files.json', *GATEWAY_RECOVERY):
                                raise ValueError('Public archives must not include profiles, host paths or duplicate gateway recovery')
                            with ExitStack() as context:
                                context.enter_context(_pinned_directory(path.parent, create=False))
                                reader = held.get(name) or context.enter_context(locked_read(file_under(entries.project, name)))
                                expected = entries.expected[name]
                                if _identity(os.fstat(reader.fileno())) != expected['identity']:
                                    raise ValueError('Source changed after inventory: ' + name)
                                validate_public_file(name, reader)
                                digest, size = hashlib.sha256(), 0
                                info = zipfile.ZipInfo.from_file(path, PREFIX + name)
                                # The Windows disk is zstd-compressed inside and gains nothing; the Debian gateway disk is raw and does.
                                windows_disk = entries.mode == 'public' and name.startswith('images/') and name.endswith('.qcow2')
                                info.compress_type = zipfile.ZIP_STORED if windows_disk else zipfile.ZIP_DEFLATED
                                info._compresslevel = 9 if name.endswith('.qcow2') else 1
                                with archive.open(info, 'w', force_zip64=True) as member:
                                    while chunk := reader.read(CHUNK):
                                        member.write(chunk)
                                        digest.update(chunk)
                                        size += len(chunk)
                                if (size != expected['bytes'] or _identity(path.lstat()) != expected['identity']
                                        or (expected['sha256'] is not None and digest.hexdigest() != expected['sha256'])):
                                    raise ValueError('Source changed or component checksum differs: ' + name)
                                manifest['files'].append({'path': name, 'bytes': size, 'sha256': digest.hexdigest()})
                        raw = (json.dumps(manifest, indent=2, ensure_ascii=True) + '\n').encode()
                        archive.writestr(PREFIX + 'MANIFEST.json', raw)
                        sums = ''.join(row['sha256'] + '  ' + row['path'] + '\n' for row in manifest['files'])
                        archive.writestr(PREFIX + 'MANIFEST.sha256', (sums + hashlib.sha256(raw).hexdigest() + '  MANIFEST.json\n').encode())
            return manifest
        except BaseException:
            if identity is not None:
                remove_owned_file(destination, identity)
            raise


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (len(names) != len(set(name.casefold() for name in names))
                or any(not name.startswith(PREFIX) or not safe_name(name) or info.is_dir()
                       or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1
                       for name, info in zip(names, infos))):
            raise ValueError('Unsafe or duplicate archive members')
        if archive.getinfo(PREFIX + 'MANIFEST.json').file_size > MAX_METADATA:
            raise ValueError('Archive manifest is oversized')
        raw = archive.read(PREFIX + 'MANIFEST.json')
        manifest = _json(io.BytesIO(raw))
        if (type(manifest) is not dict or set(manifest) != {'schema', 'kind', 'created_utc',
                'encrypted_profiles_included', 'gateway_recovery_included', 'credentials_portability',
                'vm_started_for_packaging', 'second_pc_tested', 'files'}
                or type(manifest['schema']) is not int or manifest['schema'] != 2
                or manifest['kind'] not in ('personal_copy', 'public_distribution') or type(manifest['files']) is not list
                or manifest['vm_started_for_packaging'] is not False or manifest['second_pc_tested'] is not False
                or not 1 <= len(manifest['files']) <= 40000):
            raise ValueError('Invalid personal archive manifest')
        rows = manifest['files']
        for row in rows:
            if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'} or not allowed(row['path'])
                    or type(row['bytes']) is not int or row['bytes'] < 0
                    or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
                raise ValueError('Invalid personal archive member')
        expected = {PREFIX + row['path'] for row in rows} | {PREFIX + 'MANIFEST.json', PREFIX + 'MANIFEST.sha256'}
        if set(names) != expected or len(rows) + 2 != len(names):
            raise ValueError('Archive member set differs from its manifest')
        payload = {row['path'] for row in rows}
        if manifest['kind'] == 'public_distribution' and (
                PROFILE_FILE in payload or 'config/files.json' in payload or manifest['encrypted_profiles_included'] is not False
                or manifest['credentials_portability'] != 'No connection profiles included.'):
            raise ValueError('Public archives must not include profiles')
        if (not set((*DATA_FILES, *PUBLIC_FILES, *APP_FILES, *COMPONENT_FILES, 'gateway/packages/current.json')) <= payload
                or manifest['encrypted_profiles_included'] is not (PROFILE_FILE in payload)
                or any(name in payload for name in GATEWAY_RECOVERY) != all(name in payload for name in GATEWAY_RECOVERY)
                or manifest['gateway_recovery_included'] is not all(name in payload for name in GATEWAY_RECOVERY)):
            raise ValueError('Personal archive is incomplete or has incorrect profile metadata')
        sums = ''.join(row['sha256'] + '  ' + row['path'] + '\n' for row in rows)
        if archive.read(PREFIX + 'MANIFEST.sha256') != (sums + hashlib.sha256(raw).hexdigest() + '  MANIFEST.json\n').encode():
            raise ValueError('Checksum manifest mismatch')
        for row in rows:
            member = archive.getinfo(PREFIX + row['path'])
            if member.file_size != row['bytes']:
                raise ValueError('Member size mismatch')
            with archive.open(member) as reader:
                digest = hashlib.file_digest(reader, 'sha256').hexdigest()
            if digest != row['sha256']:
                raise ValueError('Member hash mismatch')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--source-repo', type=Path, help='Deprecated: only --project itself is accepted')
    parser.add_argument('--commit', help='Deprecated; Git and source commits are not required')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('personal', 'public'), default='personal')
    args = parser.parse_args()
    project = Path(os.path.abspath(args.project))
    destination = Path(os.path.abspath(args.output))
    if not safe_name(destination.name):
        raise ValueError('Unsafe archive filename')
    partial = destination.with_suffix(destination.suffix + '.partial')
    sidecars = (destination.with_suffix(destination.suffix + '.sha256'), destination.with_suffix(destination.suffix + '.manifest.json'))
    with _pinned_directory(destination.parent):
        if any(path.exists() or path.is_symlink() for path in (destination, partial, *sidecars)):
            raise FileExistsError('Output already exists; no file is overwritten')
        entries = collect(project, args.source_repo, args.mode)
        total = sum(row['bytes'] for row in entries.expected.values())
        if shutil.disk_usage(destination.parent).free < total + 2 * 1024 ** 3:
            raise RuntimeError('Insufficient free space for a conservative local ZIP bound')
        partial_identity = None
        try:
            manifest = write_archive(entries, partial)
            with locked_read(partial) as reader:
                info = os.fstat(reader.fileno())
                partial_identity = {'device': info.st_dev, 'file_id': info.st_ino}
                if verify_archive(partial) != manifest:
                    raise ValueError('Archive manifest differs from the generated manifest')
        except BaseException:
            if partial_identity is not None:
                remove_owned_file(partial, partial_identity)
            raise
        os.rename(partial, destination)
        with locked_read(destination) as reader:
            digest = hashlib.file_digest(reader, 'sha256').hexdigest()
        with sidecars[0].open('x', encoding='utf-8') as writer:
            writer.write(digest + '  ' + destination.name + '\n')
        summary = {'zip': str(destination), 'zip_bytes': destination.stat().st_size,
                   'zip_sha256': digest, 'payload_bytes': total, 'payload_files': len(entries),
                   'kind': manifest['kind'], 'encrypted_profiles_included': PROFILE_FILE in entries,
                   'credentials_portability': manifest['credentials_portability'],
                   'crc_and_sha256_verified': True, 'second_pc_tested': False, 'vm_started_for_packaging': False}
        with sidecars[1].open('x', encoding='utf-8') as writer:
            json.dump({'summary': summary, 'manifest': manifest}, writer, indent=2)
            writer.write('\n')
        print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()

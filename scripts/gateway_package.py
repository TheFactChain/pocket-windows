"""Offline, hash-pinned gateway packages. Never accesses Windows or profile data."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import uuid
import zipfile
from file_snapshot import locked_read
from profile_store import _pinned_directory, _plain
import launch_runtime as runtime

MAX_DISK = 8 * 1024**3
MAX_VIRTUAL = 16 * 1024**3


def _version(value):
    if (not isinstance(value, str) or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,63}', value)
            or value.endswith('.') or value.split('.')[0] in {'con', 'prn', 'aux', 'nul',
                *('com' + str(i) for i in range(1, 10)), *('lpt' + str(i) for i in range(1, 10))}):
        raise ValueError('Invalid gateway version')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('A trusted SHA256 is required')
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _json(stream):
    data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('Gateway metadata exceeds 64 KiB')
    return json.loads(data, object_pairs_hook=_object)


def _manifest(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'session_schema', 'version', 'bytes', 'sha256', 'virtual_bytes'}:
        raise ValueError('Invalid gateway manifest')
    if any(type(value[key]) is not int or value[key] != 1 for key in ('schema', 'session_schema')):
        raise ValueError('Incompatible gateway schema')
    _version(value['version'])
    _digest(value['sha256'])
    if type(value['bytes']) is not int or not 104 <= value['bytes'] <= MAX_DISK:
        raise ValueError('Invalid gateway disk size')
    if type(value['virtual_bytes']) is not int or not 1 <= value['virtual_bytes'] <= MAX_VIRTUAL:
        raise ValueError('Invalid gateway virtual size')
    return value


def _pointer(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'current', 'previous'} or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Invalid gateway activation pointer')
    _version(value['current'])
    if value['previous'] is not None:
        _version(value['previous'])
        if value['previous'] == value['current']:
            raise ValueError('Invalid previous gateway')
    return value


def _read(path):
    _plain(path)
    with locked_read(path) as stream:
        _plain(path)
        return _json(stream)


def _atomic(path, value):
    _plain(path)
    runtime.atomic_json(path, value)


def _check_header(stream, manifest):
    header = stream.read(104)
    if len(header) < 104 or header[:4] != b'QFI\xfb':
        raise ValueError('Gateway requires a standalone qcow2 disk')
    version, = struct.unpack_from('>I', header, 4)
    backing, backing_size = struct.unpack_from('>QI', header, 8)
    virtual, = struct.unpack_from('>Q', header, 24)
    encryption, = struct.unpack_from('>I', header, 32)
    incompatible = struct.unpack_from('>Q', header, 72)[0] if version == 3 else 0
    if version not in (2, 3) or backing or backing_size or encryption or incompatible or virtual != manifest['virtual_bytes']:
        raise ValueError('Gateway disk is incompatible, dirty or has a backing file')


def _check_disk(stream, manifest):
    _check_header(stream, manifest)
    stream.seek(0)
    if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
        raise ValueError('Gateway disk checksum mismatch')
    if stream.tell() != manifest['bytes']:
        raise ValueError('Gateway disk size mismatch')
    stream.seek(0)


@contextmanager
def _release(base, version):
    directory = base / 'releases' / _version(version)
    with _pinned_directory(directory, create=False), ExitStack() as held:
        manifest_path, disk = directory / 'manifest.json', directory / 'gateway.qcow2'
        for path in (manifest_path, disk):
            _plain(path)
        metadata = held.enter_context(locked_read(manifest_path))
        manifest = _manifest(_json(metadata))
        if manifest['version'] != version:
            raise ValueError('Gateway directory version mismatch')
        reader = held.enter_context(locked_read(disk))
        for path in (manifest_path, disk):
            _plain(path)
        _check_disk(reader, manifest)
        yield {'schema': 1, 'version': version, 'disk': disk, 'sha256': manifest['sha256'],
               'bytes': manifest['bytes']}


@contextmanager
def open_current(root):
    base = Path(root).absolute() / 'gateway/packages'
    with _pinned_directory(base, create=False):
        path = base / 'current.json'
        _plain(path)
        with locked_read(path) as pointer:
            _plain(path)
            current = _pointer(_json(pointer))
            with _release(base, current['current']) as package:
                yield package


def inspect_current(root):
    """Bounded structural inspection only; callers must still open_current before use."""
    base = Path(root).absolute() / 'gateway/packages'
    with _pinned_directory(base, create=False):
        current = _pointer(_read(base / 'current.json'))
        directory = base / 'releases' / current['current']
        with _pinned_directory(directory, create=False):
            manifest = _manifest(_read(directory / 'manifest.json'))
            if manifest['version'] != current['current']:
                raise ValueError('Gateway directory version mismatch')
            disk = directory / 'gateway.qcow2'
            _plain(disk)
            with locked_read(disk) as reader:
                _plain(disk)
                if os.fstat(reader.fileno()).st_size != manifest['bytes']:
                    raise ValueError('Gateway disk size mismatch')
                _check_header(reader, manifest)
            return {'schema': 1, 'version': manifest['version'], 'disk': disk,
                    'bytes': manifest['bytes'], 'hash_verified': False, 'verification': 'on_start'}


def _recover(base, current, allow_damaged=False):
    path = base / 'update.json'
    _plain(path)
    if not path.exists():
        return
    journal = _read(path)
    if (not isinstance(journal, dict) or set(journal) != {'schema', 'phase', 'old', 'new'}
            or type(journal['schema']) is not int or journal['schema'] != 1
            or journal['phase'] not in ('prepared', 'committed', 'aborted')):
        raise ValueError('Unknown gateway update journal; preserve files for inspection')
    new = _pointer(journal['new'])
    old = _pointer(journal['old']) if journal['old'] is not None else None
    if current not in (old, new):
        raise ValueError('Gateway pointer and update journal disagree')
    if journal['phase'] == 'prepared':
        # Never roll back a pointer or remove files after an ambiguous interruption.
        if not allow_damaged:
            with _release(base, new['current']):
                pass
        journal['phase'] = 'committed' if current == new else 'aborted'
        _atomic(path, journal)
    elif journal['phase'] == 'committed' and current != new:
        raise ValueError('Committed gateway pointer changed unexpectedly')
    elif journal['phase'] == 'aborted' and current != old:
        raise ValueError('Aborted gateway pointer changed unexpectedly')


def _retire(base, version):
    directory = base / 'releases' / _version(version)
    with _release(base, version):
        if {path.name for path in directory.iterdir()} != {'manifest.json', 'gateway.qcow2'}:
            return False
    # Explicit filenames only; no recursive deletion or following unknown content.
    with _pinned_directory(directory, create=False):
        for name in ('gateway.qcow2', 'manifest.json'):
            path = directory / name
            _plain(path)
            path.unlink()
    directory.rmdir()
    return True


def install_package(root, source, expected_sha256):
    return _install(root, source, expected_sha256, repair=False)


def repair_package(root, source, expected_sha256):
    """Explicit offline repair; damaged releases are quarantined, never rollback candidates."""
    return _install(root, source, expected_sha256, repair=True)


def _archive_manifest(archive, base):
    entries = archive.infolist()
    if len(entries) != 2 or {entry.filename for entry in entries} != {'manifest.json', 'gateway.qcow2'}:
        raise ValueError('Gateway package has unsupported members')
    for entry in entries:
        mode = entry.external_attr >> 16
        if (entry.flag_bits & 1 or entry.is_dir() or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
            raise ValueError('Gateway archive contains an unsafe entry')
    info = archive.getinfo('manifest.json')
    if info.file_size > 65536:
        raise ValueError('Gateway metadata too large')
    with archive.open(info) as stream:
        manifest = _manifest(_json(stream))
    if archive.getinfo('gateway.qcow2').file_size != manifest['bytes']:
        raise ValueError('Gateway archive disk size mismatch')
    if shutil.disk_usage(base).free < manifest['bytes'] + 64 * 1024**2:
        raise ValueError('Insufficient free space for the gateway update')
    return manifest


def _stage_archive(base, archive, manifest):
    staging = base / 'releases' / ('.staging-' + uuid.uuid4().hex)
    staging.mkdir()
    with _pinned_directory(staging, create=False):
        disk = staging / 'gateway.qcow2'
        with archive.open('gateway.qcow2') as stream, disk.open('xb') as output:
            copied = 0
            while chunk := stream.read(1024**2):
                copied += len(chunk)
                if copied > manifest['bytes']:
                    raise ValueError('Gateway archive expands beyond its declared size')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        with locked_read(disk) as stream:
            _check_disk(stream, manifest)
        _atomic(staging / 'manifest.json', manifest)
    return staging


def _repair_journal(base):
    path = base / 'repair.json'
    _plain(path)
    if not path.exists():
        return None
    journal = _read(path)
    if (not isinstance(journal, dict) or set(journal) != {'schema', 'phase', 'package_sha256', 'old', 'new', 'staging', 'quarantines'}
            or type(journal['schema']) is not int or journal['schema'] != 1
            or journal['phase'] not in ('prepared', 'committed')):
        raise ValueError('Unknown gateway repair journal; preserve files for inspection')
    _digest(journal['package_sha256'])
    if journal['old'] is not None:
        _pointer(journal['old'])
    _pointer(journal['new'])
    if not isinstance(journal['staging'], str) or not re.fullmatch(r'\.staging-[a-f0-9]{32}', journal['staging']):
        raise ValueError('Invalid repair staging path')
    if not isinstance(journal['quarantines'], list) or len(journal['quarantines']) > 3:
        raise ValueError('Invalid repair quarantine list')
    versions, destinations = set(), set()
    for item in journal['quarantines']:
        if (not isinstance(item, dict) or set(item) != {'version', 'destination'}
                or not isinstance(item['destination'], str) or not re.fullmatch('[a-f0-9]{32}', item['destination'])):
            raise ValueError('Invalid repair quarantine path')
        _version(item['version'])
        if item['version'] in versions or item['destination'] in destinations:
            raise ValueError('Duplicate repair quarantine')
        versions.add(item['version'])
        destinations.add(item['destination'])
    return journal


def _repair_locked(base, reader, expected_sha256, current, pending):
    with _pinned_directory(base / 'quarantine'):
        with zipfile.ZipFile(reader) as archive:
            manifest = _archive_manifest(archive, base)
            version = manifest['version']
            if pending and pending['phase'] == 'prepared':
                if pending['package_sha256'] != expected_sha256 or pending['new']['current'] != version:
                    raise ValueError('Resume gateway repair with the same verified package')
                if current not in (pending['old'], pending['new']):
                    raise ValueError('Repair pointer changed unexpectedly')
                journal = pending
                staging = base / 'releases' / journal['staging']
                _plain(staging)
                # Recheck the original incoming bytes even when resuming an existing staging directory.
                with archive.open('gateway.qcow2') as stream:
                    _check_disk(stream, manifest)
            else:
                # Reconcile only a structurally known update journal, without trusting damaged disks.
                _recover(base, current, allow_damaged=True)
                candidates = [] if current is None else [current['current'], current['previous']]
                if version not in candidates:
                    candidates.append(version)
                valid, bad = [], []
                for candidate in dict.fromkeys(candidate for candidate in candidates if candidate):
                    path = base / 'releases' / candidate
                    _plain(path)
                    try:
                        with _release(base, candidate):
                            pass
                        valid.append(candidate)
                    except (OSError, ValueError):
                        if path.exists():
                            bad.append({'version': candidate, 'destination': uuid.uuid4().hex})
                if version in valid and _read(base / 'releases' / version / 'manifest.json') != manifest:
                    raise ValueError('Cannot replace a valid gateway with different content under the same version')
                previous = next((candidate for candidate in valid if candidate != version), None)
                if version in valid:
                    with archive.open('gateway.qcow2') as stream:
                        _check_disk(stream, manifest)
                    staging = base / 'releases' / ('.staging-' + uuid.uuid4().hex)
                else:
                    staging = _stage_archive(base, archive, manifest)
                journal = {'schema': 1, 'phase': 'prepared', 'package_sha256': expected_sha256,
                           'old': current, 'new': {'schema': 1, 'current': version, 'previous': previous},
                           'staging': staging.name, 'quarantines': bad}
                _atomic(base / 'repair.json', journal)
        target = base / 'releases' / version
        if staging.exists():
            with _pinned_directory(staging, create=False):
                if _read(staging / 'manifest.json') != manifest:
                    raise ValueError('Repair staging manifest changed')
                _plain(staging / 'gateway.qcow2')
                with locked_read(staging / 'gateway.qcow2') as stream:
                    _check_disk(stream, manifest)
        else:
            with _release(base, version):
                if _read(target / 'manifest.json') != manifest:
                    raise ValueError('Repair target changed')
        for item in journal['quarantines']:
            damaged = base / 'releases' / item['version']
            destination = base / 'quarantine' / item['destination']
            _plain(damaged)
            _plain(destination)
            if destination.exists():
                if not destination.is_dir():
                    raise ValueError('Repair quarantine changed')
                continue
            if damaged.exists():
                damaged.rename(destination)
        _plain(target)
        if not target.exists():
            staging.rename(target)
        with _release(base, version):
            if _read(target / 'manifest.json') != manifest:
                raise ValueError('Repair target content mismatch')
        if journal['new']['previous']:
            with _release(base, journal['new']['previous']):
                pass
        _atomic(base / 'current.json', journal['new'])
        _atomic(base / 'update.json', {'schema': 1, 'phase': 'committed', 'old': current, 'new': journal['new']})
        journal['phase'] = 'committed'
        _atomic(base / 'repair.json', journal)
        cleanup_pending = bool(journal['quarantines'])
        if journal['old']:
            for obsolete in (journal['old']['current'], journal['old']['previous']):
                if obsolete and obsolete not in (version, journal['new']['previous']):
                    path = base / 'releases' / obsolete
                    if path.exists():
                        try:
                            cleanup_pending |= not _retire(base, obsolete)
                        except (OSError, ValueError):
                            cleanup_pending = True
        with _release(base, version) as installed:
            return {**installed, 'previous': journal['new']['previous'], 'cleanup_pending': cleanup_pending,
                    'quarantined': [base / 'quarantine' / item['destination'] for item in journal['quarantines']]}


def _install(root, source, expected_sha256, repair):
    root, source = Path(root).absolute(), Path(source).absolute()
    _digest(expected_sha256)
    with _pinned_directory(root, create=False), _pinned_directory(root / 'state'), ExitStack() as held:
        _plain(root / 'state/runtime.lock')
        acquired = held.enter_context(runtime.ProjectLock(root))
        if not acquired or runtime.engine_processes(root):
            raise RuntimeError('Stop the portable virtual machines before updating the gateway')
        for directory in (root / 'gateway', root / 'gateway/packages', root / 'gateway/packages/releases'):
            held.enter_context(_pinned_directory(directory))
        base = root / 'gateway/packages'
        held.enter_context(_pinned_directory(source.parent, create=False))
        _plain(source)
        reader = held.enter_context(locked_read(source))
        _plain(source)
        if source.stat().st_size > MAX_DISK + 1024**2 or hashlib.file_digest(reader, 'sha256').hexdigest() != expected_sha256:
            raise ValueError('Untrusted gateway package checksum or size')
        reader.seek(0)
        pointer_path = base / 'current.json'
        _plain(pointer_path)
        current = _pointer(_read(pointer_path)) if pointer_path.exists() else None
        pending = _repair_journal(base)
        if repair:
            return _repair_locked(base, reader, expected_sha256, current, pending)
        if pending and pending['phase'] == 'prepared':
            raise RuntimeError('Finish the interrupted gateway repair before updating')
        _recover(base, current)
        if current:
            with _release(base, current['current']):
                pass
            if current['previous']:
                with _release(base, current['previous']):
                    pass
        with zipfile.ZipFile(reader) as archive:
            manifest = _archive_manifest(archive, base)
            disk_info = archive.getinfo('gateway.qcow2')
            version = manifest['version']
            target = base / 'releases' / version
            _plain(target)
            if target.exists():
                with _release(base, version) as existing:
                    existing_manifest = _read(target / 'manifest.json')
                    if existing_manifest != manifest:
                        raise ValueError('Gateway version already exists with different content')
                with archive.open(disk_info) as stream:
                    _check_disk(stream, manifest)
            else:
                staging = _stage_archive(base, archive, manifest)
                staging.rename(target)
        if current and current['current'] == version:
            with _release(base, version) as installed:
                return {**installed, 'previous': current['previous'], 'cleanup_pending': False}
        new = {'schema': 1, 'current': version, 'previous': current['current'] if current else None}
        journal = {'schema': 1, 'phase': 'prepared', 'old': current, 'new': new}
        _atomic(base / 'update.json', journal)
        _atomic(pointer_path, new)
        journal['phase'] = 'committed'
        _atomic(base / 'update.json', journal)
        pending = False
        if current and current['previous'] and current['previous'] not in (version, new['previous']):
            try:
                pending = not _retire(base, current['previous'])
            except (OSError, ValueError):
                pending = True
        with _release(base, version) as installed:
            return {**installed, 'previous': new['previous'], 'cleanup_pending': pending}

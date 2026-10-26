"""Content-addressed v2 updates; installed files are only read until transaction commit."""
import argparse
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_portable as release
from file_snapshot import locked_read, remove_owned_file
from profile_store import _pinned_directory, _plain

CHUNK = 4 * 1024 * 1024
LIMIT = 12 * 1024**3
ARCHIVE_LIMIT = 8 * 1024**3
EXCLUDED = {'runtime/components-recovery.zip', *release.pack.GATEWAY_RECOVERY}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Invalid SHA256')
    return value


def _number(value, maximum=LIMIT, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid bounded byte count')
    return value


def _url(url, version, suffix):
    if type(url) is not str:
        return False
    expected = ('https://github.com/nekontritce11123-lab/pocket-windows/releases/download/v'
                + version + '/Pocket-Windows-' + version + suffix)
    if url == expected:
        return True
    api = urllib.parse.urlsplit(os.environ.get('POCKET_WINDOWS_UPDATE_API', ''))
    parsed = urllib.parse.urlsplit(url)
    return (api.scheme == parsed.scheme == 'http' and api.hostname == parsed.hostname == '127.0.0.1'
            and api.port == parsed.port and parsed.username is None and parsed.password is None and not parsed.fragment)


def _validate(value):
    if (type(value) is not dict or set(value) != {'schema', 'min_updater', 'release', 'archive', 'toolkit', 'files', 'chunks', 'gateway'}
            or type(value['schema']) is not int or value['schema'] != 2
            or type(value['min_updater']) is not int or value['min_updater'] != 2):
        raise ValueError('Invalid v2 update schema')
    info = release.validate_release_info(value['release'])
    for key, suffix in (('archive', '-chunks.zip'), ('toolkit', '-updater.zip')):
        item = value[key]
        if type(item) is not dict or set(item) != {'url', 'bytes', 'sha256'} or not _url(item['url'], info['release_id'], suffix):
            raise ValueError('Invalid update asset identity')
        _number(item['bytes'], ARCHIVE_LIMIT, minimum=1)
        _digest(item['sha256'])
    if type(value['chunks']) is not list or not 1 <= len(value['chunks']) <= 100000:
        raise ValueError('Invalid chunk inventory')
    chunks, intervals = {}, []
    for row in value['chunks']:
        if type(row) is not dict or set(row) != {'sha256', 'bytes', 'offset', 'compressed_bytes', 'method'}:
            raise ValueError('Invalid chunk record')
        sha = _digest(row['sha256'])
        _number(row['bytes'], CHUNK, 1)
        _number(row['offset'], value['archive']['bytes'])
        _number(row['compressed_bytes'], CHUNK + 65536, 1)
        if (sha in chunks or type(row['method']) is not int or row['method'] not in (0, 8)
                or row['offset'] + row['compressed_bytes'] > value['archive']['bytes']
                or row['method'] == 0 and row['compressed_bytes'] != row['bytes']):
            raise ValueError('Invalid chunk range')
        chunks[sha] = row
        intervals.append((row['offset'], row['offset'] + row['compressed_bytes']))
    intervals.sort()
    if any(b[0] < a[1] for a, b in zip(intervals, intervals[1:])):
        raise ValueError('Overlapping chunk ranges')
    files, referenced, expanded = set(), set(), 0
    if type(value['files']) is not list or not 1 <= len(value['files']) <= 40000:
        raise ValueError('Invalid file inventory')
    gateway = value['gateway']
    gateway_names = set()
    if gateway is not None:
        release.pack.gateway_package._manifest(gateway)
        prefix = 'gateway/packages/releases/' + gateway['version'] + '/'
        gateway_names = {prefix + 'gateway.qcow2', prefix + 'manifest.json'}
    for row in value['files']:
        if type(row) is not dict or set(row) != {'path', 'bytes', 'sha256', 'chunks'}:
            raise ValueError('Invalid logical file')
        name = row['path']
        if (type(name) is not str or name in EXCLUDED or not (release.common_name(name) or name in gateway_names)
                or name.casefold() in files):
            raise ValueError('Unsafe or duplicate update file')
        files.add(name.casefold())
        _number(row['bytes'])
        _digest(row['sha256'])
        ids = row['chunks']
        if type(ids) is not list or len(ids) != (row['bytes'] + CHUNK - 1)//CHUNK:
            raise ValueError('Invalid file chunks')
        size = 0
        for number, sha in enumerate(ids):
            _digest(sha)
            if sha not in chunks or chunks[sha]['bytes'] != min(CHUNK, row['bytes'] - number*CHUNK):
                raise ValueError('File chunk size mismatch')
            size += chunks[sha]['bytes']
            referenced.add(sha)
        if size != row['bytes'] or not size and row['sha256'] != _sha(b''):
            raise ValueError('Invalid file size')
        expanded += size
    if (expanded > LIMIT or referenced != set(chunks) or release.INFO not in files
            or not gateway_names <= {row['path'] for row in value['files']}):
        raise ValueError('Incomplete update inventory')
    if gateway:
        disk = next(row for row in value['files'] if row['path'].endswith('/gateway.qcow2'))
        if disk['bytes'] != gateway['bytes'] or disk['sha256'] != gateway['sha256']:
            raise ValueError('Gateway and update inventory differ')
    if any('/'.join(name.split('/')[:number]) in files
           for name in files for number in range(1, len(name.split('/')))):
        raise ValueError('Conflicting update paths')
    return value


def read_index(index_path, sha256):
    path = release.absolute(index_path)
    _digest(sha256)
    with _pinned_directory(path.parent, create=False), locked_read(release.pack.file_under(path.parent, path.name)) as stream:
        raw = stream.read(release.JSON_LIMIT + 1)
    if len(raw) > release.JSON_LIMIT or _sha(raw) != sha256:
        raise ValueError('Index checksum mismatch')
    return _validate(release.pack._json(io.BytesIO(raw)))


def build(release_path, toolkit_path, base_url=None):
    root, toolkit = map(release.absolute, (release_path, toolkit_path))
    manifest = release.verify_release(root)
    if manifest.get('omitted'):
        raise ValueError('V2 build requires a complete verified release')
    info = {key: manifest[key] for key in ('schema', 'release_id', 'source_commit')}
    stem = 'Pocket-Windows-' + info['release_id']
    archive_path = root/(stem + '-chunks.zip')
    index_path = root/(stem + '-update-v2.json')
    toolkit_record = release.file_record(release.pack.file_under(toolkit.parent, toolkit.name), toolkit.name)
    base_url = base_url or ('https://github.com/nekontritce11123-lab/pocket-windows/releases/download/v' + info['release_id'])
    files, chunks = [], {}
    gateway = None
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        def add(name, stream, expected=None):
            ids, digest, size = [], hashlib.sha256(), 0
            while data := stream.read(CHUNK):
                sha = _sha(data)
                ids.append(sha)
                size += len(data)
                digest.update(data)
                if sha not in chunks:
                    archive.writestr(sha, data)
                    member = archive.getinfo(sha)
                    # Force no extra fields for these bounded members; read actual header below.
                    chunks[sha] = {'sha256': sha, 'bytes': len(data), 'offset': member.header_offset,
                                   'compressed_bytes': member.compress_size, 'method': member.compress_type}
            row = {'path': name, 'bytes': size, 'sha256': digest.hexdigest(), 'chunks': ids}
            if expected and any(row[key] != expected[key] for key in ('bytes', 'sha256')):
                raise ValueError('Release changed while building chunks')
            files.append(row)
        for row in manifest['common_files']:
            if row['path'] not in EXCLUDED:
                path = release.pack.file_under(root/'app', row['path'])
                with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
                    add(row['path'], stream, row)
        if any(row['path'] == release.pack.GATEWAY_RECOVERY[1] for row in manifest['common_files']):
            path = release.pack.file_under(root/'app', release.pack.GATEWAY_RECOVERY[1])
            with locked_read(path) as stream, zipfile.ZipFile(stream) as recovery:
                gateway = release.pack.gateway_package._archive_manifest(recovery)
                prefix = 'gateway/packages/releases/' + gateway['version'] + '/'
                for name in ('manifest.json', 'gateway.qcow2'):
                    with recovery.open(name) as incoming:
                        add(prefix + name, incoming, gateway if name == 'gateway.qcow2' else None)
    with locked_read(archive_path) as stream:
        for row in chunks.values():
            stream.seek(row['offset'])
            header = stream.read(30)
            row['offset'] += 30 + struct.unpack_from('<H', header, 26)[0] + struct.unpack_from('<H', header, 28)[0]
    archive_record = release.file_record(archive_path, archive_path.name)
    value = {'schema': 2, 'min_updater': 2, 'release': info,
             'archive': {key: archive_record[key] for key in ('bytes', 'sha256')},
             'toolkit': {key: toolkit_record[key] for key in ('bytes', 'sha256')},
             'files': files, 'chunks': list(chunks.values()), 'gateway': gateway}
    value['archive']['url'] = base_url.rstrip('/') + '/' + archive_path.name
    value['toolkit']['url'] = base_url.rstrip('/') + '/' + stem + '-updater.zip'
    _validate(value)
    raw = release.json_bytes(value)
    with index_path.open('xb') as stream:
        stream.write(raw)
    digest = _sha(raw)
    checksum = index_path.with_name(index_path.name + '.sha256')
    with checksum.open('x', encoding='ascii') as stream:
        stream.write(digest + '  ' + index_path.name + '\n')
    with archive_path.with_name(archive_path.name + '.sha256').open('x', encoding='ascii') as stream:
        stream.write(archive_record['sha256'] + '  ' + archive_path.name + '\n')
    return {'index': index_path, 'index_sha256': digest, 'index_bytes': len(raw),
            'index_checksum': checksum, 'archive': archive_path, 'archive_sha256': archive_record['sha256'],
            'archive_bytes': archive_record['bytes'], 'toolkit': toolkit, 'toolkit_sha256': toolkit_record['sha256'],
            'toolkit_bytes': toolkit_record['bytes']}


def _decode(data, row, method=8):
    if method == 8:
        decoder = zlib.decompressobj(-15)
        raw = decoder.decompress(data, row['bytes'] + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Invalid compressed chunk')
    else:
        raw = data
    if len(raw) != row['bytes'] or _sha(raw) != row['sha256']:
        raise ValueError('Chunk checksum mismatch')
    return raw


def _cache_read(cache, row):
    path = cache/row['sha256']
    _plain(path)
    if not path.exists():
        return None
    try:
        with _pinned_directory(path.parent, create=False), locked_read(release.pack.file_under(cache, path.name)) as stream:
            data = stream.read(CHUNK + 65537)
        return _decode(data, row)
    except (ValueError, zlib.error):
        return None


def _cache_write(cache, row, raw):
    if len(raw) != row['bytes'] or _sha(raw) != row['sha256']:
        raise ValueError('Local chunk mismatch')
    with _pinned_directory(cache, create=False):
        destination = cache/row['sha256']
        _plain(destination)
        temporary = cache/('.' + uuid.uuid4().hex + '.part')
        owned = None
        try:
            with temporary.open('xb') as stream:
                owned = release.identity(temporary)
                stream.write(zlib.compress(raw, 1, wbits=-15))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if owned is not None and temporary.exists():
                remove_owned_file(temporary, owned)


def _local(index, target, missing, consume, progress):
    """Hash each raw source independently; a match never trusts local metadata."""
    chunks = {row['sha256']: row for row in index['chunks']}
    same = set()
    scanned = 0
    total = sum(row['bytes'] for row in index['files'])
    def scan(stream, row=None):
        nonlocal scanned
        digest, size = hashlib.sha256(), 0
        while data := stream.read(CHUNK):
            sha = _sha(data)
            digest.update(data)
            size += len(data)
            scanned += len(data)
            if sha in missing and len(data) == chunks[sha]['bytes']:
                consume(chunks[sha], data)
                missing.remove(sha)
            progress({'phase': 'scanning', 'done_bytes': scanned, 'total_bytes': max(total, scanned), 'downloaded_bytes': 0})
        return row and size == row['bytes'] and digest.hexdigest() == row['sha256']
    for row in index['files']:
        path = target/row['path']
        _plain(path)
        if path.exists():
            with _pinned_directory(path.parent, create=False), locked_read(release.pack.file_under(target, row['path'])) as stream:
                if scan(stream, row):
                    same.add(row['path'])
    for name in ('runtime/components-recovery.zip', 'runtime/gateway-recovery.zip'):
        path = target/name
        _plain(path)
        if not missing or not path.exists():
            continue
        with _pinned_directory(path.parent, create=False), locked_read(release.pack.file_under(target, name)) as stream:
            try:
                with zipfile.ZipFile(stream) as archive:
                    seen = set()
                    for member in archive.infolist():
                        if not missing:
                            break
                        allowed = release.pack.runtime_path(member.filename) if name.endswith('components-recovery.zip') else member.filename in ('gateway.qcow2', 'manifest.json')
                        if (not allowed or member.filename.casefold() in seen or member.flag_bits & 1
                                or member.compress_type not in (0, 8) or member.file_size > LIMIT):
                            continue
                        seen.add(member.filename.casefold())
                        with archive.open(member) as incoming:
                            scan(incoming)
            except (zipfile.BadZipFile, RuntimeError, zlib.error):
                pass
    # Older clean gateway packages can share most raw chunks with a new version.
    base = target/'gateway/packages/releases'
    _plain(base)
    if missing and base.exists():
        with _pinned_directory(base, create=False):
            for child in base.iterdir():
                _plain(child)
                if not child.is_dir() or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,63}', child.name):
                    continue
                disk, metadata_path = child/'gateway.qcow2', child/'manifest.json'
                with _pinned_directory(child, create=False):
                    _plain(disk)
                    _plain(metadata_path)
                    if not disk.exists() or not metadata_path.exists():
                        continue
                    disk = release.pack.file_under(child, disk.name)
                    metadata_path = release.pack.file_under(child, metadata_path.name)
                    with locked_read(metadata_path) as metadata_stream, locked_read(disk) as stream:
                        try:
                            metadata = release.pack._json(metadata_stream, 65536)
                            release.pack.gateway_package._manifest(metadata)
                            if metadata['version'] != child.name:
                                continue
                            release.pack.gateway_package._check_disk(stream, metadata)
                        except ValueError:
                            continue
                        stream.seek(0)
                        scan(stream)
    return same


def verify_cache(index, target):
    """Fully verify every referenced compressed chunk without materializing raw files."""
    index = _validate(index)
    cache = release.absolute(target)/'state/update-cache/chunks'
    verified, raw_total = 0, 0
    with _pinned_directory(cache, create=False):
        for row in index['chunks']:
            if not (cache/row['sha256']).exists():
                raise ValueError('Required cache chunk is missing')
            path = release.pack.file_under(cache, row['sha256'])
            digest, raw_bytes, compressed_bytes = hashlib.sha256(), 0, 0
            decoder = zlib.decompressobj(-15)
            try:
                with locked_read(path) as stream:
                    while data := stream.read(65536):
                        compressed_bytes += len(data)
                        if compressed_bytes > CHUNK + 65536:
                            raise ValueError('Oversized cached chunk')
                        while data:
                            raw = decoder.decompress(data, 65536)
                            raw_bytes += len(raw)
                            if raw_bytes > row['bytes']:
                                raise ValueError('Cached raw chunk exceeds inventory')
                            digest.update(raw)
                            if decoder.unused_data:
                                raise ValueError('Trailing cached chunk data')
                            data = decoder.unconsumed_tail
                if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                        or raw_bytes != row['bytes'] or digest.hexdigest() != row['sha256']):
                    raise ValueError('Cached chunk checksum mismatch')
            except zlib.error:
                raise ValueError('Invalid compressed cache chunk') from None
            verified += 1
            raw_total += raw_bytes
    return {'chunks': verified, 'bytes': raw_total}


def cache_gc_candidates(target, referenced):
    """List only unreferenced SHA-named regular files; the caller owns deletion."""
    if type(referenced) not in (set, frozenset):
        raise ValueError('Referenced chunks must be a verified set of SHA256 names')
    for sha in referenced:
        _digest(sha)
    cache = release.absolute(target)/'state/update-cache/chunks'
    for parent in (*reversed(cache.parents), cache):
        _plain(parent)
    if not cache.exists():
        return []
    candidates = []
    with _pinned_directory(cache, create=False):
        for path in cache.iterdir():
            if re.fullmatch('[a-f0-9]{64}', path.name) and path.name not in referenced:
                candidates.append(release.pack.file_under(cache, path.name))
    return sorted(candidates)


def _require_vm_off(target):
    try:
        processes = release.runtime.engine_processes(target)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError('Cannot verify that the virtual machines are stopped') from error
    if type(processes) is not dict:
        raise RuntimeError('Runtime process snapshot is unconfirmed')
    if processes:
        raise RuntimeError('Stop the virtual machines before planning or preparing an update')


def _inventory(index, target, progress, populate=False):
    cache = target/'state/update-cache/chunks'
    for parent in (*reversed(cache.parents), cache):
        _plain(parent)
    if populate:
        release.directory(cache)
    missing = {row['sha256'] for row in index['chunks'] if _cache_read(cache, row) is None}
    def consume(row, raw):
        if populate:
            _cache_write(cache, row, raw)
    same = _local(index, target, missing, consume, progress)
    return cache, missing, same


def seed_cache(index, source_root, target, progress=lambda value: None):
    """Seed verified chunks from a checked full app tree, without changing app files."""
    index = _validate(index)
    source, target = map(release.absolute, (source_root, target))
    with _pinned_directory(source, create=False), _pinned_directory(target, create=False):
        _require_vm_off(target)
        cache = release.directory(target/'state/update-cache/chunks')
        missing = {row['sha256'] for row in index['chunks'] if _cache_read(cache, row) is None}
        required = sum(row['bytes'] + 65536 for row in index['chunks'] if row['sha256'] in missing)
        if shutil.disk_usage(cache).free < required:
            raise RuntimeError('Insufficient disk space to seed verified update cache')
        def consume(row, raw):
            _cache_write(cache, row, raw)
        _local(index, source, missing, consume, progress)
        if missing:
            raise ValueError('Full application source does not contain all required chunks')
        _require_vm_off(target)
        return verify_cache(index, target)


def _summary(index, missing, same):
    needed = sum(row['compressed_bytes'] for row in index['chunks'] if row['sha256'] in missing)
    changed = [row['path'] for row in index['files'] if row['path'] not in same]
    # Cache, staged files, transaction copies and rollback, plus bounded I/O slack.
    required = sum(row['bytes'] + 65536 for row in index['chunks']) + 3*sum(row['bytes'] for row in index['files']) + 64*1024**2
    return {'download_bytes': needed, 'required_free_bytes': required, 'changed_files': len(changed),
            'changed_paths': changed, 'reused_chunks': len(index['chunks']) - len(missing), 'total_chunks': len(index['chunks'])}


def plan(index_path, sha256, target, progress=lambda value: None):
    index, target = read_index(index_path, sha256), release.absolute(target)
    with _pinned_directory(target, create=False):
        _require_vm_off(target)
        _, missing, same = _inventory(index, target, progress)
    return dict(_summary(index, missing, same), phase='planned')


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not _final_url(request.full_url, newurl):
            raise ValueError('Unsafe update redirect')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _final_url(original, final):
    source, parsed = urllib.parse.urlsplit(original), urllib.parse.urlsplit(final)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return False
    return ((parsed.scheme == 'https' and parsed.hostname in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com') and parsed.port in (None, 443))
            or source.scheme == parsed.scheme == 'http' and source.hostname == parsed.hostname == '127.0.0.1' and source.port == parsed.port)


def _open(request, timeout):
    return urllib.request.build_opener(_Redirect()).open(request, timeout=timeout)


def _fetch(archive, row):
    start, count = row['offset'], row['compressed_bytes']
    end = start + count - 1
    request = urllib.request.Request(archive['url'], headers={'Range': f'bytes={start}-{end}',
        'Accept-Encoding': 'identity', 'User-Agent': 'PocketWindows-Launcher'})
    for attempt in range(3):
        try:
            with _open(request, timeout=30) as response:
                if (not _final_url(archive['url'], response.url) or response.status != 206
                        or response.headers.get('Content-Range') != f'bytes {start}-{end}/{archive["bytes"]}'
                        or response.headers.get('Content-Length') != str(count)
                        or response.headers.get('Content-Encoding', 'identity') != 'identity'):
                    raise ValueError('Server did not return the requested chunk range')
                data = response.read(count + 1)
                if len(data) > count:
                    raise ValueError('Oversized chunk range')
                if len(data) < count:
                    raise ConnectionError('Truncated chunk range')
                return _decode(data, row, row['method']), count
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            if attempt == 2:
                raise
            time.sleep(.2*(attempt + 1))


def prepare(index_path, sha256, target, stage, progress=lambda value: None, offline=False):
    if type(offline) is not bool:
        raise ValueError('Offline mode must be boolean')
    index_path, target, stage = map(release.absolute, (index_path, target, stage))
    index = read_index(index_path, sha256)
    _plain(stage)
    internal = stage.is_relative_to(target)
    safe_internal = (stage.parent == target/'state' and re.fullmatch('[a-f0-9]{32}', stage.name))
    if stage.exists() or target.is_relative_to(stage) or internal and not safe_internal or index_path.is_relative_to(stage):
        raise ValueError('Stage must be new, outside installation or a UUID directory directly under state')
    with _pinned_directory(target, create=False):
        _require_vm_off(target)
    release.directory(stage.parent)
    with _pinned_directory(target, create=False), _pinned_directory(stage.parent, create=False):
        stage.mkdir()
        with _pinned_directory(stage, create=False):
            cache, missing, same = _inventory(index, target, progress, populate=True)
            if offline and missing:
                raise RuntimeError(f'Offline repair needs {len(missing)} missing verified chunks; no network request was made')
            result = _summary(index, missing, same)
            if shutil.disk_usage(stage).free < result['required_free_bytes'] or shutil.disk_usage(cache).free < result['required_free_bytes']:
                raise RuntimeError('Insufficient disk space to prepare update')
            downloaded = 0
            for row in index['chunks']:
                if row['sha256'] in missing:
                    raw, count = _fetch(index['archive'], row)
                    _cache_write(cache, row, raw)
                    downloaded += count
                    progress({'phase': 'downloading', 'done_bytes': downloaded, 'total_bytes': result['download_bytes'], 'downloaded_bytes': downloaded})
            indexed = {row['sha256']: row for row in index['chunks']}
            total, done = sum(row['bytes'] for row in index['files'] if row['path'] not in same), 0
            for row in index['files']:
                if row['path'] in same:
                    continue
                path = stage/'app'/row['path']
                release.directory(path.parent)
                digest, size = hashlib.sha256(), 0
                with _pinned_directory(path.parent, create=False), path.open('xb') as stream:
                    for sha in row['chunks']:
                        chunk = indexed[sha]
                        raw = _cache_read(cache, chunk)
                        if raw is None:
                            raise ValueError('Verified cache chunk disappeared')
                        stream.write(raw)
                        digest.update(raw)
                        size += len(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if size != row['bytes'] or digest.hexdigest() != row['sha256']:
                    raise ValueError('Assembled file checksum mismatch')
                done += size
                progress({'phase': 'assembling', 'done_bytes': done, 'total_bytes': total, 'downloaded_bytes': downloaded})
            _require_vm_off(target)
            verify_cache(index, target)
            with _pinned_directory(index_path.parent, create=False), locked_read(release.pack.file_under(index_path.parent, index_path.name)) as stream:
                raw = stream.read(release.JSON_LIMIT + 1)
            if _sha(raw) != sha256:
                raise ValueError('Index changed during preparation')
            with (stage/'index.json').open('xb') as stream:
                stream.write(raw)
            release.atomic_json(stage/'prepared.json', {'schema': 2, 'index_sha256': sha256, 'omitted': sorted(same)})
            return dict(result, phase='prepared', stage=str(stage), index_sha256=sha256, downloaded_bytes=downloaded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'prepare'):
        command = commands.add_parser(name)
        command.add_argument('--index', type=Path, required=True)
        command.add_argument('--index-sha256', required=True)
        command.add_argument('--target', type=Path, required=True)
        if name == 'prepare':
            command.add_argument('--stage', type=Path, required=True)
            command.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    def progress(value):
        print(json.dumps(value), flush=True)
    try:
        result = (plan(args.index, args.index_sha256, args.target, progress) if args.command == 'plan' else
                  prepare(args.index, args.index_sha256, args.target, args.stage, progress, offline=args.offline))
        progress(result)
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Update verification failed; installed application files were not changed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

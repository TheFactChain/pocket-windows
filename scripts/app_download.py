"""Fetch only changed ZIP members; install through the existing checked transaction."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_portable as release
from file_snapshot import locked_read, remove_owned_file
from profile_store import _pinned_directory, _plain

CHUNK = 65536


def archive_url(url, version):
    expected = ('https://github.com/TheFactChain/pocket-windows/releases/download/v'
                + version + '/Pocket-Windows-' + version + '-app.zip')
    if url == expected:
        return True
    api = urllib.parse.urlsplit(os.environ.get('POCKET_WINDOWS_UPDATE_API', ''))
    parsed = urllib.parse.urlsplit(url)
    return (api.scheme == parsed.scheme == 'http' and api.hostname == parsed.hostname == '127.0.0.1'
            and api.port == parsed.port and parsed.username is None and not parsed.fragment)


def read_index(path, digest):
    if not re.fullmatch('[a-f0-9]{64}', digest or ''):
        raise ValueError('Invalid index checksum')
    with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
        raw = stream.read(release.JSON_LIMIT + 1)
    if len(raw) > release.JSON_LIMIT or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Index checksum mismatch')
    import io
    value = release.pack._json(io.BytesIO(raw))
    if type(value) is not dict or set(value) != {'schema', 'release', 'archive', 'files'} or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Invalid update index')
    manifest, archive = value['release'], value['archive']
    if type(manifest) is not dict or manifest.get('omitted'):
        raise ValueError('Index requires a complete release')
    info = release.validate_release_info({key: manifest.get(key) for key in ('schema', 'release_id', 'source_commit')})
    rows = release.validate_common_records(manifest.get('common_files'))
    if release.INFO not in {row['path'] for row in rows} or sum(row['bytes'] for row in rows) > 12 * 1024**3:
        raise ValueError('Invalid expanded update size')
    if (type(archive) is not dict or set(archive) != {'url', 'bytes', 'sha256'}
            or type(archive['url']) is not str or not archive_url(archive['url'], info['release_id'])
            or type(archive['bytes']) is not int or not 1 <= archive['bytes'] <= 8 * 1024**3
            or type(archive['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', archive['sha256'])):
        raise ValueError('Invalid archive identity')
    entries = value['files']
    if type(entries) is not list or len(entries) != len(rows):
        raise ValueError('Invalid indexed file count')
    indexed, intervals = {}, []
    for entry in entries:
        if (type(entry) is not dict or set(entry) != {'path', 'offset', 'compressed_bytes', 'method'}
                or type(entry['path']) is not str or entry['path'].casefold() in indexed
                or any(type(entry[key]) is not int for key in ('offset', 'compressed_bytes', 'method'))
                or entry['method'] not in (0, 8) or entry['offset'] < 0 or entry['compressed_bytes'] < 0
                or entry['offset'] + entry['compressed_bytes'] > archive['bytes']):
            raise ValueError('Invalid file range')
        indexed[entry['path'].casefold()] = entry
        intervals.append((entry['offset'], entry['offset'] + entry['compressed_bytes']))
    if {entry['path'] for entry in entries} != {row['path'] for row in rows}:
        raise ValueError('Index does not match release inventory')
    intervals.sort()
    if any(current[0] < previous[1] for previous, current in zip(intervals, intervals[1:])):
        raise ValueError('Overlapping file ranges')
    return manifest, archive, {row['path']: indexed[row['path'].casefold()] for row in rows}


def matches(target, row):
    path = target / row['path']
    if not path.exists():
        _plain(path)
        return False
    with _pinned_directory(path.parent, create=False):
        return release.file_record(release.pack.file_under(target, row['path']), row['path']) == row


def fetch_member(archive, entry, row, destination):
    start, count = entry['offset'], entry['compressed_bytes']
    digest, written, received = hashlib.sha256(), 0, 0
    decoder = zlib.decompressobj(-15) if entry['method'] == 8 else None

    def write(outgoing, data):
        nonlocal written
        written += len(data)
        if written > row['bytes']:
            raise ValueError('Expanded file exceeds inventory')
        digest.update(data)
        outgoing.write(data)

    with destination.open('xb') as outgoing:
        if count:
            end = start + count - 1
            request = urllib.request.Request(archive['url'], headers={
                'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity', 'User-Agent': 'PocketWindows-Launcher'})
            with urllib.request.urlopen(request, timeout=30) as response:
                final = urllib.parse.urlsplit(response.url)
                original = urllib.parse.urlsplit(archive['url'])
                secure_redirect = (final.scheme == 'https' and final.hostname in
                                   ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')
                                   and final.username is None and final.port in (None, 443))
                local_redirect = original.scheme == 'http' and final.scheme == 'http' and final.hostname == '127.0.0.1' and final.port == original.port
                if (not (secure_redirect or local_redirect) or response.status != 206
                        or response.headers.get('Content-Range') != f'bytes {start}-{end}/{archive["bytes"]}'
                        or response.headers.get('Content-Length') != str(count)
                        or response.headers.get('Content-Encoding', 'identity') != 'identity'):
                    raise ValueError('Server did not return the requested file range')
                while received < count:
                    data = response.read(min(CHUNK, count - received))
                    if not data:
                        raise ValueError('Truncated file range')
                    received += len(data)
                    if decoder is None:
                        write(outgoing, data)
                    else:
                        while data:
                            write(outgoing, decoder.decompress(data, CHUNK))
                            data = decoder.unconsumed_tail
                if response.read(1):
                    raise ValueError('Oversized file range')
        if decoder and (not decoder.eof or decoder.unused_data):
            raise ValueError('Invalid compressed file')
    if written != row['bytes'] or digest.hexdigest() != row['sha256']:
        raise ValueError('Downloaded file checksum mismatch')
    return received


def download(index, index_sha256, target, output, progress=lambda value: None):
    index, target, output = map(release.absolute, (index, target, output))
    _plain(output)
    if output.is_relative_to(target) or index == output or output.exists():
        raise ValueError('Update output must be new and outside the installation')
    manifest, archive, indexed = read_index(index, index_sha256)
    rows = manifest['common_files']
    with _pinned_directory(target, create=False), _pinned_directory(output.parent, create=False):
        same = {row['path'] for row in rows if matches(target, row)}
        include = {row['path'] for row in rows if row['path'] not in same} | {release.INFO}
        if any(name in include for name in release.pack.GATEWAY_RECOVERY):
            include.update(name for name in release.pack.GATEWAY_RECOVERY if name in indexed)
        omitted = sorted(row['path'] for row in rows if row['path'] not in include)
        needed = sum(indexed[row['path']]['compressed_bytes'] for row in rows if row['path'] not in same)
        import shutil
        if shutil.disk_usage(output.parent).free < sum(row['bytes'] for row in rows if row['path'] in include) * 2 + 64 * 1024**2:
            raise RuntimeError('Insufficient disk space for the changed files')
        downloaded = 0
        output_identity = None
        progress(0)
        try:
            with tempfile.TemporaryDirectory(prefix='pocket-changed-', dir=output.parent) as scratch, \
                    zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=1) as package:
                output_identity = release.identity(output)
                partial = dict(manifest, artifacts={}, omitted=omitted, manual_acceptance=None)
                package.writestr('RELEASE.json', release.json_bytes(partial))
                for number, row in enumerate(rows):
                    name = row['path']
                    if name not in include:
                        continue
                    if name in same:
                        with _pinned_directory((target/name).parent, create=False), \
                                locked_read(release.pack.file_under(target, name)) as incoming:
                            with package.open('app/' + name, 'w', force_zip64=True) as outgoing:
                                digest, size = hashlib.sha256(), 0
                                for data in iter(lambda: incoming.read(CHUNK), b''):
                                    size += len(data); digest.update(data); outgoing.write(data)
                            if size != row['bytes'] or digest.hexdigest() != row['sha256']:
                                raise ValueError('Local file changed during update preparation')
                    else:
                        path = Path(scratch) / str(number)
                        for attempt in range(3):
                            try:
                                downloaded += fetch_member(archive, indexed[name], row, path)
                                break
                            except (urllib.error.URLError, TimeoutError, ConnectionError):
                                path.unlink(missing_ok=True)
                                if attempt == 2:
                                    raise
                                time.sleep(.5 * (attempt + 1))
                        package.write(path, 'app/' + name)
                        path.unlink()
                        progress(min(99, int(downloaded * 100 / max(1, needed))))
            result = {'sha256': release._sha256_file(output), 'bytes': output.stat().st_size,
                      'downloaded_bytes': downloaded, 'changed_files': len(rows) - len(same), 'reused_files': len(same)}
            progress(100)
            return result
        except BaseException:
            if output_identity is not None:
                remove_owned_file(output, output_identity)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--index-sha256', required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    last = [-1]
    def progress(value):
        if value != last[0]:
            print(json.dumps({'progress': value}), flush=True)
            last[0] = value
    try:
        print(json.dumps(download(args.index, args.index_sha256, args.target, args.output, progress)), flush=True)
        return 0
    except Exception:
        print('Update download failed verification; installed files were not changed.', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())

"""Create and read-back-verify a separate baseline; never modify source files."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_snapshot import locked_read

DATA = ('images/workvm-empty.qcow2', 'images/gateway-empty.qcow2',
        'state/workvm-vars.fd', 'state/gateway-network-accepted.json',
        'private/gohost-user1/sing-box.json')
CHUNK = 4 * 1024 * 1024


def inventory(root):
    names = set(DATA)
    names.update(path.name for path in root.iterdir()
                 if path.is_file() and path.suffix.lower() in {'.cmd', '.md'})
    for directory in ('scripts', 'gateway', 'tests', 'config', 'docs'):
        names.update(path.relative_to(root).as_posix()
                     for path in (root / directory).iterdir() if path.is_file())
    names.update(path.relative_to(root).as_posix()
                 for path in (root / 'runtime').rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts)
    result = {}
    for name in sorted(names):
        path = root / name
        if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink() or path.is_junction():
            raise ValueError('Source path escapes baseline root')
        if not path.is_file():
            raise FileNotFoundError('Missing baseline source: ' + name)
        result[name] = path
    return result


def copy_and_verify(entries, destination):
    destination.mkdir()  # Exclusive: no existing directory is reused.
    total = sum(path.stat().st_size for path in entries.values())
    done, last_report = 0, 0
    manifest = {'schema': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'verified': False, 'contains_existing_credentials': True, 'files': []}
    with ExitStack() as snapshot:
        held = {name: snapshot.enter_context(locked_read(path)) for name, path in entries.items()
                if name.startswith(('images/', 'state/', 'private/'))}
        for name, source in sorted(entries.items()):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            before = source.stat()
            digest, count = hashlib.sha256(), 0
            with ExitStack() as context:
                reader = held.get(name) or context.enter_context(locked_read(source))
                with target.open('xb') as writer:
                    while chunk := reader.read(CHUNK):
                        writer.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                        done += len(chunk)
                        if time.monotonic() - last_report >= 10:
                            print(json.dumps({'phase': 'baseline-copy', 'bytes': done,
                                              'total_bytes': total, 'member': name}), flush=True)
                            last_report = time.monotonic()
            after = source.stat()
            if count != before.st_size or (after.st_size, after.st_mtime_ns) != (
                    before.st_size, before.st_mtime_ns):
                raise RuntimeError('Source changed during snapshot: ' + name)
            shutil.copystat(source, target)
            manifest['files'].append({'path': name, 'bytes': count, 'sha256': digest.hexdigest()})
        done = 0
        for row in manifest['files']:
            digest = hashlib.sha256()
            with (destination / row['path']).open('rb') as reader:
                while chunk := reader.read(CHUNK):
                    digest.update(chunk)
                    done += len(chunk)
                    if time.monotonic() - last_report >= 10:
                        print(json.dumps({'phase': 'baseline-verify', 'bytes': done,
                                          'total_bytes': total, 'member': row['path']}), flush=True)
                        last_report = time.monotonic()
            if digest.hexdigest() != row['sha256']:
                raise RuntimeError('Baseline verification mismatch: ' + row['path'])
        manifest.update(verified=True, verified_utc=datetime.now(timezone.utc).isoformat(),
                        total_bytes=total, file_count=len(entries))
        with (destination / 'BASELINE-MANIFEST.json').open('x', encoding='utf-8') as writer:
            json.dump(manifest, writer, indent=2, ensure_ascii=True)
            writer.write('\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    root = args.project.resolve(strict=True)
    destination = args.destination.resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Baseline must be separate from the original project')
    entries = inventory(root)
    total = sum(path.stat().st_size for path in entries.values())
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < total + 2 * 1024 ** 3:
        raise RuntimeError('Not enough free space for the complete baseline')
    result = copy_and_verify(entries, destination)
    print(json.dumps({'baseline': str(destination), 'verified': result['verified'],
                      'files': result['file_count'], 'bytes': result['total_bytes']}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()

"""Build an isolated x86_64 runtime candidate, with the existing repair ZIP contract.

This never installs, replaces, removes or executes runtime components. Booting both
VMs from the candidate remains an independent acceptance check.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile

QEMU = 'runtime/qemu-11.1.0'
PYTHON = 'runtime/python-3.12.14'
MANIFEST = 'runtime/components-manifest.json'
RECOVERY = 'runtime/components-recovery.zip'
# pc/q35, std VGA, explicit work UEFI, gateway direct Linux boot, e1000/virtio NICs.
# All root DLLs are retained to cover imports and dynamically loaded SDL dependencies.
REQUIRED_QEMU = (
    'qemu-system-x86_64.exe', 'qemu-img.exe', 'COPYING', 'COPYING.LIB',
    'share/bios.bin', 'share/bios-256k.bin',
    'share/edk2-x86_64-code.fd', 'share/edk2-i386-vars.fd', 'share/edk2-licenses.txt',
    'share/vgabios-stdvga.bin', 'share/vgabios.bin', 'share/kvmvapic.bin',
    'share/linuxboot_dma.bin', 'share/multiboot_dma.bin', 'share/pvh.bin',
    'share/efi-e1000.rom', 'share/pxe-e1000.rom',
    'share/efi-virtio.rom', 'share/pxe-virtio.rom', 'share/doc/about/license.html',
)
MAX_MANIFEST = 16 * 1024 * 1024


def _fail():
    raise ValueError('Unsafe path, incomplete runtime or component hash mismatch')


def _is_reparse(path, info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _plain(path, *, missing=False):
    path = Path(os.path.abspath(path))
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing:
                continue
            _fail()
        if _is_reparse(item, info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            _fail()
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            _fail()
    return path


def _runtime_name(name):
    if type(name) is not str or not (name.startswith(QEMU + '/') or name.startswith(PYTHON + '/')):
        _fail()
    for part in name.split('/'):
        if (not part or part in ('.', '..') or part.endswith(('.', ' '))
                or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', part, re.I)):
            _fail()
    return name


def _sha(path):
    _plain(path)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    _plain(path)
    return digest


def _tree(directory):
    _plain(directory)
    for item in sorted(directory.iterdir()):
        _plain(item)
        if item.is_dir():
            yield from _tree(item)
        elif item.is_file():
            yield item
        else:
            _fail()


def _manifest(root):
    path = _plain(root / MANIFEST)
    if path.stat().st_size > MAX_MANIFEST:
        _fail()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=pairs,
                          parse_constant=lambda _: _fail())
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail()
    if (type(data) is not dict or set(data) != {'schema', 'files'} or type(data['schema']) is not int
            or data['schema'] != 1 or type(data['files']) is not list or not 1 <= len(data['files']) <= 30000):
        _fail()
    records = {}
    for record in data['files']:
        if type(record) is not dict or set(record) != {'path', 'bytes', 'sha256'}:
            _fail()
        name = _runtime_name(record['path'])
        if (name.casefold() in records or type(record['bytes']) is not int or record['bytes'] < 0
                or type(record['sha256']) is not str or not re.fullmatch('[0-9a-fA-F]{64}', record['sha256'])):
            _fail()
        records[name.casefold()] = dict(record, sha256=record['sha256'].lower())
    return records


def _allowed(name):
    if name.startswith(PYTHON + '/'):
        return True
    relative = name[len(QEMU) + 1:]
    return (relative in REQUIRED_QEMU or ('/' not in relative and relative.lower().endswith('.dll'))
            or relative.startswith('share/keymaps/'))


def inventory(root):
    """Read-only validation against the supplied source manifest, not publisher authentication."""
    root = _plain(root)
    records = _manifest(root)
    files = list(_tree(root / QEMU)) + list(_tree(root / PYTHON))
    actual = {path.relative_to(root).as_posix(): path for path in files}
    # Windows repair uses case-insensitive keys; reject both spelling ambiguity and untracked files.
    if len({name.casefold() for name in actual}) != len(actual) or set(records) != {name.casefold() for name in actual}:
        _fail()
    if any(records[name.casefold()]['path'] != name for name in actual):
        _fail()
    if (any(QEMU + '/' + name not in actual for name in REQUIRED_QEMU)
            or PYTHON + '/python.exe' not in actual
            or not any(name.startswith(QEMU + '/share/keymaps/') for name in actual)
            or not any(name.startswith(QEMU + '/') and '/' not in name[len(QEMU) + 1:] and name.lower().endswith('.dll') for name in actual)):
        _fail()
    selected = []
    before = 0
    for name, path in sorted(actual.items()):
        record = records[name.casefold()]
        size = path.stat().st_size
        # Sizes of excluded files still must match the trusted inventory used for accounting.
        if size != record['bytes']:
            _fail()
        before += size
        if _allowed(name):
            if _sha(path) != record['sha256']:
                _fail()
            selected.append(record)
    source_recovery = root / RECOVERY
    _plain(source_recovery, missing=True)
    recovery_size = _plain(source_recovery).stat().st_size if source_recovery.exists() else 0
    return {
        'schema': 1, 'files': selected, 'selected_files': len(selected), 'source_files': len(files),
        'payload_bytes_before': before, 'payload_bytes_after': sum(r['bytes'] for r in selected),
        'source_recovery_bytes': recovery_size, 'source_manifest_bytes': (root / MANIFEST).stat().st_size,
        'source_manifest_sha256': _sha(root / MANIFEST),
        'selection_policy': 'x86_64 engines; all root DLLs; explicit x86 firmware; keymaps; licenses; full Python',
        'boot_verified': False,
    }


def _copy_checked(source, destination, record):
    _plain(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _plain(destination.parent)
    digest = hashlib.sha256()
    size = 0
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
            outgoing.write(chunk)
    _plain(source)
    if size != record['bytes'] or digest.hexdigest() != record['sha256']:
        _fail()


def prepare_bundle(root, outputnew):
    """Copy a validated candidate to a NEW sibling directory; never modify the source.

    On failure the partial new directory is retained for inspection and never reported
    as complete. Concurrent mutation of source/output directories is unsupported.
    """
    root = _plain(root)
    output = _plain(outputnew, missing=True)
    if output.exists() or output.is_relative_to(root) or root.is_relative_to(output):
        _fail()
    report = inventory(root)
    output.mkdir(parents=False, exist_ok=False)
    for record in report['files']:
        _copy_checked(root / record['path'], output / record['path'], record)
    archive_path = output / RECOVERY
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for record in report['files']:
            path = output / record['path']
            if _sha(path) != record['sha256']:
                _fail()
            info = zipfile.ZipInfo(record['path'], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with path.open('rb') as source, archive.open(info, 'w', force_zip64=True) as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    # Verify the recovery bytes as the repair script will see them before publishing its manifest.
    with zipfile.ZipFile(archive_path) as archive:
        if archive.namelist() != [record['path'] for record in report['files']]:
            _fail()
        for record in report['files']:
            with archive.open(record['path']) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                    _fail()
    manifest = {'schema': 1, 'files': report['files']}
    with (output / MANIFEST).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, separators=(',', ':'))
        stream.write('\n')
    report.update(archive_bytes=archive_path.stat().st_size, archive_sha256=_sha(archive_path),
                  manifest_sha256=_sha(output / MANIFEST), complete=True)
    report['bytes_before'] = report['payload_bytes_before'] + report['source_recovery_bytes'] + report['source_manifest_bytes']
    report['bytes_after'] = report['payload_bytes_after'] + report['archive_bytes'] + (output / MANIFEST).stat().st_size
    report['bytes_saved'] = report['bytes_before'] - report['bytes_after']
    with (output / 'runtime-bundle-report.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output-new', type=Path, help='Omit for read-only inventory')
    args = parser.parse_args()
    try:
        report = prepare_bundle(args.root, args.output_new) if args.output_new else inventory(args.root)
        print(json.dumps({key: value for key, value in report.items() if key != 'files'}, indent=2))
    except (ValueError, OSError, zipfile.BadZipFile):
        parser.exit(1, 'Runtime bundle refused: unsafe paths, invalid manifest or mismatched components.\n')


if __name__ == '__main__':
    main()

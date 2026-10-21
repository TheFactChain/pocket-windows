"""Prepare public, nonce-bound Audit app provisioning; never edits a VM or user profile."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference_prep import _json

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ('Run-Prepare.cmd', 'Prepare-Template.ps1', 'README.md')
MAX_RECEIPT_BYTES = 65536
GATES = {'pending_setup', 'pending_servicing', 'bitlocker', 'efs'}


def _fail():
    raise ValueError('Invalid template preparation input or receipt')


def _text(value, pattern=None, limit=2048):
    if type(value) is not str or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        _fail()
    if pattern is not None and re.fullmatch(pattern, value) is None:
        _fail()
    return value


def validate_approvals(claude, firefox):
    common = {'path', 'sha256', 'version', 'publisher', 'signer_thumbprint'}
    if (type(claude) is not dict or set(claude) != common | {'name'} or type(firefox) is not dict
            or set(firefox) != common | {'product_code', 'executable'}):
        _fail()
    result = {}
    for role, approval, filename in [('claude', claude, 'Claude.msix'), ('firefox', firefox, 'Firefox.msi')]:
        for key in common - {'path'}:
            _text(approval[key])
        _text(approval['sha256'], '[0-9a-f]{64}')
        _text(approval['signer_thumbprint'], '[0-9A-F]{40}')
        _text(approval['version'], r'[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?', 64)
        if role == 'claude':
            _text(approval['name'], r'[A-Za-z0-9][A-Za-z0-9.\-]{2,127}')
            if len(approval['version'].split('.')) != 4:
                _fail()
        else:
            _text(approval['product_code'], r'\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}')
            if approval['executable'] != 'Mozilla Firefox/firefox.exe':
                _fail()
        result[role] = {key: value for key, value in approval.items() if key != 'path'} | {'file': filename}
    return result


def _regular(path):
    path = Path(path)
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 536870912:
        _fail()


def read_handoff(directory):
    directory = Path(directory)
    _regular(directory / 'handoff.json')
    manifest = _json(directory / 'handoff.json', 16384)
    if (type(manifest) is not dict or set(manifest) != {'schema', 'phase', 'nonce', 'expected_smbios_serial', 'files', 'claude', 'firefox'}
            or type(manifest['schema']) is not int or manifest['schema'] != 1
            or manifest['phase'] != 'audit_app_provisioning'):
        _fail()
    _text(manifest['nonce'], '[0-9a-f]{32}')
    if manifest['expected_smbios_serial'] != 'WCMASTER-' + manifest['nonce']:
        _fail()
    expected = {*SOURCE_FILES, 'Claude.msix', 'Firefox.msi'}
    if (type(manifest['files']) is not dict or set(manifest['files']) != expected
            or set(path.name for path in directory.iterdir()) != expected | {'handoff.json'}):
        _fail()
    approvals = {}
    for role, filename in [('claude', 'Claude.msix'), ('firefox', 'Firefox.msi')]:
        if type(manifest[role]) is not dict or manifest[role].get('file') != filename:
            _fail()
        approvals[role] = {key: value for key, value in manifest[role].items() if key != 'file'} | {'path': str(directory / filename)}
    normalized = validate_approvals(**approvals)
    if any(normalized[role] != manifest[role] for role in normalized):
        _fail()
    for name, digest in manifest['files'].items():
        _text(digest, '[0-9a-f]{64}')
        _regular(directory / name)
        with (directory / name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                _fail()
    if any(manifest[role]['sha256'] != manifest['files'][manifest[role]['file']] for role in normalized):
        _fail()
    return manifest


def prepare_handoff(destination, approved_apps, nonce=None):
    if type(approved_apps) is not dict or set(approved_apps) != {'claude', 'firefox'}:
        _fail()
    claude, firefox = approved_apps['claude'], approved_apps['firefox']
    approvals = validate_approvals(claude, firefox)
    nonce = uuid.uuid4().hex if nonce is None else _text(nonce, '[0-9a-f]{32}')
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        _fail()
    for parent in destination.parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail()
    # A fresh directory preserves every previous payload, report and failed attempt.
    destination.mkdir(parents=False, exist_ok=False)
    for name in SOURCE_FILES:
        source = ROOT / 'guest/template' / name
        _regular(source)
        with source.open('rb') as stream, (destination / name).open('xb') as output:
            shutil.copyfileobj(stream, output)
    for role, approval in [('claude', claude), ('firefox', firefox)]:
        source = Path(approval['path'])
        _regular(source)
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != approval['sha256']:
                _fail()
            stream.seek(0)
            with (destination / approvals[role]['file']).open('xb') as output:
                shutil.copyfileobj(stream, output)
        with (destination / approvals[role]['file']).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != approval['sha256']:
                _fail()
    files = {}
    for name in (*SOURCE_FILES, 'Claude.msix', 'Firefox.msi'):
        with (destination / name).open('rb') as stream:
            files[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'schema': 1, 'phase': 'audit_app_provisioning', 'nonce': nonce,
                'expected_smbios_serial': 'WCMASTER-' + nonce, 'files': files, **approvals}
    with (destination / 'handoff.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=True, indent=2)
        stream.write('\n')
    return manifest


def validate_receipt(data, expected_nonce=None):
    if (type(data) is not dict or set(data) != {'schema', 'nonce', 'phase', 'verdict', 'code',
            'cleanup_allowed', 'generalize_allowed', 'gates', 'apps'} or type(data['schema']) is not int
            or data['schema'] != 1 or data['cleanup_allowed'] is not False or data['generalize_allowed'] is not False):
        _fail()
    _text(data['nonce'], '[0-9a-f]{32}')
    if expected_nonce is not None and _text(expected_nonce, '[0-9a-f]{32}') != data['nonce']:
        _fail()
    if type(data['code']) is not int or not 0 <= data['code'] <= 65535:
        _fail()
    if (type(data['gates']) is not dict or set(data['gates']) != GATES
            or any(value not in ('clear', 'active', 'unknown') for value in data['gates'].values())
            or type(data['apps']) is not dict or set(data['apps']) != {'claude', 'firefox'}
            or any(type(value) is not bool for value in data['apps'].values())):
        _fail()
    _text(data['phase'], limit=32)
    _text(data['verdict'], limit=32)
    allowed = {'audit_requested': ('pending', 0), 'apps_verified': ('review_required', 0), 'failed': ('failed', None)}
    expected = allowed.get(data['phase'])
    if expected is None or data['verdict'] != expected[0] or (expected[1] is not None and data['code'] != expected[1]):
        _fail()
    if data['phase'] == 'failed' and data['code'] == 0:
        _fail()
    if data['phase'] == 'apps_verified' and not all(data['apps'].values()):
        _fail()
    if data['phase'] == 'audit_requested' and any(data['apps'].values()):
        _fail()
    return data


def read_receipt(path, expected_nonce=None):
    return validate_receipt(_json(path, MAX_RECEIPT_BYTES), expected_nonce)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('approvals', type=Path, help='Public JSON containing exactly claude and firefox approvals')
    parser.add_argument('--nonce')
    args = parser.parse_args()
    try:
        approvals = _json(args.approvals, 16384)
        if type(approvals) is not dict or set(approvals) != {'claude', 'firefox'}:
            _fail()
        print(json.dumps(prepare_handoff(args.destination, approvals, nonce=args.nonce), indent=2))
    except (OSError, ValueError, TypeError):
        parser.exit(1, 'Template handoff refused; existing files preserved.\n')


if __name__ == '__main__':
    main()

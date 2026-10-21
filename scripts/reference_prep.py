"""Prepare a manual inventory handoff; never boots or modifies a Windows image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import uuid

MAX_REPORT_BYTES = 8 * 1024 * 1024
SERVICE_SIDS = {'S-1-5-18', 'S-1-5-19', 'S-1-5-20'}
MARKERS = {'claude_config', 'claude_cli', 'chrome_profile', 'edge_profile',
           'credential_store', 'vault', 'dpapi', 'temp_cache'}
SECTION_FIELDS = {
    'local_accounts': {'sid', 'name', 'disabled'},
    'users': {'sid', 'name', 'loaded', 'special', 'markers'},
    'machine_apps': {'name', 'version', 'publisher'},
    'user_apps': {'sid', 'name', 'version', 'publisher'},
    'appx': {'name', 'package', 'version', 'framework', 'users'},
    'provisioned_appx': {'name', 'package', 'version'},
}
ROOT = Path(__file__).resolve().parents[1]


def _fail():
    # Never echo input: even a rejected report might contain accidentally exported secrets.
    raise ValueError('Invalid or unsafe reference inventory / handoff')


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _fail()


def _text(value, limit=512):
    if type(value) is not str or len(value) > limit or any(ord(c) < 32 for c in value):
        _fail()


def _sid(value):
    _text(value, 184)
    if not re.fullmatch(r'S-1-(?:\d+-){1,14}\d+', value):
        _fail()


def _bool(value):
    if type(value) is not bool:
        _fail()


def _json(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        _fail()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            _fail()
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                          parse_constant=lambda _: _fail())
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail()


def validate_report(data):
    _keys(data, {'schema', 'phase', 'reference_id', 'isolated_copy_acknowledged',
                 'cleanup_executed', 'sysprep_executed', 'context', 'os', 'sections'})
    if type(data['schema']) is not int or data['schema'] != 1 or data['phase'] != 'inventory_only':
        _fail()
    if type(data['reference_id']) is not str or not re.fullmatch('[0-9a-f]{32}', data['reference_id']):
        _fail()
    for key, expected in [('isolated_copy_acknowledged', True), ('cleanup_executed', False), ('sysprep_executed', False)]:
        if data[key] is not expected:
            _fail()
    context = data['context']
    _keys(context, {'sid', 'administrator', 'interactive'})
    _sid(context['sid'])
    if context['sid'] in SERVICE_SIDS or context['administrator'] is not True or context['interactive'] is not True:
        _fail()
    os_info = data['os']
    _keys(os_info, {'version', 'build', 'edition', 'sysprep_present', 'pending_reboot', 'setup_in_progress'})
    for key in ('version', 'build', 'edition'):
        _text(os_info[key])
    for key in ('sysprep_present', 'pending_reboot', 'setup_in_progress'):
        _bool(os_info[key])
    _keys(data['sections'], SECTION_FIELDS)
    for name, fields in SECTION_FIELDS.items():
        section = data['sections'][name]
        _keys(section, {'status', 'items', 'error'})
        if section['status'] not in ('complete', 'partial', 'unavailable'):
            _fail()
        if section['error'] not in (None, 'collection_failed', 'unloaded_hives_not_read'):
            _fail()
        if (section['status'] == 'complete') != (section['error'] is None):
            _fail()
        if type(section['items']) is not list or len(section['items']) > 20000:
            _fail()
        if section['status'] == 'unavailable' and section['items']:
            _fail()
        if section['status'] == 'partial' and (name != 'user_apps' or section['error'] != 'unloaded_hives_not_read'):
            _fail()
        if section['error'] == 'unloaded_hives_not_read' and (name != 'user_apps' or section['status'] != 'partial'):
            _fail()
        for item in section['items']:
            _keys(item, fields)
            for key in fields - {'markers', 'users', 'loaded', 'special', 'framework', 'disabled'}:
                _sid(item[key]) if key == 'sid' else _text(item[key])
            for key in fields & {'loaded', 'special', 'framework', 'disabled'}:
                _bool(item[key])
            if name == 'users':
                _keys(item['markers'], MARKERS)
                for value in item['markers'].values():
                    if value is not None:
                        _bool(value)
            if name == 'appx':
                if type(item['users']) is not list or len(item['users']) > 1000:
                    _fail()
                for user in item['users']:
                    _keys(user, {'sid', 'state'})
                    _sid(user['sid'])
                    if user['state'] not in ('Installed', 'Staged', 'NotPresent', 'Unknown'):
                        _fail()
    return data


def read_report(path, expected_reference_id=None):
    data = validate_report(_json(path, MAX_REPORT_BYTES))
    if expected_reference_id is not None:
        if (type(expected_reference_id) is not str or not re.fullmatch('[0-9a-f]{32}', expected_reference_id)
                or data['reference_id'] != expected_reference_id):
            _fail()
    return data


def summarize(data):
    validate_report(data)
    sections = data['sections']
    # Name/version comparison is a review hint, not a Sysprep compatibility verdict.
    provisioned = {(p['name'], p['version']) for p in sections['provisioned_appx']['items']}
    candidates = []
    if sections['provisioned_appx']['status'] == 'complete':
        candidates = sorted({p['package'] for p in sections['appx']['items']
                             if any(u['state'] == 'Installed' for u in p['users'])
                             and (p['name'], p['version']) not in provisioned})
    return {
        'reference_id': data['reference_id'], 'decision': 'review_required', 'ready_for_generalize': False,
        'incomplete_sections': [key for key in SECTION_FIELDS if sections[key]['status'] != 'complete'],
        'counts': {key: len(sections[key]['items']) for key in SECTION_FIELDS},
        'appx_review_candidates': candidates,
        'manual_reviews': ['application_retention', 'profile_mapping', 'login_cache_cleanup',
                           'efs_and_application_licensing', 'tiny11_sysprep_support'],
        'prerequisite_warnings': [key for key, bad in [('sysprep_present', False), ('pending_reboot', True),
                                                     ('setup_in_progress', True)] if data['os'][key] is bad],
    }


def prepare_handoff(destination):
    destination = Path(destination)
    source = ROOT / 'guest/reference'
    payload = {name: (source / name).read_bytes() for name in ('Collect-ReferenceInventory.ps1', 'Run-Inventory.cmd', 'README.md')}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()}
    if destination.exists():
        if destination.is_symlink() or destination.is_junction() or not destination.is_dir() or set(p.name for p in destination.iterdir()) != set(payload) | {'handoff.json'}:
            _fail()
        if any(p.is_symlink() or not p.is_file() for p in destination.iterdir()):
            _fail()
        existing = _json(destination / 'handoff.json', 8192)
        _keys(existing, {'schema', 'phase', 'reference_id', 'expected_smbios_serial', 'files'})
        identity = existing['reference_id']
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            _fail()
        if (type(existing['schema']) is not int or existing['schema'] != 1 or existing['phase'] != 'inventory_only'
                or existing['expected_smbios_serial'] != 'WCREFERENCE-' + identity or existing['files'] != hashes
                or any((destination / name).read_bytes() != content for name, content in payload.items())):
            _fail()
        return existing
    identity = uuid.uuid4().hex
    manifest = {'schema': 1, 'phase': 'inventory_only', 'reference_id': identity,
                'expected_smbios_serial': 'WCREFERENCE-' + identity, 'files': hashes}
    destination.mkdir(parents=False, exist_ok=False)
    for name, content in payload.items():
        with (destination / name).open('xb') as stream:
            stream.write(content)
    with (destination / 'handoff.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare').add_argument('destination', type=Path)
    review = commands.add_parser('review')
    review.add_argument('report', type=Path)
    review.add_argument('--expected-reference-id', required=True)
    args = parser.parse_args()
    try:
        result = (prepare_handoff(args.destination) if args.command == 'prepare'
                  else summarize(read_report(args.report, args.expected_reference_id)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError):
        parser.exit(1, 'Reference preparation refused: invalid input or inaccessible files.\n')


if __name__ == '__main__':
    main()

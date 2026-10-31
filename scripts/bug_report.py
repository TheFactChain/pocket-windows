"""Build a privacy-filtered bug report ZIP from allowlisted sources; never stops or changes Windows.

Only listed files under logs/ and state/recovery-*.json are read. JSON drops arguments and secret
keys; every text has the program path, user profile, user and PC names and saved connection
secrets replaced. Each source gets a status in manifest.json, so "missing" is never mistaken
for "no errors". The standard package stays under MAX_REPORT_BYTES of raw content.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import zipfile

MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_TAIL_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
KEEP_REPORTS = 3
SECRET_KEYS = {'arguments', 'credentials', 'password', 'secret', 'token', 'private_key', 'uri', 'link',
               'subscription', 'auth', 'obfs_password', 'username'}
TEXT_LIMITS = {'what': 4000, 'steps': 4000, 'contact': 200}


def validate_request(request):
    if type(request) is not dict or not re.fullmatch(r'[a-f0-9]{32}', str(request.get('report_id', ''))):
        raise ValueError('Нужен номер отчёта.')
    texts = {key: str(request.get(key) or '').strip()[:limit] for key, limit in TEXT_LIMITS.items()}
    if not texts['what']:
        raise ValueError('Опиши, что произошло.')
    images = request.get('images') or []
    if type(images) is not list or len(images) > 3:
        raise ValueError('Можно приложить не больше трёх снимков.')
    return {'report_id': request['report_id'], **texts, 'include_windows_logs': request.get('include_windows_logs') is not False,
            'images': [str(path) for path in images]}


class Redactor:
    def __init__(self, root, secrets=()):
        pairs = [(str(root), '<Pocket Windows>')]
        for name, label in (('USERPROFILE', '%USERPROFILE%'), ('LOCALAPPDATA', '%LOCALAPPDATA%'), ('APPDATA', '%APPDATA%'),
                            ('TEMP', '%TEMP%')):
            if os.environ.get(name):
                pairs.append((os.environ[name], label))
        pairs += [(value, '<secret>') for value in secrets if len(value) >= 6]  # short ones are ordinary words like "auto"
        for name, label in (('USERNAME', '<user>'), ('COMPUTERNAME', '<pc>')):
            if len(os.environ.get(name, '')) >= 3:
                pairs.append((os.environ[name], label))
        # Longest first, so a path is replaced before the user name inside it; both slash styles.
        expanded = []
        for value, label in pairs:
            expanded += [(value, label), (value.replace('\\', '/'), label), (value.replace('\\', '\\\\'), label)]
        expanded.sort(key=lambda item: -len(item[0]))
        self.pattern = re.compile('|'.join(re.escape(value) for value, _ in expanded), re.IGNORECASE) if expanded else None
        self.labels = {value.casefold(): label for value, label in expanded}

    # Any other absolute path (shared folders on D:, downloads) can name a person or a client; system folders stay.
    # Possessive separator: JSON doubles backslashes, and backtracking to one of them would defeat the system-folder check.
    OTHER_PATH = re.compile(r'\b[A-Za-z]:[\\/]++(?!(?:Windows|Program Files|ProgramData)\b)[^\s"\'<>|,;]*', re.IGNORECASE)

    def text(self, value):
        value = self.pattern.sub(lambda match: self.labels[match.group(0).casefold()], value) if self.pattern else value
        return self.OTHER_PATH.sub('<path>', value)

    def data(self, value):
        if isinstance(value, dict):
            return {key: '<removed>' if str(key).casefold() in SECRET_KEYS else self.data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.data(item) for item in value]
        return self.text(value) if isinstance(value, str) else value


def connection_secrets(root):
    """Saved connection values to scrub; read locally only to replace them, never written anywhere."""
    values = set()
    try:
        from profile_store import ProfileStore
        store = ProfileStore(root / 'profiles')
        if not store.directory.exists():
            return values
        for view in store.list_profiles():
            try:
                config = store.secret_config(view['profile_id'])
            except Exception:  # an unreadable profile just has nothing to scrub
                continue
            stack = [config]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    stack += [value for key, value in item.items() if key != 'protocol']
                elif isinstance(item, list):
                    stack += item
                elif isinstance(item, str) and len(item) >= 4:
                    values.add(item)
    except Exception:
        pass
    return values


class Package:
    def __init__(self, archive, redactor):
        self.archive, self.redactor = archive, redactor
        self.sources, self.files, self.used = [], [], 0

    def note(self, name, status, **details):
        self.sources.append({'source': name, 'status': status, **details})

    def add_bytes(self, name, data, source=None):
        if self.used + len(data) > MAX_REPORT_BYTES:
            self.note(source or name, 'skipped_limit', bytes=len(data))
            return False
        self.archive.writestr(name, data)
        self.used += len(data)
        self.files.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        return True

    def add_json(self, name, value, source=None):
        return self.add_bytes(name, json.dumps(self.redactor.data(value), ensure_ascii=False, indent=1, default=str).encode('utf-8'), source)

    def add_file(self, path, name, *, tail=MAX_TAIL_BYTES, source=None):
        source = source or name
        try:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                return self.note(source, 'error', reason='not a plain file')
            size = path.stat().st_size
            with path.open('rb') as stream:
                if size > tail:
                    stream.seek(size - tail)
                data = stream.read(tail)
        except FileNotFoundError:
            return self.note(source, 'missing')
        except PermissionError:
            return self.note(source, 'denied')
        except OSError as error:
            return self.note(source, 'error', reason=type(error).__name__)
        if path.suffix in ('.json', '.jsn'):
            try:
                if self.add_json(name, json.loads(data.decode('utf-8')), source):
                    self.note(source, 'ok')
                return
            except ValueError:
                self.note(source, 'error', reason='invalid json, kept as text')
        text = self.redactor.text(data.decode('utf-8', errors='replace'))
        if self.add_bytes(name, text.encode('utf-8'), source):
            self.note(source, 'truncated' if size > tail else 'ok', bytes=size)


def newest(paths, count):
    def stamp(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0
    return sorted(paths, key=stamp, reverse=True)[:count]


def session_folders(logs):
    """The last two launches and the guest folders their receipts name, newest first."""
    launches = newest(logs.glob('runtime-launch-*'), 2)
    guests = []
    for launch in launches:
        try:
            receipt = json.loads((launch / 'receipt.json').read_text(encoding='utf-8'))
            for record in receipt.get('guests', {}).values():
                folder = Path(record.get('state', '')).parent
                if folder.parent == logs and folder.name.startswith('guest-') and folder not in guests:
                    guests.append(folder)
        except (OSError, ValueError, AttributeError):
            pass
    for folder in newest(logs.glob('guest-*'), 4):
        if folder not in guests and len(guests) < 4:
            guests.append(folder)
    return launches, guests


def collect(root, request, diagnostics=None):
    root = Path(os.path.abspath(root))
    request = validate_request(request)
    logs = root / 'logs'
    folder = logs / 'reports' / request['report_id']
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'report.zip'
    partial = folder / 'report.zip.partial'
    redactor = Redactor(root, connection_secrets(root))
    created = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as archive:
        package = Package(archive, redactor)
        package.add_json('report.json', {key: request[key] for key in ('report_id', 'what', 'steps', 'contact', 'include_windows_logs')}
                         | {'created_utc': created})
        try:
            info = json.loads((root / 'release-info.json').read_text(encoding='utf-8'))
            package.add_json('versions.json', {key: info.get(key) for key in ('release_id', 'source_commit', 'schema')}
                             | {'gateway_version': (diagnostics or {}).get('gateway_version')})
            package.note('release-info.json', 'ok')
        except FileNotFoundError:
            package.note('release-info.json', 'missing')
        except (OSError, ValueError) as error:
            package.note('release-info.json', 'error', reason=type(error).__name__)
        if diagnostics is not None:
            public = {key: diagnostics.get(key) for key in ('ok', 'message', 'checks', 'blockers', 'host', 'settings', 'runtime', 'startup',
                      'host_files', 'clipboard', 'windows_display', 'package_check', 'network_verified', 'gateway_version', 'checked_utc')}
            package.add_json('diagnostics.json', public)
            package.note('diagnostics', 'ok')
        else:
            package.note('diagnostics', 'error', reason='check did not run')
        for name in ('events.jsonl', 'events.1.jsonl'):
            package.add_file(logs / name, 'logs/' + name, tail=4 * 1024 * 1024)
        launches, guests = session_folders(logs)
        for launch in launches:
            for name in ('receipt.json', 'stop-request.json', 'supervisor.stderr.log', 'work-controller.stderr.log',
                         'gateway-controller.stderr.log', 'supervisor.stdout.log', 'work-controller.stdout.log', 'gateway-controller.stdout.log'):
                if (launch / name).exists() or name == 'receipt.json':
                    package.add_file(launch / name, 'logs/' + launch.name + '/' + name)
        for guest in guests:
            for name in ('state.json', 'qmp-events.json', 'qmp-events.jsonl', 'force-stop.json', 'stderr.txt', 'runtime-serial.log'):
                if (guest / name).exists() or name == 'state.json':
                    package.add_file(guest / name, 'logs/' + guest.name + '/' + name)
            for backup in newest(guest.glob('state-before-recovery-*.json'), 2):
                package.add_file(backup, 'logs/' + guest.name + '/' + backup.name)
        for path in newest((root / 'state').glob('recovery-*.json'), 5):
            package.add_file(path, 'state/' + path.name)
        for path in newest((logs / 'update-history').glob('*.json'), 10):
            package.add_file(path, 'logs/update-history/' + path.name)
        for name in ('update-result.json', 'update-details.txt', 'update-check.txt', 'diagnostics-latest.json',
                     'launcher-error.log', 'launcher-ui.log'):
            if (logs / name).exists():
                package.add_file(logs / name, 'logs/' + name)
        for path in newest(logs.glob('host-files-recovery-*.jsonl'), 2):
            package.add_file(path, 'logs/' + path.name)
        windows = newest((logs / 'windows-diag').glob('*'), 1) if request['include_windows_logs'] else []
        if windows and windows[0].is_dir():
            for path in sorted(windows[0].iterdir()):
                if path.is_file() and path.suffix in ('.json', '.txt', '.log', '.xml', '.jsonl'):
                    package.add_file(path, 'windows/' + path.name, tail=4 * 1024 * 1024)
        else:
            package.note('windows', 'unavailable', reason='switched off' if not request['include_windows_logs'] else
                         'Журналы Windows ещё не собираются этой версией программы.')
        for index, image in enumerate(request['images']):
            path = Path(image)
            try:
                if path.suffix.lower() not in ('.png', '.jpg', '.jpeg') or not path.is_file() or path.is_symlink():
                    package.note('image ' + str(index + 1), 'error', reason='not a PNG or JPEG file')
                elif path.stat().st_size > MAX_IMAGE_BYTES:
                    package.note('image ' + str(index + 1), 'skipped_limit', bytes=path.stat().st_size)
                elif package.add_bytes('images/' + str(index + 1) + path.suffix.lower(), path.read_bytes()):
                    package.note('image ' + str(index + 1), 'ok')
            except OSError as error:
                package.note('image ' + str(index + 1), 'error', reason=type(error).__name__)
        manifest = {'schema': 1, 'report_id': request['report_id'], 'created_utc': created, 'raw_bytes': package.used,
                    'limit_bytes': MAX_REPORT_BYTES, 'sources': package.sources, 'files': package.files}
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=1))
    os.replace(partial, target)
    status = {'report_id': request['report_id'], 'zip': target.relative_to(root).as_posix(), 'bytes': target.stat().st_size,
              'created_utc': created, 'unavailable': [item['source'] for item in package.sources if item['status'] != 'ok']}
    (folder / 'status.json').write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
    prune_reports(logs / 'reports', keep=request['report_id'])
    return status


def prune_reports(folder, keep):
    """Keep the newest reports; a report not yet sent stays until it is replaced by newer ones."""
    for old in newest([path for path in folder.iterdir() if path.is_dir() and re.fullmatch(r'[a-f0-9]{32}', path.name)], 100)[KEEP_REPORTS:]:
        if old.name != keep and not old.is_symlink():
            shutil.rmtree(old, ignore_errors=True)

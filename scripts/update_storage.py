"""Persist verified component catalogs and collect only proven update-owned data."""
import hashlib
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_config import PYTHON
import app_update_v2 as update
import release_portable as release
from file_snapshot import locked_read, remove_owned_file
from profile_store import _pinned_directory, _plain

CATALOG = 'state/components/current.json'
CATALOG_PREFIX = 'state/components/catalogs/'
TRANSACTION = r'app-update-[a-f0-9]{32}'
FINISHED = {'committed', 'rolled_back'}


def _raw(path):
    path = release.pack.file_under(path.parent, path.name)
    with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
        data = stream.read(release.JSON_LIMIT + 1)
    if len(data) > release.JSON_LIMIT:
        raise ValueError('Oversized update metadata')
    return data


def _pointer(value):
    if (type(value) is not dict or set(value) != {'schema', 'catalog', 'sha256', 'release_id'}
            or type(value['schema']) is not int or value['schema'] != 2
            or type(value['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
            or value['catalog'] != value['sha256'] + '.json'
            or type(value['release_id']) is not str):
        raise ValueError('Invalid component catalog pointer')
    return value


def current_index(target):
    target = release.absolute(target)
    pointer = _pointer(release.read_json(release.pack.file_under(target, CATALOG)))
    path = release.pack.file_under(target, CATALOG_PREFIX + pointer['catalog'])
    index = update.read_index(path, pointer['sha256'])
    if index['release']['release_id'] != pointer['release_id']:
        raise ValueError('Component pointer release differs from catalog')
    return path, pointer['sha256'], index


def build_catalog(index_path, sha, target, generated):
    index_path, target, generated = map(release.absolute, (index_path, target, generated))
    if generated == target or target.is_relative_to(generated):
        raise ValueError('Generated catalogs must use a separate staging directory')
    index = update.read_index(index_path, sha)
    update.verify_cache(index, target)
    raw = _raw(index_path)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('Component index changed after cache verification')
    name = CATALOG_PREFIX + sha + '.json'
    destination = generated/name
    release.directory(destination.parent)
    with _pinned_directory(destination.parent, create=False), destination.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    pointer = {'schema': 2, 'catalog': sha + '.json', 'sha256': sha,
               'release_id': index['release']['release_id']}
    release.atomic_json(generated/CATALOG, pointer)
    return [release.file_record(generated/name, name), release.file_record(generated/CATALOG, CATALOG)]


def _name(value, target):
    path = Path(value)
    if path.is_absolute():
        if path.parent != target/'state':
            raise ValueError('Rollback transaction is outside installation state')
        value = path.name
    else:
        value = str(value)
    if not re.fullmatch(TRANSACTION, value):
        raise ValueError('Invalid retained transaction name')
    return value


def _journal(path, target):
    value = release.read_json(release.pack.file_under(path.parent, path.name))
    if (type(value) is not dict or type(value.get('schema')) is not int or value['schema'] not in (1, 2, 3)
            or type(value.get('transaction')) is not str or not re.fullmatch(TRANSACTION, value['transaction'])
            or type(value.get('target')) is not str or not os.path.isabs(value['target'])
            or os.path.normcase(value['target']) != os.path.normcase(str(target))
            or value.get('phase') not in {'prepared', 'awaiting_probe', *FINISHED}
            or type(value.get('files')) is not list or not 1 <= len(value['files']) <= 40000):
        raise ValueError('Unknown or malformed update journal; preserve update storage')
    if value['schema'] >= 2:
        owned = value.get('target_id')
        if (type(owned) is not dict or set(owned) != {'device', 'file_id'}
                or any(type(owned[key]) is not int for key in owned) or owned != release.identity(target)):
            raise ValueError('Transaction target identity differs')
    if value['schema'] == 3:
        release.validate_release_info(value.get('release'))
        probe = value.get('probe')
        if (type(probe) is not dict or set(probe) != {'nonce', 'pid', 'created'}
                or type(probe['nonce']) is not str or not re.fullmatch('[a-f0-9]{32}', probe['nonce'])
                or type(probe['pid']) is not int or probe['pid'] < 0
                or type(probe['created']) is not str or not re.fullmatch('[0-9]{1,20}', probe['created'])):
            raise ValueError('Invalid transaction probe identity')
    seen = set()
    for row in value['files']:
        if (type(row) is not dict or not {'path', 'new', 'old', 'new_id', 'phase'} <= row.keys()
                or type(row['path']) is not str or not release.pack.safe_name(row['path'])
                or not (release.transaction_name(row['path']) or row['path'] == CATALOG
                        or re.fullmatch(CATALOG_PREFIX + '[a-f0-9]{64}\\.json', row['path']))
                or row['path'].casefold() in seen or row['phase'] not in ('pending', 'replacing', 'installed', 'restored')
                or row['new'] is None and row['old'] is None):
            raise ValueError('Invalid transaction file reference')
        seen.add(row['path'].casefold())
        for record in (row['new'], row['old']):
            if record is not None:
                if (type(record) is not dict or set(record) != {'path', 'bytes', 'sha256'}
                        or record['path'] != row['path'] or type(record['bytes']) is not int or record['bytes'] < 0):
                    raise ValueError('Invalid transaction inventory')
                update._digest(record['sha256'])
        owned = row['new_id']
        if row['new'] is not None and (type(owned) is not dict or set(owned) != {'device', 'file_id'}
                or any(type(owned[key]) is not int for key in owned) or owned['device'] < 0 or owned['file_id'] <= 0):
            raise ValueError('Invalid transaction file identity')
    return value


def _find_record(target, transaction, row, side):
    expected = row[side]
    if expected is None:
        return None
    base = target/'state'/transaction
    for root in (base/side, base/'generated', target):
        candidate = root/row['path']
        _plain(candidate)
        if candidate.exists():
            candidate = release.pack.file_under(root, row['path'])
            if release.file_record(candidate, row['path']) == expected:
                return candidate
    return None


def _journal_indexes(target, journal):
    indexes = []
    rows = journal['files']
    direct = [row for row in rows if re.fullmatch(CATALOG_PREFIX + '[a-f0-9]{64}\\.json', row['path'])]
    for row in direct:
        sha = Path(row['path']).stem
        for side in ('old', 'new'):
            if row[side] is None:
                continue
            path = _find_record(target, journal['transaction'], row, side)
            if path is None or row[side]['sha256'] != sha:
                raise ValueError('Transaction catalog is missing or changed')
            indexes.append(update.read_index(path, sha))
    for row in rows:
        if row['path'] != CATALOG:
            continue
        for side in ('old', 'new'):
            if row[side] is None:
                continue
            path = _find_record(target, journal['transaction'], row, side)
            if path is None:
                # Committed new pointers move again on the next update; its catalog row remains authoritative.
                if side == 'new' and direct and journal['phase'] in FINISHED:
                    continue
                raise ValueError('Transaction component pointer is missing or changed')
            pointer = _pointer(release.read_json(path))
            catalog = release.pack.file_under(target, CATALOG_PREFIX + pointer['catalog'])
            index = update.read_index(catalog, pointer['sha256'])
            if index['release']['release_id'] != pointer['release_id']:
                raise ValueError('Transaction pointer release differs')
            indexes.append(index)
    return indexes


def _scan(target, excluded=()):
    state = target/'state'
    for parent in (*reversed(state.parents), state):
        _plain(parent)
    journals, indexes, active = {}, [], set()
    if not state.exists():
        return journals, indexes, active
    with _pinned_directory(state, create=False):
        for path in sorted(state.iterdir()):
            name = path.name
            if name in excluded:
                continue
            if re.fullmatch(r'app-update.*\.json', name):
                journal = _journal(path, target)
                journals[journal['transaction']] = journal
                active.add(journal['transaction'])
            elif re.fullmatch(TRANSACTION, name):
                _plain(path)
                journal = _journal(path/'journal.json', target)
                if journal['transaction'] != name:
                    raise ValueError('Transaction directory and journal differ')
                journals.setdefault(name, journal)
                if journal['phase'] not in FINISHED:
                    active.add(name)
            elif re.fullmatch(r'app-updater-[a-f0-9]{32}', name):
                _plain(path)
                _plain(path/'index.json')
                if not (path/'index.json').exists():
                    continue
                raw = _raw(path/'index.json')
                indexes.append(update.read_index(path/'index.json', hashlib.sha256(raw).hexdigest()))
            elif re.fullmatch('[a-f0-9]{32}', name):
                _plain(path)
                prepared = path/'prepared.json'
                _plain(prepared)
                if prepared.exists():
                    value = release.read_json(prepared)
                    if (type(value) is not dict or set(value) != {'schema', 'index_sha256', 'omitted'}
                            or type(value['schema']) is not int or value['schema'] != 2
                            or type(value['omitted']) is not list
                            or any(type(item) is not str for item in value['omitted'])):
                        raise ValueError('Unknown prepared stage; preserve cache')
                    index = update.read_index(path/'index.json', value['index_sha256'])
                    if (len(set(value['omitted'])) != len(value['omitted'])
                            or not set(value['omitted']) <= {row['path'] for row in index['files']}):
                        raise ValueError('Prepared stage references unknown logical files')
                    indexes.append(index)
    return journals, indexes, active


def referenced_chunks(target, keep_transactions=(), *, _excluded=()):
    target = release.absolute(target)
    if isinstance(keep_transactions, (str, Path)):
        keep_transactions = (keep_transactions,)
    keep = {_name(item, target) for item in keep_transactions}
    journals, indexes, active = _scan(target, _excluded)
    pointer = target/CATALOG
    _plain(pointer)
    if pointer.exists():
        indexes.append(current_index(target)[2])
    if not keep <= journals.keys():
        raise ValueError('Retained rollback journal is missing')
    for name in keep | active:
        indexes.extend(_journal_indexes(target, journals[name]))
    return {row['sha256'] for index in indexes for row in index['chunks']}


def _tree(root, expected, required=()):
    allowed_directories = {Path('.')}
    for relative in expected:
        allowed_directories.update(Path(relative).parents)
    files, directories, seen = [], [], set()
    for directory, children, names in os.walk(root, followlinks=False):
        directory = Path(directory)
        _plain(directory)
        if directory.relative_to(root) not in allowed_directories:
            return None
        directories.append(directory)
        for child in children:
            _plain(directory/child)
        for child in names:
            path = release.pack.file_under(root, (directory/child).relative_to(root).as_posix())
            relative = path.relative_to(root).as_posix()
            if relative not in expected:
                return None
            seen.add(relative)
            row = expected[relative]
            if row is not None and release.file_record(path, row['path']) != row:
                raise ValueError('Completed transaction contents changed; preserve storage')
            files.append((path, release.identity(path)))
    if not set(required) <= seen:
        return None
    return files, directories


def _owned_tree(target, name, journal):
    expected = {'journal.json': None}
    for row in journal['files']:
        for side in ('old', 'new'):
            if row[side] is not None:
                expected[side+'/'+row['path']] = row[side]
        if row['new'] is not None:
            expected['generated/'+row['path']] = row['new']
    return _tree(target/'state'/name, expected, ('journal.json',))


def _ephemeral(root):
    raw = _raw(root/'index.json')
    sha = hashlib.sha256(raw).hexdigest()
    index = update.read_index(root/'index.json', sha)
    expected = {'index.json': release.file_record(root/'index.json', 'index.json')}
    if re.fullmatch('[a-f0-9]{32}', root.name):
        prepared = release.read_json(release.pack.file_under(root, 'prepared.json'))
        if (type(prepared) is not dict or set(prepared) != {'schema', 'index_sha256', 'omitted'}
                or type(prepared['schema']) is not int or prepared['schema'] != 2
                or prepared['index_sha256'] != sha or type(prepared['omitted']) is not list
                or any(type(name) is not str for name in prepared['omitted'])
                or len(set(prepared['omitted'])) != len(prepared['omitted'])
                or not set(prepared['omitted']) <= {row['path'] for row in index['files']}):
            raise ValueError('Unknown prepared stage; preserve storage')
        expected['prepared.json'] = release.file_record(root/'prepared.json', 'prepared.json')
        for row in index['files']:
            expected['app/'+row['path']] = {key: row[key] for key in ('path', 'bytes', 'sha256')}
        required = ('index.json', 'prepared.json')
        kind = 'stages'
    else:
        manifest = release.read_json(release.pack.file_under(root, 'toolkit-manifest.json'))
        if (type(manifest) is not dict or set(manifest) != {'schema', 'release_id', 'source_commit', 'source_inputs', 'files'}
                or type(manifest['schema']) is not int or manifest['schema'] != 1
                or manifest['release_id'] != index['release']['release_id']
                or manifest['source_commit'] != index['release']['source_commit']
                or type(manifest['source_inputs']) is not list
                or type(manifest['files']) is not list or not 1 <= len(manifest['files']) <= 15000):
            raise ValueError('Unknown toolkit provenance; preserve storage')
        seen, total = set(), 0
        for row in manifest['files']:
            if (type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'}
                    or not release.pack.safe_name(row['path']) or row['path'].casefold() in seen
                    or not (row['path'].startswith('py/') or re.fullmatch(r'scripts/[A-Za-z0-9_-]+\.py', row['path'])
                            or row['path'] == 'docs/third-party.md')
                    or type(row['bytes']) is not int or row['bytes'] < 0):
                raise ValueError('Invalid toolkit file inventory')
            update._digest(row['sha256'])
            seen.add(row['path'].casefold())
            total += row['bytes']
            expected[row['path']] = row
        if total > 300*1024**2 or not {'py/python.exe', 'scripts/app_update_v2.py', 'scripts/release_portable.py'} <= expected.keys():
            raise ValueError('Incomplete or oversized toolkit')
        if len(manifest['source_inputs']) != len(manifest['files']):
            raise ValueError('Toolkit source inventory is incomplete')
        mapped = set()
        for row in manifest['source_inputs']:
            if type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'} or not release.pack.safe_name(row['path']):
                raise ValueError('Invalid toolkit source inventory')
            name = row['path']
            if name.startswith(PYTHON + '/'):
                name = 'py/' + name[len(PYTHON + '/'):]
            if name in mapped or expected.get(name) != dict(row, path=name):
                raise ValueError('Toolkit sources differ from file inventory')
            mapped.add(name)
        expected['toolkit-manifest.json'] = release.file_record(root/'toolkit-manifest.json', 'toolkit-manifest.json')
        required = tuple(expected)
        kind = 'tools'
    return sha, index, kind, _tree(root, expected, required)


def _protected_ephemeral(target, journals, names):
    protected = set()
    for journal in journals:
        for key, pattern in (('prepared', '[a-f0-9]{32}'), ('tools', r'app-updater-[a-f0-9]{32}')):
            if key in journal:
                name = journal[key]
                if type(name) is not str or not re.fullmatch(pattern, name):
                    raise ValueError('Unknown ephemeral journal reference')
                protected.add(name)
    marker = target/'state/update-recovery.json'
    _plain(marker)
    if marker.exists():
        value = release.read_json(release.pack.file_under(marker.parent, marker.name), 4096)
        if type(value) is not dict or set(value) != {'schema', 'work'} or type(value['schema']) is not int or value['schema'] not in (1, 2):
            raise ValueError('Unknown recovery toolkit reference')
        name = value['work']
        if value['schema'] == 1:
            path = Path(name)
            if path.parent != target/'state':
                raise ValueError('Legacy recovery toolkit is outside state')
            name = path.name
        if type(name) is not str or not re.fullmatch(r'app-updater-[a-f0-9]{32}', name):
            raise ValueError('Unsafe recovery toolkit reference')
        protected.add(name)
    executable = release.absolute(sys.executable)
    if executable.name.casefold() == 'python.exe' and executable.parent.name == 'py' and executable.parent.parent.parent == target/'state':
        protected.add(executable.parent.parent.name)
    if not protected <= names:
        raise ValueError('Referenced ephemeral update directory is missing')
    return protected


def _process_guard(target, keep, journal, allowed_probe):
    processes = release.project_processes(target)
    if type(processes) is not dict:
        raise RuntimeError('Project process snapshot is unconfirmed')
    if allowed_probe is not None:
        if (type(allowed_probe) is not dict or set(allowed_probe) != {'nonce', 'pid', 'created'}
                or journal.get('schema') != 3 or allowed_probe != journal.get('probe')):
            raise ValueError('Allowed probe differs from accepted transaction')
        receipt = release.read_json(release.pack.file_under(target, 'state/update-accepted.json'), 8192)
        expected = dict(schema=1, committed=True, transaction=keep, root=str(target),
                        release_id=journal['release']['release_id'], source_commit=journal['release']['source_commit'], **allowed_probe)
        if receipt != expected:
            raise ValueError('Probe receipt differs from accepted transaction')
        actual = release.runtime.process_identity(allowed_probe['pid'])
        if (not actual or actual.get('pid') != allowed_probe['pid']
                or str(actual.get('created_filetime')) != allowed_probe['created']
                or os.path.normcase(actual.get('image', '')) != os.path.normcase(str(target/'Launcher.exe'))
                or processes.get(allowed_probe['pid']) != actual):
            raise RuntimeError('Accepted launcher process identity differs')
        processes = {pid: value for pid, value in processes.items() if pid != allowed_probe['pid']}
    if processes:
        raise RuntimeError('Stop other project processes before collecting update storage')


def _delete_tree(tree):
    files, directories = tree
    for path, owned in files:
        with _pinned_directory(path.parent, create=False):
            remove_owned_file(path, owned)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        with _pinned_directory(directory.parent, create=False):
            _plain(directory)
            directory.rmdir()


def cleanup(target, keep_transaction, allowed_probe=None):
    """Call after accepted commit and after releasing the installation transaction lock."""
    target = release.absolute(target)
    keep = _name(keep_transaction, target)
    with _pinned_directory(target, create=False), release.runtime.ProjectLock(target) as acquired:
        if not acquired:
            raise RuntimeError('Project storage is busy')
        journals, _, active = _scan(target)
        if keep not in journals or journals[keep]['phase'] != 'committed':
            raise ValueError('Keep an accepted committed rollback transaction before cleanup')
        _process_guard(target, keep, journals[keep], allowed_probe)
        # Unknown contents can hide references: preserve everything until ownership is understood.
        trees, preserved = {}, []
        for name, journal in journals.items():
            if name == keep or name in active or journal['schema'] == 1 or journal['phase'] not in FINISHED:
                continue
            tree = _owned_tree(target, name, journal)
            if tree is None:
                preserved.append(name)
            else:
                trees[name] = tree
        ephemeral = {}
        for root in (target/'state').iterdir():
            if re.fullmatch(r'([a-f0-9]{32}|app-updater-[a-f0-9]{32})', root.name):
                # Unmarked UUID directories remain outside the protocol ownership boundary.
                if re.fullmatch('[a-f0-9]{32}', root.name) and not (root/'prepared.json').exists():
                    continue
                try:
                    ephemeral[root.name] = _ephemeral(root)
                    if ephemeral[root.name][3] is None:
                        preserved.append(root.name)
                except (OSError, ValueError):
                    # The earlier scan has validated index references; unproven content stays intact.
                    preserved.append(root.name)
        protected = _protected_ephemeral(target, [journals[name] for name in {keep, *active}], set(ephemeral) | set(preserved))
        # Retain a usable toolkit for the current catalog and rollback even after its recovery marker moves.
        protected_indexes = _journal_indexes(target, journals[keep])
        if (target/CATALOG).exists():
            protected_indexes.append(current_index(target)[2])
        for index in protected_indexes:
            matches = [(name, data) for name, data in ephemeral.items() if data[2] == 'tools' and data[1] == index and data[3] is not None]
            if matches:
                protected.add(max(matches, key=lambda item: (target/'state'/item[0]).stat().st_mtime_ns)[0])
        if active:
            protected.update(ephemeral)
        disposable = {name: data for name, data in ephemeral.items() if name not in protected and data[3] is not None}
        referenced = referenced_chunks(target, keep_transactions=(keep, *[name for name in preserved if name in journals]), _excluded=disposable)
        candidates = [(path, release.identity(path)) for path in update.cache_gc_candidates(target, referenced)]
        result = {'deleted_transactions': [], 'deleted_chunks': [], 'deleted_stages': [], 'deleted_tools': [], 'preserved': preserved}
        for name, (files, directories) in trees.items():
            _delete_tree((files, directories))
            result['deleted_transactions'].append(name)
        for name, (_, _, kind, tree) in disposable.items():
            _delete_tree(tree)
            result['deleted_'+kind].append(name)
        for path, owned in candidates:
            with _pinned_directory(path.parent, create=False):
                remove_owned_file(path, owned)
            result['deleted_chunks'].append(path.name)
        return result

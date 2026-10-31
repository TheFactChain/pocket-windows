"""Build an independent, source-accounted updater and verify guest build inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_config import PYTHON
import pack_existing_runtime as pack
from file_snapshot import locked_read
from profile_store import _plain, _pinned_directory

PYTHON_ROOT_FILES = ('python.exe', 'python3.dll', 'python312.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
GUEST_INPUTS = ('guest/files/GuestFiles.cs', 'guest/GuestEnvironment.cs', 'guest/GuestClipboard.cs', 'guest/ClipboardFiles.cs',
                'guest/ClipboardStage.cs', 'guest/ClipboardProtocol.cs', 'guest/ClipboardTransport.cs',
                'ui/app.manifest', 'scripts/Build-GuestFiles.ps1', 'scripts/Build-HostFiles.ps1')
EXCLUDED = {'test', 'tests', '__pycache__', 'site-packages', 'tkinter', 'idlelib', 'ensurepip'}
MAX_FILES = 15000
MAX_BYTES = 300 * 1024 * 1024


def _git(source, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + source.as_posix(), '-C', str(source), *args],
                                   text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()


def record(root, name):
    path = pack.file_under(Path(root), name)
    with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
        return {'path': name, 'bytes': os.fstat(stream.fileno()).st_size,
                'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def validate_guest_build(source, component_directory=None):
    """Reject unproven or stale guest helpers; component_directory may be staging."""
    source = Path(source)
    component = Path(component_directory) if component_directory is not None else source / 'runtime/host-files'
    with locked_read(pack.file_under(component, 'build-manifest.json')) as stream:
        manifest = pack._json(stream)
    expected = [record(source, name) for name in GUEST_INPUTS]
    sources = manifest.get('local_sources') if type(manifest) is dict else None
    if (type(manifest) is not dict or type(manifest.get('schema')) is not int or manifest['schema'] != 1
            or manifest.get('component') != 'host-files' or type(sources) is not list
            or sources[:len(expected)] != expected):
        raise ValueError('Guest build has missing or stale local source provenance')
    if any(type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'} or type(row['bytes']) is not int
           or row != record(source, row['path']) for row in sources):
        raise ValueError('Local build source provenance differs from source')
    output = record(component, 'HostFilesGuest.exe')
    rows = manifest.get('files')
    if type(rows) is not list or [row for row in rows if type(row) is dict and row.get('name') == output['path']] != [
            {'name': output['path'], 'bytes': output['bytes'], 'sha256': output['sha256']}]:
        raise ValueError('Guest executable differs from its build provenance')
    return manifest


def validate_host_build(source, component_directory=None):
    """A complete host component must come from the current full pinned build."""
    source = Path(source)
    component = Path(component_directory) if component_directory is not None else source/'runtime/host-files'
    manifest = validate_guest_build(source, component)
    names = [*GUEST_INPUTS]
    names.extend(path.relative_to(source).as_posix() for path in sorted((source/'host/files').glob('*.cs'), key=lambda path: path.name.casefold()))
    names.append('host/files/dependency-patches/SMBServer.Signatures.cs')
    names.append('patches/smblibrary/smblibrary-1.5.8.patch')
    expected = [record(source, name) for name in names]
    outputs = []
    for name in pack.FILE_COMPONENT_FILES:
        filename = Path(name).name
        if filename == 'build-manifest.json':
            continue
        row = record(component, filename)
        outputs.append({'name': filename, 'bytes': row['bytes'], 'sha256': row['sha256']})
    rows = manifest.get('files')
    if (manifest.get('upstream_commit') != '2edbcf3161084b51dbe11b8a960b0bc7c224b851'
            or manifest.get('source_archive_sha256') != '2240c49f69aaffa5225e8316d5a5440ab2f20bc577bd3161091905f05b94d318'
            or manifest.get('local_sources') != expected or type(rows) is not list
            or any(type(row) is not dict or set(row) != {'name', 'bytes', 'sha256'}
                   or type(row['bytes']) is not int or type(row['name']) is not str for row in rows)
            or sorted(rows, key=lambda row: row['name']) != sorted(outputs, key=lambda row: row['name'])):
        raise ValueError('Host files component has missing, stale or unproven build inputs/outputs')
    return manifest


def _runtime_members(source):
    root = source / PYTHON
    members = [('py/' + name, PYTHON + '/' + name) for name in PYTHON_ROOT_FILES]
    for name in ('LICENSE.txt', 'NOTICE.txt'):
        if (root / name).exists():
            members.append(('py/' + name, PYTHON + '/' + name))
    for folder in ('Lib', 'DLLs'):
        base = root / folder
        _plain(base)
        for directory, directories, files in os.walk(base, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in EXCLUDED)
            for name in directories:
                _plain(Path(directory) / name)
            for name in sorted(files):
                path = Path(directory) / name
                if folder == 'Lib' and path.suffix.lower() in ('.pyc', '.pyo'):
                    continue
                if folder == 'DLLs' and (path.suffix.lower() not in ('.dll', '.pyd') or name.startswith(('_test', '_ctypes_test', '_tkinter', 'tcl', 'tk'))):
                    continue
                relative = path.relative_to(root).as_posix()
                members.append(('py/' + relative, PYTHON + '/' + relative))
    return members


def build(source, release_directory, release_id):
    source, destination = Path(source).absolute(), Path(release_directory).absolute()
    if not re.fullmatch(r'[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,8}', release_id):
        raise ValueError('Invalid release identifier')
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Toolkit output must be outside source')
    tracked = set(_git(source, 'ls-files').splitlines())
    scripts = {name for name in pack.APP_FILES if name.endswith('.py')}
    scripts.update(('scripts/app_update_v2.py', 'scripts/update_toolkit.py'))
    if not scripts <= tracked:
        raise ValueError('Updater scripts must be tracked source files')
    commit = _git(source, 'rev-parse', 'HEAD')
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Invalid source commit')
    members = _runtime_members(source) + [(name, name) for name in sorted(scripts)]
    if (source / 'docs/third-party.md').exists():
        members.append(('docs/third-party.md', 'docs/third-party.md'))
    if len(members) > MAX_FILES:
        raise ValueError('Toolkit file count exceeds limit')
    inputs = [record(source, name) for _, name in members]
    if sum(row['bytes'] for row in inputs) > MAX_BYTES:
        raise ValueError('Toolkit size exceeds limit')
    report = {'schema': 1, 'release_id': release_id, 'source_commit': commit, 'source_inputs': inputs,
              'files': [dict(row, path=name) for (name, _), row in zip(members, inputs)]}
    for parent in (*reversed(destination.parents), destination):
        _plain(parent)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / ('Pocket-Windows-' + release_id + '-updater.zip')
    checksum = output.with_name(output.name + '.sha256')
    if output.exists() or checksum.exists():
        raise ValueError('Toolkit output already exists')
    with _pinned_directory(destination, create=False), zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for (name, relative), expected in zip(members, inputs):
            path = pack.file_under(source, relative)
            digest, size = hashlib.sha256(), 0
            with _pinned_directory(path.parent, create=False), locked_read(path) as incoming, archive.open(name, 'w') as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    if size > expected['bytes']:
                        raise ValueError('Toolkit source grew while packaging: ' + relative)
                    outgoing.write(chunk)
            if size != expected['bytes'] or digest.hexdigest() != expected['sha256']:
                raise ValueError('Toolkit source changed while packaging: ' + relative)
        archive.writestr('toolkit-manifest.json', json.dumps(report, ensure_ascii=True, indent=2) + '\n')
    if _git(source, 'rev-parse', 'HEAD') != commit:
        raise ValueError('Toolkit source commit changed while packaging')
    result = record(destination, output.name)
    with checksum.open('x', encoding='ascii') as stream:
        stream.write(result['sha256'] + '  ' + output.name + '\n')
    return {'path': str(output), 'sha256_path': str(checksum), 'sha256': result['sha256'], 'bytes': result['bytes']}

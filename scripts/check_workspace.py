"""Read-only order check: tracked files must be sources only; the project root must hold only known folders."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

SOURCE_DIRS = {'docs', 'gateway', 'guest', 'host', 'launcher-wpf', 'patches', 'scripts', 'tests', 'ui'}
SOURCE_FILES = {'.gitattributes', '.gitignore', 'AGENTS.md', 'README.md', 'Start.cmd'}
BANNED_SUFFIXES = {'.zip', '.7z', '.exe', '.dll', '.qcow2', '.qcow', '.vhd', '.vhdx', '.vmdk', '.iso', '.img',
                   '.raw', '.fd', '.log', '.pcap', '.pcapng', '.pem', '.key', '.png', '.jpg', '.ppm', '.pyc'}
BANNED_NAMES = {'.env'}
MAX_TRACKED_BYTES = 3 * 1024 * 1024
RELEASE_NAME = re.compile(r'(release|compact-master)-\d{4}\.\d{2}\.\d{2}\.\d+(-final)?$')


def tracked_problems(source):
    """Return problems found among files Git tracks, so junk cannot be committed unnoticed."""
    files = subprocess.check_output(['git', '-c', 'safe.directory=' + source.as_posix(), '-C', str(source), 'ls-files', '-z'],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
    problems = []
    for name in filter(None, files.decode('utf-8').split('\0')):
        path = Path(name)
        file = source / name
        if len(path.parts) == 1 and name not in SOURCE_FILES:
            problems.append('Unexpected file in the repository root: ' + name)
        elif len(path.parts) > 1 and path.parts[0] not in SOURCE_DIRS:
            problems.append('Unexpected folder in the repository: ' + path.parts[0])
        if path.suffix.lower() in BANNED_SUFFIXES:
            problems.append('Binary, log or capture is tracked: ' + name)
        if path.name in BANNED_NAMES or path.name.startswith('.env'):
            problems.append('Secret file is tracked: ' + name)
        if file.is_file() and file.stat().st_size > MAX_TRACKED_BYTES:
            problems.append('Tracked file is larger than 3 MiB: ' + name)
    return problems


def workspace_problems(project):
    """Return problems found in the project root and in the service folder."""
    problems = []
    service = project / 'Разработка/Служебное'
    for item in sorted(service.iterdir()) if service.is_dir() else []:
        if item.is_dir() and not (RELEASE_NAME.fullmatch(item.name) or item.name in ('Разработка-и-история', '_temp')):
            problems.append('Service folder is not a release, a master or history: ' + item.name)
    for kind in ('release', 'compact-master'):
        versions = sorted(item.name for item in service.glob(kind + '-*') if RELEASE_NAME.fullmatch(item.name)) \
            if service.is_dir() else []
        if len(versions) > 2:
            problems.append('Keep at most two folders of ' + kind + ', found ' + str(len(versions)) + ': ' + ', '.join(versions))
    for item in sorted(service.glob('release-*/public')) + sorted(service.glob('release-*/app')) if service.is_dir() else []:
        problems.append('Build leftover to delete after the ZIP is verified: ' + str(item.relative_to(project)))
    sources = project / 'Разработка/Исходники'
    for name in ('bin', 'obj'):
        if (sources / 'launcher-wpf' / name).exists():
            problems.append('Build output left in sources: launcher-wpf/' + name)
    # The release inventory compares runtime/ with its manifest, so a bytecode cache makes `prepare` fail.
    for item in sorted((sources / 'runtime').rglob('__pycache__')) if (sources / 'runtime').is_dir() else []:
        problems.append('Bytecode cache inside runtime (run bundled Python with -B): ' + str(item.relative_to(project)))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--project', type=Path, help='project root; defaults to two levels above the sources')
    args = parser.parse_args(argv)
    project = args.project or args.source.parents[1]
    problems = tracked_problems(args.source) + (workspace_problems(project) if (project / 'Разработка').is_dir() else [])
    for problem in problems:
        print(problem)
    print('OK' if not problems else str(len(problems)) + ' problem(s)')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())

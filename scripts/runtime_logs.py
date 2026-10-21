"""Budget only explicit normal runtime sessions; never discover/delete build evidence.

Caller supplies pinned directories and verified process-exit checks under its lifecycle
lock. Receipts, state and QMP history count toward the budget but are never deleted.
If protected/live logs exceed the cap, budget_met stays False: callers must refuse
new sessions rather than claiming cleanup succeeded. Stream producers must separately
enforce MAX_STREAM_BYTES while running; pruning does not bound concurrent writes.
"""
import os
from pathlib import Path
import re
import stat
import threading
from contextlib import ExitStack

from file_snapshot import remove_owned_file

MAX_LOG_BYTES = 50 * 1024 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024
NORMAL_PAIR_RESERVE = 7 * MAX_STREAM_BYTES + 2 * 1024 * 1024
GUEST_DELETABLE = frozenset({'stderr.txt', 'runtime-serial.log'})
GUEST_PROTECTED = frozenset({'state.json', 'qmp-events.json', 'qmp-events.jsonl',
                             'stop-session.json', 'work-lifecycle.lock'})
LAUNCH_DELETABLE = frozenset({role + '-controller.' + channel + '.log'
                            for role in ('work', 'gateway') for channel in ('stdout', 'stderr')}
                           | {'supervisor.stdout.log', 'supervisor.stderr.log'})
LAUNCH_PROTECTED = frozenset({'receipt.json', 'stop-request.json'})


class CappedWriter:
    """New binary log, capped at its writer; excess input is counted and discarded."""
    def __init__(self, path, limit=MAX_STREAM_BYTES):
        from profile_store import _pinned_directory
        if type(limit) is not int or not 0 <= limit <= MAX_STREAM_BYTES:
            raise ValueError('A stream cap between zero and four MiB is required')
        self.limit, self.received_bytes, self.written_bytes = limit, 0, 0
        self._stack = ExitStack()
        try:
            path = Path(os.path.abspath(path))
            self._stack.enter_context(_pinned_directory(path.parent, create=False))
            self._stream = self._stack.enter_context(path.open('xb', buffering=0))
            _plain(path)
        except BaseException:
            self._stack.close()
            raise

    def write(self, data):
        if not isinstance(data, bytes):
            raise TypeError('Binary stream bytes are required')
        self.received_bytes += len(data)
        retained = memoryview(data)[:max(0, self.limit-self.written_bytes)]
        while retained:
            count = self._stream.write(retained)
            if not count:
                raise OSError('Log write did not complete')
            self.written_bytes += count
            retained = retained[count:]
        return len(data)

    def close(self):
        self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class CappedDrain:
    """Own a binary pipe until EOF, even after cap or disk failure. Never kill its writer."""
    def __init__(self, source, destination, limit=MAX_STREAM_BYTES):
        self._source = source
        self._owns_writer = not isinstance(destination, CappedWriter)
        self._writer = destination if isinstance(destination, CappedWriter) else CappedWriter(destination, limit)
        self.received_bytes, self.failed = 0, False
        self._thread = threading.Thread(target=self._run, daemon=True)
        try:
            self._thread.start()
        except BaseException:
            if self._owns_writer:
                self._writer.close()
            source.close()
            raise

    @property
    def written_bytes(self):
        return self._writer.written_bytes

    def _run(self):
        try:
            read = getattr(self._source, 'read1', self._source.read)
            while True:
                data = read(64 * 1024)
                if not data:
                    break
                self.received_bytes += len(data)
                if not self.failed:
                    try:
                        self._writer.write(data)
                    except (OSError, ValueError, TypeError):
                        self.failed = True
        except (OSError, ValueError):
            self.failed = True
        finally:
            for owned in ((self._source,self._writer) if self._owns_writer else (self._source,)):
                try:
                    owned.close()
                except (OSError, ValueError):
                    self.failed = True

    def join(self, timeout=None):
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def close(self, timeout=None):
        return self.join(timeout)


def start_capped_drain(source, destination, *, limit=MAX_STREAM_BYTES):
    return CappedDrain(source, destination, limit)


def _plain(path, directory=False):
    info = path.lstat()
    if (getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode)
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
        raise ValueError('Log path must be an ordinary unlinked file or directory')
    return info


def _descriptors(sessions):
    if type(sessions) not in (list, tuple):
        raise ValueError('Explicit normal session descriptors are required')
    result, seen = [], set()
    for session in sessions:
        if (type(session) is not dict or set(session) != {'directory', 'normal', 'exited', 'processes'}
                or session['normal'] is not True or type(session['exited']) is not bool
                or type(session['processes']) is not list):
            raise ValueError('Invalid normal session descriptor')
        name = session['directory']
        if (type(name) is not str or not re.fullmatch(r'(guest-(work|gateway)-boot|runtime-launch)-[A-Za-z0-9_-]{8,96}', name)
                or name.casefold() in seen):
            raise ValueError('Invalid or duplicated normal session directory')
        seen.add(name.casefold())
        for process in session['processes']:
            if (type(process) is not dict or any(type(process.get(key)) is not int or process[key] <= 0
                                                for key in ('pid', 'created_filetime'))
                    or type(process.get('image')) is not str or not process['image']):
                raise ValueError('A captured process identity is required')
        result.append(dict(session))
    return result


def _exited(session, process_gone):
    if not session['exited'] or not session['processes']:
        return False
    try:
        return all(process_gone(process) is True for process in session['processes'])
    except (OSError, RuntimeError, ValueError):
        return False


def prune_logs(root, sessions, *, process_gone, pin_directory, budget_bytes=MAX_LOG_BYTES):
    """Prune allowlisted logs from explicitly proven exited normal sessions, oldest first.

    sessions items: {directory: basename under logs, normal: True, exited: bool,
    processes: [captured {pid, created_filetime, image, ...} for ALL writers]}.
    process_gone must fail closed: True only when that exact process is proven gone.
    pin_directory must deny rename/delete of the directory AND its ancestors.
    No receipt or process discovery is performed here; caller retains that authority.
    """
    if (type(budget_bytes) is not int or not 0 <= budget_bytes <= MAX_LOG_BYTES
            or not callable(process_gone) or not callable(pin_directory)):
        raise ValueError('A bounded budget, lifecycle exit proof and directory pin are required')
    sessions = _descriptors(sessions)
    root = Path(os.path.abspath(root))
    logs = root / 'logs'
    report = {'budget_bytes': budget_bytes, 'considered_bytes': 0, 'remaining_bytes': 0,
              'protected_bytes': 0, 'removed_bytes': 0, 'removed_files': [], 'failures': [],
              'overgrown_streams': [], 'budget_met': False}
    candidates = []
    with pin_directory(logs):
        _plain(logs, directory=True)
        for session in sessions:
            directory = logs / session['directory']
            with pin_directory(directory):
                _plain(directory, directory=True)
                guest = directory.name.startswith('guest-')
                deletable = GUEST_DELETABLE if guest else LAUNCH_DELETABLE
                names = deletable | (GUEST_PROTECTED if guest else LAUNCH_PROTECTED)
                exited = _exited(session, process_gone)
                for name in sorted(names):
                    path = directory / name
                    try:
                        info = _plain(path)
                    except FileNotFoundError:
                        continue
                    report['considered_bytes'] += info.st_size
                    if name in deletable and info.st_size > MAX_STREAM_BYTES:
                        report['overgrown_streams'].append(path.relative_to(root).as_posix())
                    if not exited or name not in deletable:
                        report['protected_bytes'] += info.st_size
                        continue
                    candidates.append((info.st_mtime_ns, path, info.st_size,
                                       {'device': info.st_dev, 'file_id': info.st_ino}, session))
        report['remaining_bytes'] = report['considered_bytes']
        for _, path, size, identity, session in sorted(candidates, key=lambda item: (item[0], str(item[1]))):
            if report['remaining_bytes'] <= budget_bytes:
                break
            try:
                with pin_directory(path.parent):
                    _plain(path.parent, directory=True)
                    if not _exited(session, process_gone):
                        raise ValueError('Session exit is no longer proven')
                    if remove_owned_file(path, identity):
                        report['removed_files'].append(path.relative_to(root).as_posix())
                        report['removed_bytes'] += size
                        report['remaining_bytes'] -= size
                    else:
                        # It was already removed; the scanned bytes are no longer retained.
                        report['remaining_bytes'] -= size
            except (OSError, RuntimeError, ValueError):
                report['failures'].append(path.relative_to(root).as_posix())
        report['budget_met'] = report['remaining_bytes'] <= budget_bytes and not report['failures']
    return report

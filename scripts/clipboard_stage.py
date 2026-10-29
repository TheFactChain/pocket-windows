"""Boot-scoped leases for completed private clipboard copies. No clipboard IO."""
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess

import clipboard_files as core

MAX_LEASE_BYTES = 64 * 1024 * 1024
_STAGE = re.compile(r'clipboard-[0-9a-f]{32}\Z')
_BOOT = re.compile(r'[0-9]{14}\.[0-9]{6}Z\Z')


def detect_boot_id():
    """A failed/unknown boot identity permits publication, never automatic deletion."""
    script = ("$ErrorActionPreference='Stop';"
              "$o=Get-CimInstance -ClassName Win32_OperatingSystem;"
              "$o.LastBootUpTime.ToUniversalTime().ToString('yyyyMMddHHmmss.ffffffZ',"
              "[Globalization.CultureInfo]::InvariantCulture)")
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        value = result.stdout.decode('ascii').strip() if result.returncode == 0 and len(result.stdout) < 128 else ''
        return value if _BOOT.fullmatch(value) else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def _identity(handle):
    volume, high, low = core._identity(handle)
    if not (high or low):
        raise ValueError('unusable marker identity')
    return {'volume': f'{volume:016x}', 'file_id': f'{(high << 32) | low:016x}'}


def _tuple(value):
    if (type(value) is not dict or set(value) != {'volume', 'file_id'}
            or any(type(value[k]) is not str or re.fullmatch(r'[0-9a-f]{16}', value[k]) is None for k in value)):
        raise ValueError('invalid marker identity')
    volume, index = int(value['volume'], 16), int(value['file_id'], 16)
    if volume > 0xffffffff or not index:
        raise ValueError('unusable marker identity')
    return volume, index >> 32, index & 0xffffffff


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate lease key')
        value[key] = item
    return value


class Cache:
    def __init__(self, parent, boot_id=None):
        self.parent = Path(os.path.abspath(parent))
        if re.match(r'^[A-Za-z]:\\', str(self.parent)) is None:
            raise ValueError('a local private parent is required')
        if boot_id is not None and (type(boot_id) is not str or _BOOT.fullmatch(boot_id) is None):
            raise ValueError('invalid boot id')
        self.boot_id = boot_id
        handles = core._pins(self.parent)
        try:
            self.identity = core._identity(handles[-1])
        finally:
            for handle in reversed(handles):
                core._kernel.CloseHandle(handle)

    def _pins(self):
        handles = core._pins(self.parent)
        try:
            if core._identity(handles[-1]) != self.identity:
                raise ValueError('private parent was replaced')
            return handles
        except BaseException:
            for handle in reversed(handles):
                core._kernel.CloseHandle(handle)
            raise

    def prepare(self, receiver):
        lease = receiver.export_lease(self.boot_id)
        stage = Path(receiver.stage)
        if stage.parent != self.parent or not _STAGE.fullmatch(stage.name):
            raise ValueError('receiver belongs to another private parent')
        marker = self.parent / (stage.name + '.lease.json')
        pins = self._pins()
        identity = None
        try:
            parent_handle = core._pin(stage.parent)
            try:
                if core._identity(parent_handle) != self.identity:
                    raise ValueError('receiver belongs to another private parent')
            finally:
                core._kernel.CloseHandle(parent_handle)
            # CreateNew and a held read/write handle exclude replacement until fsync.
            with core._write_open(marker) as target:
                import msvcrt
                identity = _identity(msvcrt.get_osfhandle(target.fileno()))
                value = dict(lease, marker=identity)
                payload = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii')
                if len(payload) > MAX_LEASE_BYTES:
                    raise ValueError('lease metadata limit')
                core._space(self.parent, len(payload))
                if target.write(payload) != len(payload):
                    raise OSError('short lease write')
                target.flush()
                os.fsync(target.fileno())
            # The durable ticket now owns explicit abort/park. An incidental
            # Receiver.close (including a late invalid frame) must not delete it.
            ticket = Ticket(self, receiver, stage, marker, identity, list(receiver.roots))
            receiver.detach()
            return ticket
        except BaseException:
            # Publication has not happened; remove only our own failed marker.
            if identity is not None:
                try:
                    core._delete_owned(marker, _tuple(identity), False)
                except (OSError, ValueError):
                    pass
            raise
        finally:
            for handle in reversed(pins):
                core._kernel.CloseHandle(handle)

    def cleanup(self, current_paths):
        # No foreign-path probing, UNC resolution or guesses about 8.3 aliases.
        confirmed_clear = type(current_paths) in (list, tuple) and not current_paths
        result = dict(removed=0, retained=0, failed=0, cleanup_unconfirmed=self.boot_id is None or not confirmed_clear)
        try:
            pins = self._pins()
        except (OSError, ValueError):
            return dict(result, failed=1, cleanup_unconfirmed=True)
        stages, markers = set(), set()
        try:
            with os.scandir(self.parent) as children:
                for scanned, child in enumerate(children):
                    if scanned >= 2 * core.MAX_ENTRIES:
                        result['cleanup_unconfirmed'] = True
                        break
                    if _STAGE.fullmatch(child.name):
                        stages.add(child.name)
                    if not child.name.endswith('.lease.json') or not _STAGE.fullmatch(child.name[:-11]):
                        continue
                    markers.add(child.name[:-11])
                    if result['removed'] + result['retained'] + result['failed'] >= core.MAX_ENTRIES:
                        result['cleanup_unconfirmed'] = True
                        break
                    marker = self.parent / child.name
                    try:
                        value, marker_identity = self._read(marker)
                        if self.boot_id is None or value['boot_id'] is None or not confirmed_clear:
                            result['retained'] += 1
                            result['cleanup_unconfirmed'] = True
                            continue
                        if value['boot_id'] == self.boot_id:
                            result['retained'] += 1
                            continue
                        del value['marker']
                        core.Receiver.resume_cleanup(self.parent, value)
                        core._delete_owned(marker, marker_identity, False)
                        result['removed'] += 1
                    except (OSError, ValueError, TypeError, KeyError):
                        result['failed'] += 1
                        result['cleanup_unconfirmed'] = True
        finally:
            for handle in reversed(pins):
                core._kernel.CloseHandle(handle)
        if stages - markers:
            result['retained'] += len(stages - markers)
            result['cleanup_unconfirmed'] = True
        return result

    def _read(self, marker):
        import msvcrt
        with core._read_open(marker) as source:
            identity = core._identity(msvcrt.get_osfhandle(source.fileno()))
            if os.fstat(source.fileno()).st_size > MAX_LEASE_BYTES:
                raise ValueError('lease metadata limit')
            payload = source.read(MAX_LEASE_BYTES + 1)
            value = json.loads(payload, object_pairs_hook=_unique)
            if (type(value) is not dict or set(value) != {'schema', 'stage', 'boot_id', 'roots', 'entries', 'marker'}
                    or type(value['schema']) is not int or value['schema'] != 1
                    or value['stage'] != marker.name[:-11]
                    or value['boot_id'] is not None and (type(value['boot_id']) is not str or _BOOT.fullmatch(value['boot_id']) is None)
                    or _tuple(value['marker']) != identity):
                raise ValueError('invalid/replaced stage marker')
            return value, identity


class Ticket:
    def __init__(self, cache, receiver, stage, marker, identity, roots):
        self.cache, self.receiver = cache, receiver
        self.stage, self.marker, self.roots = stage, marker, roots
        self.identity, self.parked, self.aborted = identity, False, False

    def park(self):
        if self.parked:
            return
        if self.aborted:
            raise ValueError('ticket already aborted')
        # prepare already fsynced the marker. Once native publication succeeded,
        # even a missing/replaced marker must never trigger immediate stage deletion.
        self.receiver.park()
        self.receiver = None
        self.parked = True

    def abort(self):
        if self.parked:
            raise ValueError('published/parked copies cannot be aborted')
        if self.aborted:
            return
        # Caller authorizes abort only before any successful native publication.
        pins = self.cache._pins()
        try:
            self.receiver.release()
            if self.receiver.stage is not None:
                raise OSError('unpublished stage cleanup is unconfirmed')
            core._delete_owned(self.marker, _tuple(self.identity), False)
            self.receiver = None
            self.aborted = True
        finally:
            for handle in reversed(pins):
                core._kernel.CloseHandle(handle)

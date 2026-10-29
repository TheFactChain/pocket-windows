"""Acknowledged clipboard transfer over the gateway-free, restricted guest file NIC."""
import base64
import hmac
import json
import os
import ntpath
import struct
import select
import queue
import uuid
from datetime import datetime, timezone
import socket
import threading
import time
import zlib
from contextlib import ExitStack

MAX_CHARS = 4 * 1024 * 1024  # UTF-16 code units; native text is bounded to 8 MiB.
MAX_LINE = 16 * 1024 * 1024
MAX_SEQUENCE = 9007199254740991
EXCLUDE_FORMAT = 'ExcludeClipboardContentFromMonitorProcessing'
GUEST_PORT = 27844
HELLO_IDLE_TIMEOUT = 300
HELLO_MESSAGE_TIMEOUT = 2
HELLO_MAX_BYTES = 4096
MAX_DROP_BYTES = 64 * 1024 * 1024
PRIVATE_COPY = object()


def drop_paths(raw):
    if not 20 <= len(raw) <= MAX_DROP_BYTES:
        raise ValueError('Invalid file clipboard size')
    offset, _, _, _, wide = struct.unpack_from('<IiiII', raw)
    # fWide is a Win32 BOOL; WinForms uses -1, and every nonzero value means Unicode.
    if not 20 <= offset < len(raw) or (wide and offset % 2):
        raise ValueError('Invalid file clipboard header')
    unit, terminator = (2, b'\0\0\0\0') if wide else (1, b'\0\0')
    end = raw.find(terminator, offset)
    while end >= 0 and (end - offset) % unit:
        end = raw.find(terminator, end + 1)
    if end < 0:
        raise ValueError('Unterminated file clipboard')
    paths = raw[offset:end].decode('utf-16-le' if wide else 'mbcs').split('\0')
    if not 1 <= len(paths) <= 100000 or any(not path or len(path) > 32767 or not ntpath.isabs(path)
                                         or not ntpath.splitdrive(path)[0] for path in paths):
        raise ValueError('Invalid file clipboard paths')
    return paths


def drop_data(paths):
    if not paths or len(paths) > 100000 or any(not path or '\0' in path or not ntpath.isabs(path)
                                             or not ntpath.splitdrive(path)[0] for path in paths):
        raise ValueError('Invalid file clipboard paths')
    raw = struct.pack('<IiiII', 20, 0, 0, 0, 1) + ('\0'.join(paths) + '\0\0').encode('utf-16-le')
    if len(raw) > MAX_DROP_BYTES:
        raise ValueError('File clipboard exceeds its limit')
    return raw


def packet(nonce, kind, **fields):
    return (json.dumps(dict(v=2, n=nonce, t=kind, **fields), separators=(',', ':')) + '\n').encode('ascii')


def text_limit_error(text):
    if type(text) is str:
        if len(text) > MAX_CHARS:
            return 'CLIPBOARD_TEXT_TOO_LARGE'
        try:
            if len(text.encode('utf-16-le')) > MAX_CHARS * 2:
                return 'CLIPBOARD_TEXT_TOO_LARGE'
        except UnicodeError:
            pass
    return None


def encode(nonce, text, sequence=1, origin='guest'):
    """One protocol line for a copied text, or None when it is empty or too long to send."""
    if type(text) is not str or not text or len(text) > MAX_CHARS or type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE or origin not in ('host', 'guest'):
        return None
    try:
        raw = text.encode('utf-8')
        if len(text.encode('utf-16-le')) > MAX_CHARS * 2:
            return None
    except UnicodeError:
        return None
    packer = zlib.compressobj(6, zlib.DEFLATED, -15)
    packed = packer.compress(raw) + packer.flush()
    line = packet(nonce, 'text', s=sequence, o=origin, z=1, d=base64.b64encode(packed).decode('ascii'))
    return line if len(line) <= MAX_LINE else None


def decode_packet(line, nonce):
    try:
        if len(line) > MAX_LINE:
            return None
        message = json.loads(line.decode('utf-8'))
        if (type(message) is not dict or type(message.get('v')) is not int or message['v'] != 2
                or type(message.get('n')) is not str or not hmac.compare_digest(message['n'].encode(), nonce.encode())
                or message.get('o') not in ('host', 'guest')):
            return None
        if message.get('t') == 'hello':
            return message
        if message.get('t') == 'ping':
            return message if set(message) == {'v', 'n', 't', 'o'} and message['o'] == 'guest' else None
        if type(message.get('s')) is not int or not 1 <= message['s'] <= MAX_SEQUENCE:
            return None
        if message.get('t') == 'skip':
            return message
        if message.get('t') == 'ack':
            return message if (type(message.get('ok')) is bool
                and type(message.get('applied', False)) is bool
                and ('i' not in message or valid_transfer_id(message['i']))) else None
        if message.get('t') in ('files', 'stored', 'cancel'):
            identifier = message.get('b', {}).get('id') if type(message.get('b')) is dict else message.get('i')
            if message['t'] == 'files' and type(message.get('b')) is not dict:
                return None
            return message if valid_transfer_id(identifier) else None
        if (message.get('t') != 'text' or type(message.get('z')) is not int or message['z'] not in (0, 1)
                or type(message.get('d')) is not str):
            return None
        raw = base64.b64decode(message['d'].encode('ascii'), validate=True)
        if message['z']:
            unpacker = zlib.decompressobj(-15)
            raw = unpacker.decompress(raw, MAX_CHARS * 4)
            if unpacker.unconsumed_tail or not unpacker.eof or unpacker.unused_data:
                return None
        text = raw.decode('utf-8')
        if not 0 < len(text) <= MAX_CHARS or len(text.encode('utf-16-le')) > MAX_CHARS * 2:
            return None
        message['text'] = text
        return message
    except (ValueError, zlib.error, UnicodeError, TypeError, RecursionError):
        return None


def decode(line, nonce):
    """Text convenience for the shared protocol regression harness."""
    message = decode_packet(line, nonce)
    return message.get('text') if message else None


def valid_transfer_id(value):
    return type(value) is str and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def guest_hello(message):
    return (message is not None and set(message) == {'v', 'n', 't', 'o', 'ready'}
            and message.get('t') == 'hello' and message.get('o') == 'guest' and type(message['ready']) is bool)


class Bridge:
    """Retains the latest version until acknowledged; reconnects after every guest restart."""

    def __init__(self, port, nonce, clipboard, log=None, *, cache=None):
        self.port, self.nonce, self.clipboard, self.log = port, nonce, clipboard, log or (lambda message: None)
        self.stopping = threading.Event()
        self.gate = threading.RLock()
        self.clock, self.version = 0, (0, '')
        self.current = self.pending = None
        self.thread = None
        self.listener = self.connection = None
        self.state, self.code = 'waiting', 'CLIPBOARD_WAITING'
        self.last_transfer_utc = None
        self.confirmed_version = None
        self.latest_seen = None
        self.last_applied = None
        self.cache, self.private = cache, ExitStack()
        self.snapshot_known = False
        self.snapshot_paths = []
        self.incoming = None
        self.control = self.cancel = None
        self.source_signal = threading.Event()
        self.source_frames = queue.Queue(maxsize=1)
        self.cleanup_jobs = queue.Queue(maxsize=1)
        self.cleanup_busy = threading.Event()
        self.io_thread = None
        self.io_stopping = threading.Event()
        self.epoch = 0
        self.last_text = None
        self.text_retry = 0
        self.transfer_error = None

    def start(self):
        listener = socket.socket()
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', self.port))
            listener.listen(4)
            listener.settimeout(.3)
            self.port = listener.getsockname()[1]
            self.listener = listener
            if self.cache is None and hasattr(self.clipboard, 'set_files'):
                import session_carrier as private
                import clipboard_stage
                parent = private._local_app_data() / 'WindowsClaudePortable/clipboard-sessions'
                self.private.enter_context(private._private_base(parent, create=True))
                self.cache = clipboard_stage.Cache(parent, clipboard_stage.detect_boot_id())
            self.clipboard.start(self.local_change)
            if self.cache is not None:
                self.cache.cleanup(self.snapshot_paths if self.snapshot_known else None)
        except BaseException:
            self.stopping.set()
            self.clipboard.stop()
            listener.close()
            self.listener = None
            self.private.close()
            raise
        self.io_thread = threading.Thread(target=self.file_io, name='clipboard-files', daemon=True)
        self.io_thread.start()
        self.thread = threading.Thread(target=self.run, name='clipboard-bridge', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        for connection in (self.connection, self.listener):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.clipboard.stop()
        if self.thread is not None:
            self.thread.join()
        self.drop_incoming()
        self.io_stopping.set()
        self.source_signal.set()
        if self.io_thread is not None:
            self.io_thread.join()
        self.private.close()

    def local_change(self, text):
        with self.gate:
            self.snapshot_known = text is not PRIVATE_COPY
            self.snapshot_paths = list(text) if isinstance(text, list) else []
            if self.clock >= MAX_SEQUENCE:
                self.transfer_error = 'CLIPBOARD_VERSION_LIMIT'
                return
            self.clock += 1
            self.version = (self.clock, 'host')
            if isinstance(text, list) and text:
                self.current = self.pending = {'paths': list(text), 'id': uuid.uuid4().hex,
                                              'version': self.version, 'stored': False}
            else:
                self.current = self.pending = encode(self.nonce, text, self.clock, 'host') or packet(
                    self.nonce, 'skip', s=self.clock, o='host')
            self.transfer_error = text_limit_error(text)
            if self.incoming is not None:
                self.incoming['cancelled'] = True
        self.source_signal.set()

    def acknowledge(self, version, ok=True, applied=False, identifier=None):
        fields = dict(s=version[0], o=version[1], ok=ok, applied=applied)
        if identifier is not None:
            fields['i'] = identifier
        return packet(self.nonce, 'ack', **fields)

    def confirmed(self, version):
        if version != self.confirmed_version:
            self.confirmed_version = version
            self.last_transfer_utc = datetime.now(timezone.utc).isoformat()

    def schedule_cleanup(self, owner):
        self.cleanup_busy.set()
        self.cleanup_jobs.put(owner)
        self.source_signal.set()

    def drop_incoming(self):
        with self.gate:
            owner, self.incoming = self.incoming, None
        if owner is not None:
            if self.io_thread is None:
                owner['ticket'].abort() if owner.get('ticket') else owner['receiver'].close()
            else:
                self.schedule_cleanup(owner)

    def file_io(self):
        from clipboard_files import Sender
        active, frames, retry, epoch = None, None, 0, -1
        try:
            while not self.io_stopping.is_set() or not self.cleanup_jobs.empty():
                try:
                    owner = self.cleanup_jobs.get_nowait()
                except queue.Empty:
                    owner = None
                if owner is not None:
                    try:
                        owner['ticket'].abort() if owner.get('ticket') else owner['receiver'].close()
                    except (OSError, ValueError):
                        self.transfer_error = 'CLIPBOARD_CLEANUP_FAILED'
                    finally:
                        self.cleanup_jobs.task_done()
                        if self.cleanup_jobs.empty():
                            self.cleanup_busy.clear()
                    continue
                with self.gate:
                    copy = self.pending if isinstance(self.pending, dict) else None
                    current_epoch = self.epoch
                if current_epoch != epoch or copy is not active or (copy is not None and copy['stored']):
                    if frames is not None:
                        frames.close()
                        frames = None
                    if active is not None and copy is not active:
                        with self.gate:
                            self.cancel = packet(self.nonce, 'cancel', s=active['version'][0], o=active['version'][1], i=active['id'])
                    active, retry, epoch = copy, 0, current_epoch
                if (copy is None or copy['stored'] or self.cancel is not None or not self.source_frames.empty()
                        or self.stopping.is_set() or time.monotonic() < retry):
                    self.source_signal.wait(.05)
                    self.source_signal.clear()
                    continue
                try:
                    if frames is None:
                        frames = Sender(copy['paths'], copy['id']).frames()
                    body = next(frames)
                    with self.gate:
                        if self.pending is copy and not copy['stored']:
                            self.source_frames.put_nowait((copy, epoch, packet(self.nonce, 'files', s=copy['version'][0], o=copy['version'][1], b=body)))
                except StopIteration:
                    frames, retry = None, time.monotonic() + 1
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    if frames is not None:
                        frames.close()
                    frames = None
                    with self.gate:
                        if self.pending is copy:
                            self.pending = None
                            self.transfer_error = 'CLIPBOARD_SOURCE_FAILED'
                        self.cancel = packet(self.nonce, 'cancel', s=copy['version'][0], o=copy['version'][1], i=copy['id'])
        finally:
            if frames is not None:
                frames.close()

    def next_outgoing(self):
        with self.gate:
            if self.control is not None:
                value, self.control = self.control, None
                return value
            if self.cancel is not None:
                value, self.cancel = self.cancel, None
                self.source_signal.set()
                return value
        try:
            copy, epoch, line = self.source_frames.get_nowait()
            self.source_signal.set()
            with self.gate:
                if self.pending is copy and not copy['stored'] and epoch == self.epoch:
                    return line
        except queue.Empty:
            pass
        with self.gate:
            pending = self.pending
        if isinstance(pending, bytes) and (pending != self.last_text or time.monotonic() >= self.text_retry):
            self.last_text, self.text_retry = pending, time.monotonic() + 1
            return pending
        return None

    def receive(self, line):
        message = decode_packet(line, self.nonce)
        if message is None:
            return None
        if message['t'] == 'ping':
            return packet(self.nonce, 'pong', o='host')
        if message['t'] == 'hello':
            if not guest_hello(message) or not message['ready']:
                return None
            self.drop_incoming()
            with self.gate:
                self.pending = self.current
                if isinstance(self.pending, dict):
                    self.pending['stored'] = False
                self.control = self.cancel = None
                self.latest_seen = None
                self.last_text = None
                self.epoch += 1
                self.source_signal.set()
            return b'\n' + packet(self.nonce, 'hello', o='host', ready=True)
        if message['t'] == 'files':
            return self.receive_files(message)
        with self.gate:
            version = (message['s'], message['o'])
            self.clock = max(self.clock, version[0])
            if message['t'] == 'ack':
                if (message['ok'] and version == self.version and
                        (not isinstance(self.current, dict) or message.get('i') == self.current['id'])):
                    self.pending = None
                    if message.get('applied'):
                        self.confirmed(version)
                self.source_signal.set()
                return None
            if message['t'] == 'stored':
                if isinstance(self.pending, dict) and version == self.version and message['i'] == self.pending['id']:
                    self.pending['stored'] = True
                self.source_signal.set()
                return None
            incoming = self.incoming
            if message['t'] == 'cancel':
                if incoming is not None and version == incoming['version'] and message['i'] == incoming['id']:
                    incoming['cancelled'] = True
                return None
            key = (version, message['t'], message.get('text'))
            if self.latest_seen is not None and (version < self.latest_seen[0] or
                    version == self.latest_seen[0] and key != self.latest_seen):
                return self.acknowledge(version, ok=False)
            self.latest_seen = key
            if incoming is not None and version < incoming['version'] and not incoming.get('cancelled'):
                return self.acknowledge(version, ok=False)
            expected = self.version
            if version <= expected:
                return self.acknowledge(version, applied=key == self.last_applied)
            if message['t'] == 'skip':
                self.version, self.pending = version, None
                self.current = packet(self.nonce, 'skip', s=version[0], o=version[1])
                if incoming is not None:
                    incoming['cancelled'] = True
                return self.acknowledge(version)
            if incoming is not None:
                incoming['cancelled'] = True
        def still_current():
            with self.gate:
                return not self.stopping.is_set() and self.version == expected
        # The window checks native clipboard sequence as well as this logical version before writing.
        applied = self.clipboard.set_text(message['text'], still_current)
        with self.gate:
            if applied and self.version == expected:
                self.version = version
                self.current = encode(self.nonce, message['text'], *version)
                self.pending = None
            if applied:
                if self.transfer_error == 'CLIPBOARD_TEXT_TOO_LARGE':
                    self.transfer_error = None
                self.last_applied = key
                self.confirmed(version)
            return self.acknowledge(version, ok=bool(applied or version <= self.version), applied=bool(applied))

    def receive_files(self, message):
        from clipboard_files import Receiver
        version, body = (message['s'], message['o']), message['b']
        identifier = body['id']
        with self.gate:
            self.clock = max(self.clock, version[0])
            incoming, current = self.incoming, self.version
            key = (version, 'files', identifier)
            if self.latest_seen is not None and (version < self.latest_seen[0] or
                    version == self.latest_seen[0] and key != self.latest_seen):
                return self.acknowledge(version, ok=False, identifier=identifier)
            if body.get('event') == 'begin':
                self.latest_seen = key
        if version <= current:
            return self.acknowledge(version, applied=key == self.last_applied, identifier=identifier)
        if incoming is not None and (incoming.get('cancelled') or version > incoming['version']):
            self.drop_incoming()
            incoming = None
        if incoming is not None and (version != incoming['version'] or identifier != incoming['id']):
            return self.acknowledge(version, ok=False, identifier=identifier)
        if self.cache is None or not hasattr(self.clipboard, 'set_files') or self.cleanup_busy.is_set():
            return self.acknowledge(version, ok=False, identifier=identifier)
        try:
            if incoming is None:
                if body.get('event') != 'begin':
                    return self.acknowledge(version, ok=False, identifier=identifier)
                incoming = {'receiver': Receiver(self.cache.parent, identifier), 'id': identifier, 'version': version,
                            'cancelled': False, 'ticket': None}
                with self.gate:
                    self.incoming = incoming
                    if self.version >= version:
                        incoming['cancelled'] = True
                if incoming['cancelled']:
                    self.drop_incoming()
                    return self.acknowledge(version, identifier=identifier)
            if incoming['ticket'] is not None:
                return packet(self.nonce, 'stored', s=version[0], o=version[1], i=identifier)
            roots = incoming['receiver'].accept(body)
            if roots is not None:
                incoming['ticket'] = self.cache.prepare(incoming['receiver'])
                return packet(self.nonce, 'stored', s=version[0], o=version[1], i=identifier)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            self.transfer_error = 'CLIPBOARD_TRANSFER_FAILED'
            self.drop_incoming()
            return self.acknowledge(version, ok=False, identifier=identifier)
        return None

    def apply_incoming(self):
        with self.gate:
            incoming = self.incoming
            expected = self.version
        if incoming is None:
            return None
        if incoming.get('cancelled') or incoming['version'] <= expected:
            self.drop_incoming()
            return self.acknowledge(incoming['version'], identifier=incoming['id'])
        ticket = incoming.get('ticket')
        if ticket is None:
            return None
        def current():
            with self.gate:
                return not self.stopping.is_set() and self.incoming is incoming and not incoming['cancelled'] and self.version == expected
        applied = self.clipboard.set_files(ticket.roots, current)
        if not applied:
            return None
        # Once paths have entered the native clipboard, a paste may already be reading them.
        ticket.park()
        with self.gate:
            if self.version == expected:
                self.version = incoming['version']
                self.current = {'paths': list(ticket.roots), 'id': incoming['id'], 'version': self.version, 'stored': True}
                self.pending = None
            if self.incoming is incoming:
                self.incoming = None
            self.confirmed(incoming['version'])
            if self.transfer_error == 'CLIPBOARD_TEXT_TOO_LARGE':
                self.transfer_error = None
            self.last_applied = (incoming['version'], 'files', incoming['id'])
        return self.acknowledge(incoming['version'], applied=True, identifier=incoming['id'])

    def run(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connection = connection
            try:
                buffered = self.authenticate(connection)
                if buffered is not None:
                    self.receive(packet(self.nonce, 'hello', o='guest', ready=True))
                    self.state, self.code = 'ready', 'CLIPBOARD_READY'
                    self.log('connected')
                    self.serve(connection, buffered)
            except OSError as error:
                self.log('connection lost: ' + type(error).__name__)
            finally:
                with self.gate:
                    partial = self.incoming is not None and self.incoming.get('ticket') is None
                if partial:
                    self.drop_incoming()
                connection.close()
                self.connection = None
                if self.state == 'ready':
                    self.state, self.code = 'waiting', 'CLIPBOARD_WAITING'

    def authenticate(self, connection):
        # QEMU opens guestfwd at boot, before Windows can send its first HELLO.
        deadline = time.monotonic() + HELLO_IDLE_TIMEOUT
        fragment = False
        buffered = b''
        while not self.stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('Clipboard handshake deadline exceeded')
            if b'\n' in buffered:
                line, buffered = buffered.split(b'\n', 1)
                if line in (b'', b'\r'):
                    continue
                if len(line) > HELLO_MAX_BYTES:
                    return None
                message = decode_packet(line, self.nonce)
                if not guest_hello(message):
                    return None
                if not message['ready']:
                    self.state, self.code = 'failed', 'GUEST_CLIPBOARD_UNAVAILABLE'
                    return None
                return buffered
            if len(buffered) > HELLO_MAX_BYTES:
                return None
            connection.settimeout(min(.2, remaining))
            try:
                chunk = connection.recv(HELLO_MAX_BYTES + 1 - len(buffered))
            except socket.timeout:
                continue
            if not chunk:
                return None
            if not fragment:
                deadline = time.monotonic() + HELLO_MESSAGE_TIMEOUT
                fragment = True
            buffered += chunk
        return None

    def public(self):
        restoring = bool(getattr(self.clipboard, 'recovery', []))
        error = 'CLIPBOARD_RESTORING' if restoring else getattr(self.clipboard, 'last_error', None) or self.transfer_error
        return {'status': 'unconfirmed' if error else self.state,
                'code': error or self.code,
                'last_transfer_utc': self.last_transfer_utc}

    def serve(self, connection, buffer=b''):
        connection.settimeout(5)
        connection.sendall(b'\n' + packet(self.nonce, 'hello', o='host', ready=True))
        while not self.stopping.is_set():
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                reply = self.receive(line)
                if reply is not None:
                    connection.sendall(reply)
            if len(buffer) > MAX_LINE:
                raise OSError('Clipboard frame exceeds its limit')
            reply = self.apply_incoming()
            if reply is not None:
                connection.sendall(reply)
            outgoing = self.next_outgoing()
            if outgoing is not None:
                connection.sendall(outgoing)
            try:
                if not select.select([connection], [], [], .05)[0]:
                    continue
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer += chunk


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    ole32 = ctypes.WinDLL('ole32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    WM_CLIPBOARDUPDATE, WM_APP, WM_CLOSE, WM_TIMER = 0x031D, 0x8000, 0x0010, 0x0113
    CF_UNICODETEXT, CF_HDROP, GMEM_MOVEABLE = 13, 15, 2
    CF_TEXT, CF_OEMTEXT, CF_LOCALE = 1, 7, 16

    class WNDCLASS(ctypes.Structure):
        _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                    ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HANDLE), ('hCursor', wintypes.HANDLE),
                    ('hbrBackground', wintypes.HANDLE), ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.GetClipboardOwner.restype = wintypes.HWND
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GetThreadLocale.restype = wintypes.DWORD
    kernel32.GetLocaleInfoW.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int]
    kernel32.GetLocaleInfoW.restype = ctypes.c_int
    kernel32.WideCharToMultiByte.argtypes = [wintypes.UINT, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int,
                                           wintypes.LPVOID, ctypes.c_int, wintypes.LPVOID, wintypes.LPVOID]
    kernel32.WideCharToMultiByte.restype = ctypes.c_int
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    ole32.OleDuplicateData.argtypes = [wintypes.HANDLE, wintypes.WORD, wintypes.UINT]
    ole32.OleDuplicateData.restype = wintypes.HANDLE
    ole32.OleInitialize.argtypes = [wintypes.LPVOID]
    ole32.OleInitialize.restype = ctypes.c_long
    ole32.OleUninitialize.argtypes = []
    for name in ('DeleteObject', 'DeleteMetaFile', 'DeleteEnhMetaFile'):
        getattr(gdi32, name).argtypes = [wintypes.HANDLE]
    gdi32.CopyEnhMetaFileW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    gdi32.CopyEnhMetaFileW.restype = wintypes.HANDLE
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID]
    gdi32.GetPaletteEntries.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.UINT, wintypes.LPVOID]
    gdi32.GetMetaFileBitsEx.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID]
    gdi32.GetEnhMetaFileBits.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID]

    class _Bitmap(ctypes.Structure):
        _fields_ = [('type', wintypes.LONG), ('width', wintypes.LONG), ('height', wintypes.LONG),
                    ('stride', wintypes.LONG), ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('pixels', wintypes.LPVOID)]

    class _MetafilePicture(ctypes.Structure):
        _fields_ = [('mode', wintypes.LONG), ('x', wintypes.LONG), ('y', wintypes.LONG), ('metafile', wintypes.HANDLE)]

    def _base_format(format):
        return {0x82: 2, 0x83: 3, 0x8E: 14}.get(format, format)

    def _metafile(handle):
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError('Clipboard metafile is unavailable')
        try:
            return _MetafilePicture.from_address(pointer).metafile
        finally:
            kernel32.GlobalUnlock(handle)

    def _free_clipboard_copy(format, handle):
        format = _base_format(format)
        if format in (2, 9):
            gdi32.DeleteObject(handle)
        elif format == 14:
            gdi32.DeleteEnhMetaFile(handle)
        else:
            if format == 3:
                try:
                    gdi32.DeleteMetaFile(_metafile(handle))
                except OSError:
                    pass
            kernel32.GlobalFree(handle)

    def _snapshot_clipboard():
        """Copy every materialized format while the caller holds OpenClipboard."""
        copies, seen, size, format = [], set(), 0, 0
        try:
            while True:
                ctypes.set_last_error(0)
                format = user32.EnumClipboardFormats(format)
                if not format:
                    if ctypes.get_last_error():
                        raise OSError('Clipboard enumeration failed')
                    return copies
                if format in seen or len(seen) == 256 or format == 0x80 or 0x200 <= format < 0x400:
                    raise OSError('Clipboard format cannot be preserved safely')
                seen.add(format)
                source, base = user32.GetClipboardData(format), _base_format(format)
                if not source:
                    raise OSError('Clipboard data could not be materialized')
                if base == 2:
                    bitmap = _Bitmap()
                    if not gdi32.GetObjectW(source, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
                        raise OSError('Clipboard bitmap is unavailable')
                    count = abs(bitmap.height) * bitmap.stride * bitmap.planes
                elif base == 9:
                    count = gdi32.GetPaletteEntries(source, 0, 0, None) * 4
                elif base == 14:
                    count = gdi32.GetEnhMetaFileBits(source, 0, None)
                elif base == 3:
                    if kernel32.GlobalSize(source) < ctypes.sizeof(_MetafilePicture):
                        raise OSError('Invalid clipboard metafile')
                    count = gdi32.GetMetaFileBitsEx(_metafile(source), 0, None)
                else:
                    count = kernel32.GlobalSize(source)
                size += count
                if not 0 < count or size > 64 * 1024 * 1024:
                    raise OSError('Clipboard backup exceeds its limit')
                copy = gdi32.CopyEnhMetaFileW(source, None) if base == 14 else ole32.OleDuplicateData(source, base, GMEM_MOVEABLE)
                if not copy:
                    raise OSError('Clipboard data could not be preserved')
                copies.append((format, copy))
        except BaseException:
            for format, copy in copies:
                _free_clipboard_copy(format, copy)
            raise

    def _text_payloads(text):
        """Prepare all text formats before touching the clipboard; Close must not synthesize more."""
        if type(text) is not str or text_limit_error(text):
            raise ValueError('Invalid native clipboard text')
        raw = (text + '\0').encode('utf-16-le')
        source = ctypes.create_string_buffer(raw)
        pointer = ctypes.cast(source, wintypes.LPCWSTR)
        units = len(raw) // 2  # Python len counts supplementary characters differently from Win32 WCHAR units.
        locale = kernel32.GetThreadLocale()
        if not locale:
            raise OSError('Clipboard locale is unavailable')
        converted = []
        for kind in (0x1004, 0xB):  # ANSI/OEM code pages for precisely the LCID supplied in CF_LOCALE.
            codepage = wintypes.DWORD()
            if kernel32.GetLocaleInfoW(locale, kind | 0x20000000, ctypes.cast(ctypes.byref(codepage), wintypes.LPWSTR), 2) != 2:
                raise OSError('Clipboard locale code page is unavailable')
            # Zero is valid CP_ACP for Unicode-only locales; let the platform perform/reject the conversion.
            count = kernel32.WideCharToMultiByte(codepage.value, 0, pointer, units, None, 0, None, None)
            if not 0 < count <= MAX_CHARS * 4 + 1:
                raise OSError('Clipboard text conversion failed')
            output = ctypes.create_string_buffer(count)
            if kernel32.WideCharToMultiByte(codepage.value, 0, pointer, units, output, count, None, None) != count:
                raise OSError('Clipboard text conversion failed')
            converted.append(output.raw)
        return [(CF_UNICODETEXT, raw), (CF_TEXT, converted[0]), (CF_OEMTEXT, converted[1]),
                (CF_LOCALE, struct.pack('<I', locale))]

    class WindowsClipboard:
        """Text clipboard of this PC. Owns a hidden message window on its own thread, which must keep pumping
        messages: another program's EmptyClipboard waits for the owner window to answer."""

        def __init__(self):
            self.hwnd, self.on_change, self.pending = None, None, None
            self.observed = None
            self.recovery = []
            self.reset_pending = False
            self.close_requested = self.writing = False
            self.last_error = None
            self.ready, self.thread = threading.Event(), None
            self.exclude = user32.RegisterClipboardFormatW(EXCLUDE_FORMAT)
            if not self.exclude:
                raise OSError('Cannot register clipboard privacy format')
            self.proc = WNDPROC(self.window_proc)

        def start(self, on_change):
            self.on_change = on_change
            self.thread = threading.Thread(target=self.pump, name='clipboard-window', daemon=True)
            self.thread.start()
            if not self.ready.wait(5) or not self.hwnd:
                raise OSError('Clipboard window was not created')

        def stop(self):
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if self.thread is not None:
                while self.thread.is_alive():
                    self.thread.join(timeout=3)
                    if not self.writing and not self.recovery and not self.reset_pending:
                        break

        def set_text(self, text, guard):
            return self._submit({'text': text}, guard)

        def set_files(self, paths, guard):
            return self._submit({'paths': paths}, guard)

        def _submit(self, value, guard):
            request = dict(value, guard=guard, done=threading.Event(), started=threading.Event(), cancelled=threading.Event(), ok=False)
            self.pending = request
            if not user32.PostMessageW(self.hwnd, WM_APP, 0, 0):
                request['cancelled'].set()
                return False
            if not request['done'].wait(2):
                request['cancelled'].set()
                self.last_error = 'CLIPBOARD_BUSY'
                # SetClipboardData may already have published paths: only its owner knows whether they may be removed.
                if not request['started'].is_set():
                    return False
                request['done'].wait()
            return request['ok']

        def pump(self):
            if ole32.OleInitialize(None) < 0:
                self.ready.set()
                return
            try:
                self._pump()
            finally:
                ole32.OleUninitialize()

        def _pump(self):
            window_class = WNDCLASS()
            window_class.lpfnWndProc = self.proc
            window_class.hInstance = kernel32.GetModuleHandleW(None)
            window_class.lpszClassName = 'WindowsClaudeClipboard'
            user32.RegisterClassW(ctypes.byref(window_class))
            self.hwnd = user32.CreateWindowExW(0, window_class.lpszClassName, None, 0, 0, 0, 0, 0, wintypes.HWND(-3), None,
                                               window_class.hInstance, None)
            if self.hwnd:
                if not user32.AddClipboardFormatListener(self.hwnd) or not user32.SetTimer(self.hwnd, 1, 100, None):
                    user32.RemoveClipboardFormatListener(self.hwnd)
                    user32.DestroyWindow(self.hwnd)
                    self.hwnd = None
                else:
                    self.poll()
            self.ready.set()
            message = wintypes.MSG()
            while self.hwnd and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.DispatchMessageW(ctypes.byref(message))

        def window_proc(self, hwnd, message, wparam, lparam):
            try:
                if message in (WM_CLIPBOARDUPDATE, WM_TIMER):
                    self.poll()
                    return 0
                if message == WM_APP:
                    request, self.pending = self.pending, None
                    if request is not None:
                        request['started'].set()
                        try:
                            self.poll()
                            guard = lambda: not request['cancelled'].is_set() and request['guard']()
                            request['ok'] = self.write_files(request['paths'], guard) if 'paths' in request else self.write(request['text'], guard)
                        finally:
                            request['done'].set()
                    return 0
                if message == WM_CLOSE:
                    self.close_requested = True
                    if self.writing or not self.restore_pending():
                        return 0
                    user32.KillTimer(hwnd, 1)
                    user32.RemoveClipboardFormatListener(hwnd)
                    self.hwnd = None
                    user32.DestroyWindow(hwnd)
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:  # a failed copy must never take the window thread down
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        def open(self):
            for _ in range(20):
                if user32.OpenClipboard(self.hwnd):
                    return True
                time.sleep(0.025)
            return False

        def poll(self):
            if not self.restore_pending():
                return
            if self.close_requested:
                if not self.writing:
                    user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
                return
            if self.observed == user32.GetClipboardSequenceNumber():
                return
            self.last_error = None
            snapshot = self.read()
            if snapshot is None and self.last_error is None:
                self.last_error = 'CLIPBOARD_BUSY'
            if snapshot is not None:
                sequence, text = snapshot
                if sequence != self.observed:
                    self.observed = sequence
                    if self.on_change:
                        self.on_change(text)

        def _restore_copies(self, copies):
            pending = []
            for format, copy in copies:
                if not user32.SetClipboardData(format, copy):
                    pending.append((format, copy))
            self.observed = user32.GetClipboardSequenceNumber()
            return pending

        def restore_pending(self):
            if not self.recovery and not self.reset_pending:
                return True
            self.last_error = 'CLIPBOARD_RESTORING'
            if not self.open():
                return False
            try:
                if user32.GetClipboardSequenceNumber() != self.observed:
                    owner = user32.GetClipboardOwner()
                    # Owner is NOT a self-echo identity: compatible writers can Set without Empty.
                    # A changed sequence with our/unknown owner may be such a writer or Close synthesis.
                    # Preserve the backup and do not overwrite either case. Only a confirmed new owner
                    # makes the old backup obsolete; the next normal capture will observe its copy.
                    if not owner or owner == self.hwnd:
                        self.last_error = 'CLIPBOARD_RESTORING'
                        return False
                    for format, copy in self.recovery:
                        _free_clipboard_copy(format, copy)
                    self.recovery, self.reset_pending = [], False
                else:
                    if self.reset_pending:
                        if not user32.EmptyClipboard():
                            self.last_error = 'CLIPBOARD_RESTORING'
                            return False
                        self.reset_pending = False
                        self.observed = user32.GetClipboardSequenceNumber()
                    self.recovery = self._restore_copies(self.recovery)
                if not self.recovery:
                    self.last_error = None
                return not self.recovery
            finally:
                user32.CloseClipboard()

        def read(self):
            if not self.open():
                return None
            try:
                sequence = user32.GetClipboardSequenceNumber()
                if user32.IsClipboardFormatAvailable(self.exclude):
                    return sequence, PRIVATE_COPY
                if user32.IsClipboardFormatAvailable(CF_HDROP):
                    handle = user32.GetClipboardData(CF_HDROP)
                    size = kernel32.GlobalSize(handle) if handle else 0
                    if not 20 <= size <= MAX_DROP_BYTES:
                        self.last_error = 'CLIPBOARD_FILE_SELECTION_INVALID'
                        return sequence, PRIVATE_COPY
                    pointer = kernel32.GlobalLock(handle)
                    if not pointer:
                        return None
                    try:
                        try:
                            return user32.GetClipboardSequenceNumber(), drop_paths(ctypes.string_at(pointer, size))
                        except (ValueError, UnicodeError):
                            self.last_error = 'CLIPBOARD_FILE_SELECTION_INVALID'
                            return sequence, PRIVATE_COPY
                    finally:
                        kernel32.GlobalUnlock(handle)
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return sequence, None
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                size = kernel32.GlobalSize(handle) if handle else 0
                if size > (MAX_CHARS + 1) * 2:
                    self.last_error = 'CLIPBOARD_TEXT_TOO_LARGE'
                    return sequence, PRIVATE_COPY
                if size < 2 or size % 2:
                    return sequence, None
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    return None
                try:
                    raw = ctypes.string_at(pointer, size)
                    end = next((index for index in range(0, size, 2) if raw[index:index + 2] == b'\0\0'), None)
                    try:
                        return user32.GetClipboardSequenceNumber(), raw[:end].decode('utf-16-le') if end is not None else None
                    except UnicodeError:
                        return sequence, None
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()

        def write(self, text, guard=lambda: True):
            return self._replace(lambda: _text_payloads(text), guard)

        def write_files(self, paths, guard=lambda: True):
            return self._replace(lambda: [(CF_HDROP, drop_data(paths))], guard)

        def _replace(self, payloads, guard):
            if self.close_requested:
                return False
            self.writing = True
            try:
                applied = self._write(payloads, guard)
                if applied:
                    self.last_error = None
                elif guard():
                    self.last_error = 'CLIPBOARD_RESTORING' if self.recovery or self.reset_pending else 'CLIPBOARD_WRITE_FAILED'
                return applied
            finally:
                self.writing = False

        def _write(self, payloads, guard):
            if not self.restore_pending():
                return False
            prepared, backup, opened = [], [], False
            try:
                try:
                    for format, data in payloads():
                        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                        if not handle:
                            return False
                        prepared.append((format, handle))
                        pointer = kernel32.GlobalLock(handle)
                        if not pointer:
                            return False
                        try:
                            ctypes.memmove(pointer, data, len(data))
                        finally:
                            kernel32.GlobalUnlock(handle)
                except (OSError, ValueError, UnicodeError):
                    return False
                if not self.open():
                    return False
                opened = True
                if self.observed != user32.GetClipboardSequenceNumber() or not guard():
                    return False
                try:
                    backup = _snapshot_clipboard()
                except OSError:
                    return False
                finally:
                    self.observed = user32.GetClipboardSequenceNumber()
                if not guard():
                    return False
                if not user32.EmptyClipboard():
                    return False
                for index, (format, handle) in enumerate(prepared):
                    if not user32.SetClipboardData(format, handle):
                        self.recovery, backup, self.reset_pending = backup, [], True
                        self.observed = user32.GetClipboardSequenceNumber()
                        # Never restore old formats on top of a half-published new set.
                        if user32.EmptyClipboard():
                            self.reset_pending = False
                            self.recovery = self._restore_copies(self.recovery)
                        return False
                    prepared[index] = (format, None)  # Windows owns only successfully published handles.
                self.observed = user32.GetClipboardSequenceNumber()  # All four formats, still under original lock.
                return True
            finally:
                for format, copy in backup:
                    _free_clipboard_copy(format, copy)
                for format, handle in prepared:
                    if handle:
                        kernel32.GlobalFree(handle)
                if opened:
                    user32.CloseClipboard()


def network_forward(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid clipboard listener port')
    # The outer -netdev parser consumes one comma before the legacy TCP chardev parses its options.
    return 'guestfwd=tcp:192.168.234.4:' + str(GUEST_PORT) + '-tcp:127.0.0.1:' + str(port) + ',,reconnect-ms=1000'

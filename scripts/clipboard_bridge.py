"""Shared text clipboard between this PC and the Windows guest.

The guest helper talks over the guest's third serial port (COM3), which QEMU exposes as a loopback TCP socket that
only this controller connects to. One JSON line per copy: {"v":1,"n":session nonce,"t":"text","z":1,"d":base64(raw deflate of UTF-8)}.
Text only, capped at MAX_CHARS: the emulated UART moves about 11 KB/s, and a copy made by a password manager is never sent.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import socket
import threading
import time
import zlib

MAX_CHARS = 256 * 1024
MAX_LINE = 1024 * 1024
EXCLUDE_FORMAT = 'ExcludeClipboardContentFromMonitorProcessing'


def digest(text):
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).digest()


def encode(nonce, text):
    """One protocol line for a copied text, or None when it is empty or too long to send."""
    if not text or len(text) > MAX_CHARS:
        return None
    packer = zlib.compressobj(6, zlib.DEFLATED, -15)
    packed = packer.compress(text.encode('utf-8')) + packer.flush()
    return (json.dumps({'v': 1, 'n': nonce, 't': 'text', 'z': 1, 'd': base64.b64encode(packed).decode('ascii')},
                       separators=(',', ':')) + '\n').encode('ascii')


def decode(line, nonce):
    """The text of a valid line of this session, otherwise None. Anything unexpected is dropped, never raised."""
    try:
        if len(line) > MAX_LINE:
            return None
        message = json.loads(line.decode('utf-8'))
        if (type(message) is not dict or message.get('v') != 1 or message.get('t') != 'text' or type(message.get('n')) is not str
                or not hmac.compare_digest(message['n'].encode(), nonce.encode()) or message.get('z') not in (0, 1)
                or type(message.get('d')) is not str):
            return None
        raw = base64.b64decode(message['d'].encode('ascii'), validate=True)
        if message['z']:
            unpacker = zlib.decompressobj(-15)
            raw = unpacker.decompress(raw, MAX_CHARS * 4)
            if unpacker.unconsumed_tail:
                return None
        text = raw.decode('utf-8')
        return text if 0 < len(text) <= MAX_CHARS else None
    except (ValueError, zlib.error, UnicodeError, TypeError):
        return None


class Bridge:
    """Keeps both clipboards equal while the guest runs; reconnects after every guest restart."""

    def __init__(self, port, nonce, clipboard, log=None):
        self.port, self.nonce, self.clipboard, self.log = port, nonce, clipboard, log or (lambda message: None)
        self.stopping = threading.Event()
        self.outgoing = queue.Queue()
        self.applied = self.sent = None
        self.thread = None

    def start(self):
        self.clipboard.start(self.local_change)
        self.thread = threading.Thread(target=self.run, name='clipboard-bridge', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.clipboard.stop()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def local_change(self, text):
        # Our own write comes back as a change; the digest of the last applied text recognises it.
        value = digest(text)
        if value == self.applied or value == self.sent:
            return
        line = encode(self.nonce, text)
        if line is not None:
            self.sent = value
            self.outgoing.put(line)

    def run(self):
        while not self.stopping.is_set():
            try:
                connection = socket.create_connection(('127.0.0.1', self.port), timeout=1)
            except OSError:
                self.stopping.wait(1)
                continue
            self.log('connected')
            try:
                self.serve(connection)
            except OSError as error:
                self.log('connection lost: ' + type(error).__name__)
            finally:
                connection.close()
            self.stopping.wait(1)

    def serve(self, connection):
        connection.settimeout(0.3)
        buffer = b''
        while not self.stopping.is_set():
            try:
                while True:
                    connection.sendall(self.outgoing.get_nowait())
            except queue.Empty:
                pass
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                text = decode(line, self.nonce)
                if text is not None and digest(text) != self.applied:
                    self.applied, self.sent = digest(text), None
                    self.clipboard.set_text(text)
            if len(buffer) > MAX_LINE:
                buffer = b''


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    WM_CLIPBOARDUPDATE, WM_APP, WM_CLOSE = 0x031D, 0x8000, 0x0010
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 2

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

    class WindowsClipboard:
        """Text clipboard of this PC. Owns a hidden message window on its own thread, which must keep pumping
        messages: another program's EmptyClipboard waits for the owner window to answer."""

        def __init__(self):
            self.hwnd, self.on_change, self.pending = None, None, None
            self.ready, self.thread = threading.Event(), None
            self.exclude = user32.RegisterClipboardFormatW(EXCLUDE_FORMAT)
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
                self.thread.join(timeout=3)

        def set_text(self, text):
            self.pending = text
            user32.PostMessageW(self.hwnd, WM_APP, 0, 0)

        def pump(self):
            window_class = WNDCLASS()
            window_class.lpfnWndProc = self.proc
            window_class.hInstance = kernel32.GetModuleHandleW(None)
            window_class.lpszClassName = 'WindowsClaudeClipboard'
            user32.RegisterClassW(ctypes.byref(window_class))
            self.hwnd = user32.CreateWindowExW(0, window_class.lpszClassName, None, 0, 0, 0, 0, 0, wintypes.HWND(-3), None,
                                               window_class.hInstance, None)
            if self.hwnd:
                user32.AddClipboardFormatListener(self.hwnd)
            self.ready.set()
            message = wintypes.MSG()
            while self.hwnd and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.DispatchMessageW(ctypes.byref(message))

        def window_proc(self, hwnd, message, wparam, lparam):
            try:
                if message == WM_CLIPBOARDUPDATE:
                    text = self.read()
                    if text and self.on_change:
                        self.on_change(text)
                    return 0
                if message == WM_APP:
                    text, self.pending = self.pending, None
                    if text:
                        self.write(text)
                    return 0
                if message == WM_CLOSE:
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

        def read(self):
            if not self.open():
                return None
            try:
                if user32.IsClipboardFormatAvailable(self.exclude) or not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return None
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle or kernel32.GlobalSize(handle) > (MAX_CHARS + 1) * 2:
                    return None
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    return None
                try:
                    return ctypes.wstring_at(pointer)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()

        def write(self, text):
            data = (text + '\0').encode('utf-16-le')
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return
            pointer = kernel32.GlobalLock(handle)
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not self.open():
                kernel32.GlobalFree(handle)
                return
            try:
                user32.EmptyClipboard()
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    kernel32.GlobalFree(handle)
            finally:
                user32.CloseClipboard()


def reserve_port():
    """A free loopback port for the guest's serial socket; QEMU listens on it itself."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def arguments(port):
    # IRQ 5 is free: COM1 uses 4 and COM2 uses 3, and two UARTs on one edge-triggered line would conflict in Windows.
    return ['-chardev', 'socket,id=wcclip,host=127.0.0.1,port=' + str(port) + ',server=on,wait=off',
            '-device', 'isa-serial,index=2,iobase=0x3e8,irq=5,chardev=wcclip']

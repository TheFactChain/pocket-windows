"""Read-only FAT directory protected by the existing CurrentUser+SYSTEM ACL boundary."""
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
from pathlib import Path

from file_snapshot import locked_read
import session_carrier as private


class Medium:
    def __init__(self, config, executable, *, base_directory=None):
        if not isinstance(executable, bytes) or not executable.startswith(b'MZ') or len(executable) > 2 * 1024 ** 2:
            raise ValueError('A bounded compiled guest file helper is required')
        nonce = private._nonce(config['session_nonce'])
        self.base = private._base_path(base_directory if base_directory is not None else private._local_app_data() / 'WindowsClaudePortable/file-sessions')
        self.path = self.base / nonce
        self.holds = 0
        self.identities = {}
        self.files = {'HOSTFILS.JSN': json.dumps(config, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                      'HOSTFILS.EXE': executable,
                      'RUNFILES.CMD': b'@echo off\r\n"%~dp0HOSTFILS.EXE" --install\r\n'}
        if len(self.files['HOSTFILS.JSN']) > 65536:
            raise ValueError('Guest file-sharing config is too large')
        if len(self.files['HOSTFILS.JSN']) > 32768:
            # The installed 32KiB helper must parse a bootstrap before it can update itself.
            self.files['HOSTFIL2.JSN'] = self.files['HOSTFILS.JSN']
            self.files['HOSTFILS.JSN'] = json.dumps(dict(config, shares=[]), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(self.files['HOSTFILS.JSN']) > 32768:
                raise ValueError('Guest file-sharing bootstrap is too large')
        kernel, _ = private._api()
        with private._private_base(self.base, create=True):
            with private._descriptor(True) as attributes:
                if not kernel.CreateDirectoryW(str(self.path), ctypes.byref(attributes)):
                    raise private.CarrierError('File-sharing medium already exists')
            with private._open_private(self.path, directory=True, delete=True) as directory:
                self.directory_id = self.path.lstat().st_ino
                try:
                    for name, data in self.files.items():
                        with private._write_new(self.path / name, data):
                            self.identities[name] = private._identity(self.path / name)
                except BaseException:
                    for name in self.identities:
                        with private._open_private(self.path / name, delete=True) as handle:
                            private._delete_handle(handle)
                    if not any(self.path.iterdir()):
                        private._delete_handle(directory)
                    raise

    def _check_directory(self):
        if self.path.lstat().st_ino != self.directory_id or {item.name for item in self.path.iterdir()} != set(self.files):
            raise private.CarrierError('File-sharing medium directory changed')

    def _check_file(self, name, data):
        if private._identity(self.path / name) != self.identities[name] or hashlib.sha256(data).digest() != hashlib.sha256(self.files[name]).digest():
            raise private.CarrierError('File-sharing medium content changed')

    @contextmanager
    def hold(self):
        with ExitStack() as stack:
            stack.enter_context(private._private_base(self.base))
            stack.enter_context(private._open_private(self.path, directory=True))
            self._check_directory()
            for name, data in self.files.items():
                stack.enter_context(private._open_private(self.path / name))
                source = stack.enter_context(locked_read(self.path / name))
                self._check_file(name, source.read(len(data) + 1))
            block = {'driver': 'vvfat', 'dir': str(self.path), 'label': 'WCPFILES', 'fat-type': 16,
                     'rw': False, 'read-only': True, 'node-name': 'wcfilesmedia'}
            self.holds += 1
            try:
                yield ['-blockdev', json.dumps(block, separators=(',', ':')),
                       '-device', 'qemu-xhci,id=wcfilesusb',
                       '-device', 'usb-storage,drive=wcfilesmedia,bus=wcfilesusb.0,removable=on']
            finally:
                self.holds -= 1

    def cleanup(self, *, guest_stopped):
        if guest_stopped is not True or self.holds:
            raise private.CarrierError('File-sharing cleanup needs a stopped Windows guest')
        with private._private_base(self.base), private._open_private(self.path, directory=True, delete=True) as directory:
            self._check_directory()
            with ExitStack() as stack:
                handles = {}
                for name, data in self.files.items():
                    handle = stack.enter_context(private._open_private(self.path / name, delete=True))
                    self._check_file(name, private._read(handle, len(data)))
                    handles[name] = handle
                for handle in handles.values():
                    private._delete_handle(handle)
            private._delete_handle(directory)

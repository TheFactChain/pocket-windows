"""Pinned Windows reads and exact-identity deletion of caller-owned ephemeral files."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

QEMU_IMG_SHA256 = 'e28ccc80467af96433a044fcae7541d797904bc2e3afd3eb64aabe578896e73b'


def qemu_path(path):
    """The bundled QEMU uses UTF-8; no short-name alias or existing parent is needed."""
    return str(Path(path)).replace('\\', '/')


def remove_owned_file(path, identity):
    """Delete one exact Windows file; caller must pin its parent directory.

    identity is captured from stat at creation: {device: st_dev, file_id: st_ino}.
    Missing returns False. Identity/type/link mismatch raises ValueError; native
    access/sharing failures raise OSError. No path-based deletion fallback exists.
    This establishes file identity, not process exit or ownership authorization.
    """
    if (type(identity) is not dict or any(type(identity.get(key)) is not int for key in ('device', 'file_id'))
            or identity['device'] < 0 or identity['file_id'] <= 0):
        raise ValueError('A captured device and file ID are required')
    if os.name != 'nt':
        raise RuntimeError('Owned file removal requires Windows')

    class FileInformation(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
                    ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                    ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                    ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]

    class FileIdInformation(ctypes.Structure):
        _fields_ = [('volume', ctypes.c_ulonglong), ('file_id', ctypes.c_ubyte * 16)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    # DELETE | FILE_READ_ATTRIBUTES; share READ/WRITE, explicitly deny DELETE/rename.
    # OPEN_REPARSE_POINT prevents following a final link. BACKUP_SEMANTICS permits
    # inspecting and refusing a directory using the same handle instead of its path.
    handle = kernel.CreateFileW(str(path), 0x00010080, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):
            return False
        raise ctypes.WinError(error)
    try:
        info, file_id = FileInformation(), FileIdInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(file_id), ctypes.sizeof(file_id)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (kernel.GetFileType(handle) != 1 or info.attributes & (0x10 | 0x400) or info.links != 1
                or file_id.volume != identity['device']
                or int.from_bytes(bytes(file_id.file_id), 'little') != identity['file_id']):
            raise ValueError('Owned file identity, type or link count changed')
        disposition = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN.
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def locked_read(path):
    if os.name != 'nt':
        raise RuntimeError('The approved snapshot lock requires Windows')
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        yield stream

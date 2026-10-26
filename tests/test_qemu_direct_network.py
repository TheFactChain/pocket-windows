"""Verify the real QEMU socket binding helper against Windows Winsock."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get('WCP_QEMU_BUILD_ROOT', ROOT / 'state/qemu-window-build'))

class QemuDirectNetworkTests(unittest.TestCase):
    def test_socket_binding_is_per_socket_and_fail_closed(self):
        source = BUILD / 'qemu-11.1.0/net/slirp.c'
        compiler = BUILD / 'msys64/mingw64/bin/gcc.exe'
        if os.name != 'nt' or not source.exists() or not compiler.exists():
            self.skipTest('Native QEMU source and MinGW toolchain required')
        text = source.read_text(encoding='utf-8')
        match = re.search(r'#pragma push_macro\("getsockopt"\).*?#pragma pop_macro\("getsockopt"\)', text, re.S)
        self.assertIsNotNone(match, 'QEMU has no per-socket physical interface binding')
        program = r'''
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
typedef SOCKET slirp_os_socket;
#define getsockopt CRT_WRAPPER_MUST_NOT_BE_USED
#define setsockopt CRT_WRAPPER_MUST_NOT_BE_USED
HELPER
#undef getsockopt
#undef setsockopt
int main(void) {
    WSADATA data;
    DWORD index, actual;
    int length;
    assert(WSAStartup(MAKEWORD(2,2), &data) == 0);
    assert(GetBestInterface(inet_addr("127.0.0.1"), &index) == NO_ERROR);
    for (int type = SOCK_STREAM; type <= SOCK_DGRAM; type++) {
        SOCKET fd = socket(AF_INET, type, 0);
        assert(fd != INVALID_SOCKET);
        assert(net_slirp_bind_interface(fd, 0));
        assert(net_slirp_bind_interface(fd, index));
        length = sizeof(actual);
        assert(getsockopt(fd, IPPROTO_IP, IP_UNICAST_IF, (char *)&actual, &length) == 0);
        assert(actual == index);
        closesocket(fd);
    }
    assert(!net_slirp_bind_interface(INVALID_SOCKET, index));
    SOCKET ipv6 = socket(AF_INET6, SOCK_DGRAM, 0);
    assert(ipv6 != INVALID_SOCKET);
    assert(!net_slirp_bind_interface(ipv6, index));
    closesocket(ipv6);
    WSACleanup();
    return 0;
}
'''.replace('HELPER', match.group())
        env = os.environ.copy()
        env['PATH'] = str(compiler.parent) + os.pathsep + env.get('PATH', '')
        with tempfile.TemporaryDirectory(prefix='wcp-direct-', dir=BUILD) as folder:
            cfile, exe = Path(folder) / 'check.c', Path(folder) / 'check.exe'
            cfile.write_text(program, encoding='utf-8')
            subprocess.run([str(compiler), '-Wall', '-Werror', str(cfile), '-lws2_32', '-liphlpapi', '-o', str(exe)], env=env, check=True, capture_output=True)
            result = subprocess.run([str(exe)], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

if __name__ == '__main__':
    unittest.main()

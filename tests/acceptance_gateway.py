"""Native isolated gateway probe. No Windows disk or real connection is used."""
import argparse
import ipaddress
import json
from pathlib import Path
import select
import socket
import socketserver
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gateway_controller
import launch_runtime as runtime
from profile_store import ProfileStore, _pinned_directory


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class ConnectHandler(socketserver.StreamRequestHandler):
    def destination(self, authority):
        address, port = authority.decode('ascii').rsplit(':', 1)
        if not ipaddress.IPv4Address(address).is_global or port != '443':
            raise ValueError('Only public HTTPS fixture traffic is allowed')
        return address, 443

    def handle(self):
        self.connection.settimeout(20)
        header = self.rfile.readline(4097)
        parts = header.split()
        if len(header) > 4096 or len(parts) != 3 or parts[0] != b'CONNECT':
            return
        total = len(header)
        while True:
            line = self.rfile.readline(4097)
            total += len(line)
            if not line or total > 8192:
                return
            if line == b'\r\n':
                break
        self.server.attempts += 1
        if self.server.reject:
            self.wfile.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
            return
        try:
            destination = self.destination(parts[1])
            with socket.create_connection(destination, timeout=15) as outbound:
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self.wfile.flush()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not self.server.reject:
                    readable, _, _ = select.select([self.connection, outbound], [], [], 1)
                    for source in readable:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (outbound if source is self.connection else self.connection).sendall(chunk)
        except (OSError, ValueError):
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('reject', 'connect', 'reconnect'), required=True)
    args = parser.parse_args()
    operation = ROOT / 'logs' / ('gateway-acceptance-' + args.scenario + '-' + uuid.uuid4().hex)
    with runtime.ProjectLock(ROOT) as acquired, _pinned_directory(ROOT / 'logs'):
        if not acquired or runtime.engine_processes(ROOT):
            raise RuntimeError('All portable VMs must be stopped before this isolated probe')
        operation.mkdir()
        with _pinned_directory(operation, create=False), Proxy(('127.0.0.1', 0), ConnectHandler) as proxy:
            proxy.reject, proxy.attempts = args.scenario == 'reject', 0
            service = threading.Thread(target=proxy.serve_forever, daemon=True)
            service.start()
            store = ProfileStore(operation / 'fixture-profiles')
            profile = store.save({'name': 'Disposable acceptance proxy', 'protocol': 'http',
                                  'host': '10.0.2.2', 'port': proxy.server_address[1]})
            store.select(profile['profile_id'])
            before = set((ROOT / 'logs').glob('guest-gateway-boot-*'))
            started = runtime.utc_now()
            observed = {'outage_observed': False, 'recovered': False, 'windows_attached': False}
            finished = threading.Event()
            def exercise_outage():
                deadline, outage_at, path = time.monotonic() + 240, None, None
                try:
                    while not finished.wait(.2) and time.monotonic() < deadline:
                        created = set((ROOT / 'logs').glob('guest-gateway-boot-*')) - before
                        if len(created) != 1:
                            continue
                        path = created.pop() / 'state.json'
                        try:
                            state = runtime.read_json(path)
                        except FileNotFoundError:
                            continue
                        if (state.get('profile_id') != profile['profile_id']
                                or state.get('test_only') is not True):
                            raise ValueError('Unrelated probe state')
                        if state['status'] == 'running' and outage_at is None:
                            proxy.reject, outage_at = True, time.monotonic()
                        elif state['status'] == 'reconnecting':
                            observed['outage_observed'] = True
                        if outage_at is not None and time.monotonic() - outage_at >= 25:
                            proxy.reject = False
                            if observed['outage_observed'] and state['status'] == 'running':
                                observed['recovered'] = True
                                break
                        if state['status'] == 'exited':
                            return
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    observed['probe_error'] = type(error).__name__
                finally:
                    if path and path.exists() and not finished.is_set():
                        runtime.atomic_json(path.parent / 'control-runtime-probe.json', {'action': 'powerdown'})
            observer = None
            if args.scenario == 'reconnect':
                observer = threading.Thread(target=exercise_outage, daemon=True)
                observer.start()
            try:
                code = gateway_controller.run(ROOT, test_only=True, store=store,
                                              stop_after_ready=args.scenario != 'reconnect')
            finally:
                finished.set()
                if observer:
                    observer.join(timeout=5)
                proxy.shutdown()
                service.join(timeout=5)
            created = set((ROOT / 'logs').glob('guest-gateway-boot-*')) - before
            if len(created) != 1:
                raise RuntimeError('The probe has no unique gateway evidence')
            state_path = created.pop() / 'state.json'
            state = runtime.read_json(state_path)
            processes = runtime.engine_processes(ROOT)
            expected_connection = args.scenario != 'reject'
            result = {
                'schema': 1, 'scenario': args.scenario, 'started_utc': started,
                'ended_utc': runtime.utc_now(), 'controller_exit_code': code,
                'state': str(state_path), 'proxy_attempts': proxy.attempts,
                'connection_test_passed': state.get('connection_test_passed', False),
                'guest_shutdown': state.get('guest_shutdown', False),
                'carrier_removed': state.get('carrier_removed', False),
                'overlay_removed': state.get('overlay_removed', False),
                'windows_attached': False, 'user_profiles_read': False,
                'live_project_qemu': list(processes), 'lan_path_tested': False,
                **observed,
            }
            result['passed'] = (bool(result['connection_test_passed']) == expected_connection
                and result['guest_shutdown'] and result['carrier_removed'] and not processes
                and result['overlay_removed'] and proxy.attempts > 0 and ((code == 0) == expected_connection)
                and (args.scenario != 'reconnect' or observed['outage_observed'] and observed['recovered']))
            runtime.atomic_json(operation / 'result.json', result)
            print(json.dumps(result), flush=True)
            return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

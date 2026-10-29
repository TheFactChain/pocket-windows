import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/host_network.py'
sys.path.insert(0, str(SCRIPT.parent))
REMOTE = '192.168.50.30'
network = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('host_network', SCRIPT)
    network = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(network)


def row(**changes):
    return dict(interface_index=4, local_ipv4='192.168.0.2', gateway_ipv4='192.168.0.1',
                prefix_length=24, route_metric=0, interface_metric=25,
                physical=True, up=True, destination_prefix='0.0.0.0/0') | changes


class HostNetworkTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(network, 'Physical network selection is missing')

    def test_virtual_and_down_adapters_cannot_win_over_physical(self):
        selected = network.choose_physical_ipv4([
            row(interface_index=23, physical=False, interface_metric=0),
            row(interface_index=7, up=False, interface_metric=0), row()], REMOTE)
        self.assertEqual(selected, {'interface_index': 4, 'local_ipv4': '192.168.0.2',
                                    'gateway_ipv4': '192.168.0.1'})

    def test_route_and_interface_metrics_rank_together_then_index_and_address(self):
        selected = network.choose_physical_ipv4([
            row(interface_index=5, route_metric=2, interface_metric=2),
            row(interface_index=4, route_metric=3, interface_metric=1),
            row(interface_index=3, route_metric=0, interface_metric=5)], REMOTE)
        self.assertEqual(selected['interface_index'], 4)
        selected = network.choose_physical_ipv4([row(local_ipv4='192.168.0.3'), row()], REMOTE)
        self.assertEqual(selected['local_ipv4'], '192.168.0.2')

    def test_invalid_addresses_are_ignored(self):
        invalid = ['127.0.0.1', '169.254.1.1', '0.0.0.0', '224.0.0.1', '255.255.255.255', '::1',
                   None, True, 123, {}, 'malformed']
        for address in invalid:
            with self.subTest(address=address):
                self.assertEqual(network.choose_physical_ipv4([row(local_ipv4=address), row()], REMOTE)['interface_index'], 4)
                with self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
                    network.choose_physical_ipv4([row(gateway_ipv4=address)], REMOTE)
        for changes in ({'gateway_ipv4': '192.168.0.2'},):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                network.choose_physical_ipv4([row(**changes)], REMOTE)

    def test_windows_default_route_can_use_an_offlink_gateway(self):
        selected = network.choose_physical_ipv4([row(prefix_length=32, gateway_ipv4='10.0.0.1')], REMOTE)
        self.assertEqual(selected['interface_index'], 4)

    def test_malformed_records_and_boolean_numbers_are_not_accepted(self):
        invalid = [None, {}, 'text', row(interface_index=True), row(interface_index=0),
                   row(interface_index=2**24), row(route_metric=-1), row(interface_metric=True),
                   row(prefix_length=0), row(prefix_length=33), row(physical=1), row(up=1)]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
                network.choose_physical_ipv4([candidate], REMOTE)
        for value in (None, {}, 'text', []):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                network.choose_physical_ipv4(value, REMOTE)

    def test_powershell_is_absolute_bounded_and_noninteractive(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([row()]), '')
        with mock.patch.dict(network.os.environ, {'SystemRoot': r'C:\Windows'}), \
             mock.patch.object(network.subprocess, 'run', return_value=result) as run:
            self.assertEqual(network.select_physical_ipv4(REMOTE)['interface_index'], 4)
        args, kwargs = run.call_args
        self.assertEqual(str(args[0][0]), r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
        self.assertIn('-NoProfile', args[0])
        self.assertIn('-NonInteractive', args[0])
        self.assertIn('Get-NetAdapter -Physical', args[0][-1])
        self.assertIn('Find-NetRoute -InterfaceIndex $index -RemoteIPAddress $endpoint', args[0][-1])
        self.assertIn("$endpoint = '192.168.50.30'", args[0][-1])
        self.assertNotIn('__ENDPOINT__', args[0][-1])
        self.assertTrue(kwargs['check'])
        self.assertEqual(kwargs['encoding'], 'utf-8')
        self.assertLessEqual(kwargs['timeout'], 15)

    def test_point_to_point_subnet_and_relative_systemroot(self):
        selected = network.choose_physical_ipv4([
            row(local_ipv4='192.168.0.2', gateway_ipv4='192.168.0.3', prefix_length=31)], REMOTE)
        self.assertEqual(selected['gateway_ipv4'], '192.168.0.3')
        with mock.patch.dict(network.os.environ, {'SystemRoot': 'relative'}), \
             mock.patch.object(network.subprocess, 'run') as run, \
             self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
            network.select_physical_ipv4(REMOTE)
        run.assert_not_called()

    def test_external_errors_are_replaced_with_safe_code(self):
        errors = [subprocess.TimeoutExpired('sensitive command', 10), OSError('sensitive OS text'),
                  subprocess.CalledProcessError(1, 'sensitive command', stderr='private data')]
        for error in errors:
            with self.subTest(error=error), mock.patch.object(network.subprocess, 'run', side_effect=error), \
                 self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
                network.select_physical_ipv4(REMOTE)
        for output in ('private text', '{}', '[true]', '[' + ' ' * 65536 + ']', '[' * 10000 + '0' + ']' * 10000):
            result = subprocess.CompletedProcess([], 0, output, '')
            with self.subTest(output=output[:20]), mock.patch.object(network.subprocess, 'run', return_value=result), \
                 self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
                network.select_physical_ipv4(REMOTE)

    def test_specific_onlink_route_beats_a_lower_metric_default_route(self):
        selected = network.choose_physical_ipv4([
            row(interface_index=4, route_metric=0, interface_metric=1),
            row(interface_index=9, local_ipv4='192.168.50.2', gateway_ipv4='0.0.0.0',
                destination_prefix='192.168.50.0/24', route_metric=999, interface_metric=999)], REMOTE)
        self.assertEqual(selected, {'interface_index': 9, 'local_ipv4': '192.168.50.2', 'gateway_ipv4': '0.0.0.0'})

    def test_more_specific_virtual_and_down_routes_cannot_win(self):
        selected = network.choose_physical_ipv4([
            row(interface_index=30, physical=False, destination_prefix='192.168.50.30/32', interface_metric=0),
            row(interface_index=31, up=False, destination_prefix='192.168.50.30/32', interface_metric=0),
            row(interface_index=9, local_ipv4='192.168.50.2', gateway_ipv4='0.0.0.0', destination_prefix='192.168.50.0/24')], REMOTE)
        self.assertEqual(selected['interface_index'], 9)

    def test_equal_matching_prefixes_use_combined_metrics_before_interface_index(self):
        selected = network.choose_physical_ipv4([
            row(interface_index=4, route_metric=0, interface_metric=0),
            row(interface_index=9, local_ipv4='192.168.50.2', gateway_ipv4='0.0.0.0',
                destination_prefix='192.168.50.0/24', route_metric=3, interface_metric=7),
            row(interface_index=10, local_ipv4='192.168.50.3', gateway_ipv4='0.0.0.0',
                destination_prefix='192.168.50.0/24', route_metric=4, interface_metric=5)], REMOTE)
        self.assertEqual(selected['interface_index'], 10)

    def test_mismatched_and_invalid_prefixes_offer_no_route(self):
        for prefix in ('192.168.51.0/24', '192.168.50.1/24', '192.168.50.0/33',
                       '::/0', '192.168.50.0/255.255.255.0', None, True, '', '192.168.50.30'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
                network.choose_physical_ipv4([row(destination_prefix=prefix)], REMOTE)

    def test_invalid_endpoint_never_reaches_powershell(self):
        for endpoint in ("192.168.1.1'; Remove-Item C:\\*; '", 'proxy.example', '::1', '0.0.0.0', '169.254.1.2', 123, None):
            with self.subTest(endpoint=endpoint), mock.patch.object(network.subprocess, 'run') as run, \
                    self.assertRaisesRegex(RuntimeError, '^DIRECT_NETWORK_UNAVAILABLE$'):
                network.select_physical_ipv4(endpoint)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()


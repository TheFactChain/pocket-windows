import importlib.util
from contextlib import ExitStack
import io
import json
import sys
import tarfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('gateway_session', ROOT / 'gateway/runtime/session.py')
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


class SessionTests(unittest.TestCase):
    def exercise_service(self, probe, event_hook=None, probe_seconds=0, child_exit=None, trace=None, rule_hook=None):
        value = {'schema':1,'nonce':'a'*32,'revision':'b'*32,'gateway_version':'fixture1',
                 'endpoint_ipv4':'203.0.113.7','profile':{'protocol':'socks5','host':'proxy.example','port':1080}}
        events, children, handlers, clock, policies = [], [], {}, [0], []
        probe_result = mock.Mock(side_effect=probe)
        def bounded_probe(deadline):
            clock[0] += probe_seconds
            return probe_result()
        def spawn(*args, **kwargs):
            child=mock.Mock()
            child.poll.return_value=child_exit if not children else None
            if trace is not None:
                trace.append(("spawn", args[0]))
            children.append(child)
            return child
        def emit(event, *args):
            events.append((event,*args))
            if event_hook:
                event_hook(event,children,handlers)
        def apply_rules(policy):
            policies.append(policy)
            if trace is not None:
                trace.append(('firewall', policy))
            if rule_hook:
                rule_hook(policy, clock, handlers)
        with ExitStack() as stack:
            for target, replacement in [('RUN',mock.MagicMock()),('VERSION',mock.MagicMock()),
                    ('wait_nics',mock.Mock()),('wait_lan_ipv6',mock.Mock(),),
                    ('setup_forwarding',mock.Mock(side_effect=lambda: trace.append(('forwarding',)) if trace is not None else None)),
                    ('command',mock.Mock(side_effect=lambda *args: trace.append(('command', *args)) if trace is not None else None)),
                    ('read_session',mock.Mock(return_value=value)),
                    ('apply_rules',mock.Mock(side_effect=apply_rules)),
                    ('emit',mock.Mock(side_effect=emit)),('health',mock.Mock(side_effect=bounded_probe)),
                    ('stop_children', mock.Mock(wraps=getattr(session,'stop_children',lambda children:None)))]:
                if target == 'stop_children' and not hasattr(session,target):
                    continue
                stack.enter_context(mock.patch.object(session,target,replacement))
            session.VERSION.read_text.return_value=value['gateway_version']
            stack.enter_context(mock.patch.object(session.glob,'glob',return_value=['/dev/disk/by-id/usb-WCSESSION']))
            stack.enter_context(mock.patch('builtins.open',mock.mock_open(read_data=b'')))
            stack.enter_context(mock.patch.object(session.os,'umask'))
            stack.enter_context(mock.patch.object(session.signal,'signal',side_effect=lambda signum,handler:handlers.update({signum:handler})))
            start=stack.enter_context(mock.patch.object(session.subprocess,'Popen',side_effect=spawn))
            stack.enter_context(mock.patch.object(session.time,'monotonic',side_effect=lambda:clock[0]))
            stack.enter_context(mock.patch.object(session.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)))
            result=session.main()
        return result, events, children, policies, start, value, clock[0]

    def test_install_keeps_ipv6_address_family_but_disables_ipv6_traffic(self):
        install = (ROOT / 'gateway/build/install.sh').read_text(encoding='utf-8')
        self.assertNotIn('ipv6.disable=1', install)
        self.assertIn('net.ipv6.conf.all.disable_ipv6=0', install)
        self.assertIn('net.ipv6.conf.default.disable_ipv6=0', install)
        self.assertIn('net.ipv6.conf.wan0.disable_ipv6=1', install)

    def test_default_route_and_pinned_endpoint_start_only_under_closed_policy(self):
        trace = []
        def event(event, children, handlers):
            if event == 'READY':
                handlers[session.signal.SIGTERM](None, None)
        result, _, _, _, _, value, _ = self.exercise_service(lambda: None, event, trace=trace)
        self.assertEqual(result, 0)
        offline = session.firewall(value, connected=False)
        closed_at = trace.index(('firewall', session.closed_firewall()))
        offline_at = trace.index(('firewall', offline))
        forwarding_at = trace.index(('forwarding',))
        nic_at = trace.index(('command', '/usr/sbin/ip', 'link', 'set', 'lo', 'up'))
        pinned_at = trace.index(('command', '/usr/sbin/ip', 'route', 'replace',
                                 value['endpoint_ipv4'] + '/32', 'via', '10.0.2.2', 'dev', 'wan0'))
        default_at = trace.index(('command', '/usr/sbin/ip', 'route', 'replace',
                                  'default', 'via', '10.0.2.2', 'dev', 'wan0'))
        spawn_at = next(i for i, item in enumerate(trace) if item[0] == 'spawn')
        self.assertLess(closed_at, offline_at)
        self.assertLess(offline_at, forwarding_at)
        self.assertLess(forwarding_at, nic_at)
        self.assertLess(nic_at, pinned_at)
        self.assertLess(pinned_at, default_at)
        self.assertLess(default_at, spawn_at)
        # A kernel route grants no extra WAN access: only the pinned proxy IPv4+port.
        for connected in (False, True):
            policy = session.firewall(value, connected=connected)
            wan_output = [line.strip() for line in policy.splitlines()
                          if 'oifname "wan0"' in line]
            self.assertEqual(wan_output, [
                'oifname "wan0" ip daddr 203.0.113.7 tcp dport 1080 accept'])
            self.assertEqual(policy.count('policy drop;'), 3)
            self.assertTrue(all('ip6' not in line for line in wan_output))
        self.assertNotIn('oifname "wc-tun0"', offline)
        self.assertNotIn('udp dport 53 accept', offline)
        self.assertNotIn('tcp dport 53 accept', offline)

    def test_initial_child_exit_is_service_failure_and_never_opens_lan(self):
        result, events, _, policies, _, value, _ = self.exercise_service(
            lambda: self.fail('No health probe after known service exit'), child_exit=1)
        self.assertEqual(result, 1)
        self.assertIn(('ERROR', value, 'PROXY_SERVICE_FAILED'), events)
        self.assertNotIn('READY', [row[0] for row in events])
        self.assertNotIn(session.firewall(value), policies)
        self.assertEqual(policies[-1], session.closed_firewall())

    def test_ready_timeout_retry_ready_does_not_stop_children_or_emit_error(self):
        ready=[]
        def event(event,children,handlers):
            if event == 'READY':
                ready.append(True)
                if len(ready)==2:
                    self.assertTrue(all(not child.terminate.called for child in children))
                    handlers[session.signal.SIGTERM](None,None)
        result,events,children,policies,start,value,_=self.exercise_service([None,TimeoutError(),None],event)
        self.assertEqual(result,0)
        self.assertEqual([row[0] for row in events],['FIREWALL_READY','READY','RECONNECTING','READY','STOPPED'])
        self.assertEqual(start.call_count,2)
        self.assertIn(session.firewall(value,connected=False),policies)

    def test_sustained_outage_remains_closed_and_uses_bounded_retry(self):
        probes=[0]
        def health():
            probes[0]+=1
            if probes[0]>1:
                raise TimeoutError()
        def event(event,children,handlers):
            if event=='RECONNECTING' and probes[0]>=7:
                handlers[session.signal.SIGTERM](None,None)
        result,events,_,policies,start,value,elapsed=self.exercise_service(health,event)
        self.assertEqual(result,0)
        self.assertFalse(any(row[0]=='ERROR' for row in events))
        self.assertEqual(start.call_count,2)
        self.assertGreaterEqual(elapsed,10+2+4+8+15+15)
        self.assertLessEqual(elapsed,100)
        offline=session.firewall(value,connected=False)
        self.assertTrue(all(policy in (offline,session.closed_firewall()) for policy in policies[policies.index(offline,2):]))

    def test_dead_child_restarts_same_immutable_config_then_recovers(self):
        ready=[]
        def event(event,children,handlers):
            if event=='READY':
                ready.append(True)
                if len(ready)==1:
                    children[0].poll.return_value=1
                else:
                    handlers[session.signal.SIGTERM](None,None)
        result,events,children,_,start,_,_=self.exercise_service(lambda:None,event)
        self.assertEqual(result,0)
        self.assertEqual(start.call_count,4)
        self.assertEqual(start.call_args_list[0].args,start.call_args_list[2].args)
        self.assertEqual(start.call_args_list[1].args,start.call_args_list[3].args)
        self.assertFalse(any(row[0]=='ERROR' for row in events))
        self.assertTrue(children[1].terminate.called)

    def test_shutdown_during_outage_does_not_emit_transient_error(self):
        def event(event,children,handlers):
            if event=='RECONNECTING':
                handlers[session.signal.SIGTERM](None,None)
        result,events,_,_,_,_,_=self.exercise_service([None,TimeoutError()],event)
        self.assertEqual(result,0)
        self.assertEqual(events[-1][0],'STOPPED')
        self.assertFalse(any(row[0]=='ERROR' for row in events))

    def test_initial_connection_deadline_still_emits_error(self):
        result,events,_,_,_,_,elapsed=self.exercise_service(lambda:(_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(result,1)
        self.assertGreaterEqual(elapsed,75)
        self.assertIn('ERROR',[row[0] for row in events])
        self.assertNotIn('READY',[row[0] for row in events])
        self.assertNotIn('RECONNECTING',[row[0] for row in events])

    def test_slow_success_after_initial_deadline_never_opens_lan_or_emits_ready(self):
        def event(event, children, handlers):
            if event == 'READY':
                handlers[session.signal.SIGTERM](None, None)
        result, events, _, policies, _, value, _ = self.exercise_service(lambda: None, event, probe_seconds=76)
        self.assertEqual(result, 1)
        self.assertNotIn('READY', [row[0] for row in events])
        self.assertNotIn(session.firewall(value), policies)

    def test_trickled_connect_response_cannot_extend_total_deadline(self):
        clock = [0]
        raw = mock.MagicMock()
        raw.__enter__.return_value = raw
        def trickle(length):
            clock[0] += 1
            return b'H'
        raw.recv.side_effect = trickle
        with mock.patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(session.socket, 'create_connection', return_value=raw), \
             mock.patch.object(session.ssl, 'create_default_context') as tls:
            with self.assertRaises(TimeoutError):
                session.proxy_https('1.1.1.1', 'cloudflare-dns.com', '/', deadline=3)
        self.assertLessEqual(raw.recv.call_count, 3)
        tls.assert_not_called()

    def test_deadline_after_opening_firewall_closes_lan_and_retries(self):
        for reconnect in (False, True):
            openings = [0]
            events_seen = []
            def rule(policy, clock, handlers):
                if 'oifname "wc-tun0"' in policy:
                    openings[0] += 1
                    if openings[0] == (2 if reconnect else 1):
                        clock[0] += 0.3
            def event(name, children, handlers):
                events_seen.append(name)
                if name == 'READY' and openings[0] >= (3 if reconnect else 2):
                    handlers[session.signal.SIGTERM](None, None)
            probe = [None, TimeoutError(), None, None] if reconnect else [None, None]
            with self.subTest(reconnect=reconnect):
                result, events, _, policies, _, value, _ = self.exercise_service(
                    probe, event, probe_seconds=24.8, rule_hook=rule)
                self.assertEqual(result, 0)
                self.assertNotIn('ERROR', [row[0] for row in events])
                self.assertEqual(events_seen.count('READY'), 2 if reconnect else 1)
                opening = session.firewall(value)
                expired = [i for i, policy in enumerate(policies) if policy == opening][1 if reconnect else 0]
                self.assertEqual(policies[expired + 1], session.firewall(value, connected=False))

    def test_opening_firewall_failure_is_fatal_not_retried(self):
        def rule(policy, clock, handlers):
            if 'oifname "wc-tun0"' in policy:
                raise OSError('nft failed')
        result, events, _, policies, _, _, _ = self.exercise_service([None], rule_hook=rule)
        self.assertEqual(result, 1)
        self.assertEqual(events[-1][0], 'ERROR')
        self.assertEqual(events[-1][-1], 'NFT_RULES_FAILED')
        self.assertNotIn('RECONNECTING', [row[0] for row in events])
        self.assertEqual(policies[-1], session.closed_firewall())

    def test_shutdown_during_firewall_application_never_emits_ready_or_error(self):
        def rule(policy, clock, handlers):
            if 'oifname "wc-tun0"' in policy:
                handlers[session.signal.SIGTERM](None, None)
                clock[0] += 26
        result, events, _, policies, _, _, _ = self.exercise_service([None], rule_hook=rule)
        self.assertEqual(result, 0)
        self.assertEqual(events[-1][0], 'STOPPED')
        self.assertFalse(any(row[0] in ('READY', 'ERROR') for row in events))
        self.assertEqual(policies[-1], session.closed_firewall())

    def test_health_shares_one_deadline_across_dns_and_https(self):
        response = b'{"Status":0,"Answer":[{"type":1,"data":"93.184.215.14"}]}'
        with mock.patch.object(session.time, 'monotonic', return_value=10), \
             mock.patch.object(session, 'proxy_https', side_effect=[response, b'OK']) as probe:
            session.health(deadline=30)
        self.assertEqual([call.kwargs['deadline'] for call in probe.call_args_list], [30, 30])

    def test_trickled_https_body_cannot_extend_total_deadline(self):
        clock = [0]
        raw, secure = mock.MagicMock(), mock.MagicMock()
        raw.__enter__.return_value = raw
        raw.recv.side_effect = [bytes([value]) for value in b'HTTP/1.1 200 OK\r\n\r\n']
        secure.__enter__.return_value = secure
        def trickle(length):
            clock[0] += 1
            return b'x'
        secure.recv.side_effect = trickle
        with mock.patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(session.socket, 'create_connection', return_value=raw), \
             mock.patch.object(session.ssl, 'create_default_context') as tls:
            tls.return_value.wrap_socket.return_value = secure
            with self.assertRaises(TimeoutError):
                session.proxy_https('1.1.1.1', 'cloudflare-dns.com', '/', deadline=3)
        self.assertLessEqual(secure.recv.call_count, 3)
        self.assertEqual([call.args[0] for call in secure.settimeout.call_args_list], [3, 3, 2, 1])

    def test_tls_verification_failure_after_ready_is_fatal_and_closed(self):
        result,events,_,policies,_,_,_=self.exercise_service([None,session.ssl.SSLCertVerificationError('fixture untrusted certificate')])
        self.assertEqual(result,1)
        self.assertEqual(events[-1][0],'ERROR')
        self.assertEqual(policies[-1],session.closed_firewall())
        self.assertNotIn('RECONNECTING',[row[0] for row in events])

    def test_owned_child_stop_is_bounded_and_clears_only_its_list(self):
        child=mock.Mock()
        child.poll.return_value=None
        child.wait.side_effect=[session.subprocess.TimeoutExpired('fixture',5),0]
        children=[child]
        session.stop_children(children)
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list,[mock.call(timeout=5),mock.call(timeout=5)])
        self.assertEqual(children,[])

    def test_forwarding_enables_only_lan_and_future_tun_ipv6_before_nics(self):
        with mock.patch.object(session, 'command') as command:
            session.setup_forwarding()
        calls = [call.args for call in command.call_args_list]
        flat = [arg for call in calls for arg in call]
        for required in ('net.ipv4.ip_forward=1', 'net.ipv6.conf.all.disable_ipv6=0',
                         'net.ipv6.conf.default.disable_ipv6=0', 'net.ipv6.conf.wan0.disable_ipv6=1',
                         'net.ipv6.conf.all.accept_ra=0', 'net.ipv6.conf.default.accept_ra=0',
                         'net.ipv6.conf.wan0.accept_ra=0', 'net.ipv6.conf.lan0.accept_ra=0',
                         'net.ipv6.conf.all.accept_redirects=0', 'net.ipv6.conf.default.accept_redirects=0',
                         'net.ipv6.conf.all.forwarding=1'):
            self.assertIn(required, flat)
        with mock.patch.object(session, 'command', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                session.setup_forwarding()

    def test_lan_ipv6_dad_checks_exact_ula_and_link_local_and_is_bounded(self):
        ready = [{'ifname': 'lan0', 'addr_info': [
            {'family':'inet6','local':'fd77:233::1','prefixlen':64},
            {'family':'inet6','local':'fe80::1','prefixlen':64}]}]
        def response(value):
            return mock.Mock(stdout=json.dumps(value))
        tentative = json.loads(json.dumps(ready))
        tentative[0]['addr_info'][0]['tentative'] = True
        with mock.patch.object(session.subprocess, 'run', side_effect=[response(tentative),response(ready)]) as command, \
             mock.patch.object(session.time, 'sleep') as sleep:
            session.wait_lan_ipv6(lambda: False)
        self.assertEqual(command.call_count, 2)
        sleep.assert_called_once_with(1)
        for value in ([], [{'ifname':'lan0','addr_info':ready[0]['addr_info'][:1]}], tentative):
            with self.subTest(value=value), \
                 mock.patch.object(session.subprocess, 'run', return_value=response(value)), \
                 mock.patch.object(session.time, 'monotonic', side_effect=[0, 0, 16]), \
                 mock.patch.object(session.time, 'sleep'):
                with self.assertRaises(ValueError):
                    session.wait_lan_ipv6(lambda: False)
        failed = json.loads(json.dumps(ready))
        failed[0]['addr_info'][0]['dadfailed'] = True
        with mock.patch.object(session.subprocess, 'run', return_value=response(failed)), \
             mock.patch.object(session.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                session.wait_lan_ipv6(lambda: False)
            sleep.assert_not_called()

    def test_dnsmasq_advertises_lan_ula_dns_without_becoming_dns_forwarder(self):
        children = []
        with mock.patch('builtins.open', mock.mock_open()), \
             mock.patch.object(session.subprocess, 'Popen') as start:
            session.start_services(Path('fixture.json'), children)
        args = start.call_args_list[1].args[0]
        for option in ('--port=0', '--interface=lan0', '--enable-ra',
                       '--dhcp-range=fd77:233::,ra-stateless,64,1h',
                       '--dhcp-option=option6:dns-server,[fd77:233::1]',
                       '--ra-param=lan0,mtu:1280,30,90'):
            self.assertIn(option, args)

    def test_network_failures_remain_closed_and_never_emit_ready(self):
        value = {'schema': 1, 'nonce': 'a' * 32, 'revision': 'b' * 32,
                 'gateway_version': '2026.10.20.1', 'endpoint_ipv4': '203.0.113.1',
                 'profile': {'protocol': 'socks5', 'host': 'proxy.example', 'port': 1080}}
        for expected in ('NFT_RULES_FAILED', 'INTERFACE_SETUP_FAILED', 'ENDPOINT_ROUTE_FAILED', 'FORWARDING_SETUP_FAILED', 'IPV6_ADDRESS_FAILED'):
            with self.subTest(expected=expected), ExitStack() as stack:
                for target, replacement in (('RUN', mock.MagicMock()), ('VERSION', mock.MagicMock()),
                                             ('wait_nics', mock.Mock()), ('wait_lan_ipv6', mock.Mock(side_effect=ValueError('DAD failed') if expected == 'IPV6_ADDRESS_FAILED' else None)), ('read_session', mock.Mock(return_value=value))):
                    stack.enter_context(mock.patch.object(session, target, replacement))
                session.VERSION.read_text.return_value = value['gateway_version']
                stack.enter_context(mock.patch.object(session.glob, 'glob', return_value=['/dev/disk/by-id/usb-WCSESSION']))
                stack.enter_context(mock.patch('builtins.open', mock.mock_open(read_data=b'')))
                stack.enter_context(mock.patch.object(session.signal, 'signal'))
                emit = stack.enter_context(mock.patch.object(session, 'emit'))
                start = stack.enter_context(mock.patch.object(session.subprocess, 'Popen'))
                def rule_call(content):
                    if expected == 'NFT_RULES_FAILED' and content != session.closed_firewall():
                        raise OSError('rule failed')
                rules = stack.enter_context(mock.patch.object(session, 'apply_rules', side_effect=rule_call))
                def command_call(*args):
                    if expected == 'INTERFACE_SETUP_FAILED' and args[:3] == ('/usr/sbin/ip', 'link', 'set'):
                        raise OSError('interface failed')
                    if expected == 'ENDPOINT_ROUTE_FAILED' and args[:2] == ('/usr/sbin/ip', 'route'):
                        raise OSError('route failed')
                stack.enter_context(mock.patch.object(session, 'command', side_effect=command_call))
                stack.enter_context(mock.patch.object(session, 'setup_forwarding', side_effect=OSError('forwarding failed') if expected == 'FORWARDING_SETUP_FAILED' else None))
                self.assertEqual(session.main(), 1)
                self.assertIn(mock.call('ERROR', value, expected), emit.call_args_list)
                self.assertFalse(any(call.args[0] == 'READY' for call in emit.call_args_list))
                self.assertEqual(rules.call_args.args, (session.closed_firewall(),))
                start.assert_not_called()

    def archive(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as tar:
            for name, content, kind in members:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.type = kind
                tar.addfile(member, io.BytesIO(content))
        output.seek(0)
        return output

    def test_only_fixed_regular_member_and_unique_json_keys(self):
        value = {'schema': 1, 'nonce': 'a' * 32, 'revision': 'b' * 32,
                 'gateway_version': '2026.10.20.1', 'endpoint_ipv4': '203.0.113.1',
                 'profile': {'protocol': 'socks5', 'host': 'proxy.example', 'port': 1080}}
        result = session.read_session(self.archive([('session.json', json.dumps(value).encode(), tarfile.REGTYPE)]))
        self.assertEqual(result['nonce'], value['nonce'])
        for members in [ [('other', b'{}', tarfile.REGTYPE)],
                         [('session.json', b'{}', tarfile.SYMTYPE)],
                         [('session.json', b'{"schema":1,"schema":1}', tarfile.REGTYPE)],
                         [('session.json', b'x' * 65537, tarfile.REGTYPE)],
                         [('session.json', b'{}', tarfile.REGTYPE), ('extra', b'', tarfile.REGTYPE)]]:
            with self.assertRaises((ValueError, tarfile.TarError)):
                session.read_session(self.archive(members))

    def test_status_never_contains_profile_or_secret(self):
        value = {'nonce': 'a' * 32, 'revision': 'b' * 32, 'gateway_version': 'v1',
                 'profile': {'credentials': {'password': 'not-for-log'}}}
        status = session.status_record('READY', value)
        self.assertEqual(set(status), {'event', 'nonce', 'revision', 'gateway_version'})
        self.assertNotIn('not-for-log', json.dumps(status))

    def test_bad_base_version_stays_closed_without_starting_network_or_proxy(self):
        value = {'nonce': 'a' * 32, 'revision': 'b' * 32, 'gateway_version': 'wrong'}
        run = mock.MagicMock()
        with mock.patch.object(session, 'RUN', run), \
             mock.patch.object(session, 'VERSION') as version, \
             mock.patch.object(session, 'apply_rules') as rules, \
             mock.patch.object(session, 'emit') as emit, \
             mock.patch.object(session, 'command') as command, \
             mock.patch.object(session.glob, 'glob', return_value=['/dev/disk/by-id/usb-WCSESSION']), \
             mock.patch.object(session, 'read_session', return_value=value), \
             mock.patch('builtins.open', mock.mock_open(read_data=b'')), \
             mock.patch.object(session.subprocess, 'Popen') as start, \
             mock.patch.object(session.signal, 'signal'):
            version.read_text.return_value = '2026.10.20.1'
            self.assertEqual(session.main(), 1)
        self.assertTrue(all(call.args == (session.closed_firewall(),) for call in rules.call_args_list))
        self.assertFalse(command.called)
        self.assertFalse(start.called)
        self.assertFalse(any(call.args[0] == 'READY' for call in emit.call_args_list))
        self.assertFalse(any(call.args[0] == 'FIREWALL_READY' for call in emit.call_args_list))

    def test_nic_wait_requires_both_expected_macs_and_is_bounded(self):
        macs = {'/sys/class/net/wan0/address': '52:54:00:77:00:01',
                '/sys/class/net/lan0/address': '52:54:00:77:00:02'}
        def read_mac(path):
            return macs[path.as_posix()]
        with mock.patch.object(session.Path, 'read_text', autospec=True, side_effect=read_mac):
            session.wait_nics(lambda: False)
        with mock.patch.object(session.Path, 'read_text', return_value='52:54:00:00:00:00'), \
             mock.patch.object(session.time, 'monotonic', side_effect=[0, 0, 31]), \
             mock.patch.object(session.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                session.wait_nics(lambda: False)
            self.assertEqual(sleep.call_count, 1)
        with mock.patch.object(session.Path, 'read_text', side_effect=FileNotFoundError), \
             mock.patch.object(session.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaises(ValueError):
                session.wait_nics(lambda: False)

    def test_firewall_ack_has_current_identity_before_waiting_for_nics(self):
        value = {'schema': 1, 'nonce': 'a' * 32, 'revision': 'b' * 32,
                 'gateway_version': '2026.10.20.1', 'endpoint_ipv4': '203.0.113.1',
                 'profile': {'protocol': 'socks5', 'host': 'proxy.example', 'port': 1080}}
        order = []
        with mock.patch.object(session, 'RUN', mock.MagicMock()), \
             mock.patch.object(session, 'VERSION') as version, \
             mock.patch.object(session, 'apply_rules'), \
             mock.patch.object(session, 'emit', side_effect=lambda *args: order.append(args)), \
             mock.patch.object(session, 'command') as command, \
             mock.patch.object(session, 'wait_nics', side_effect=lambda *args: (order.append(('wait',)), (_ for _ in ()).throw(ValueError()))), \
             mock.patch.object(session.glob, 'glob', return_value=['/dev/disk/by-id/usb-WCSESSION']), \
             mock.patch.object(session, 'read_session', return_value=value), \
             mock.patch('builtins.open', mock.mock_open(read_data=b'')), \
             mock.patch.object(session.signal, 'signal'):
            version.read_text.return_value = value['gateway_version']
            self.assertEqual(session.main(), 1)
        self.assertEqual(order[:2], [('FIREWALL_READY', value), ('wait',)])
        self.assertEqual(session.status_record(*order[0])['nonce'], value['nonce'])
        self.assertEqual(command.call_count, 1)  # config check, no IP configuration

    def test_health_requires_dns_before_https_and_never_falls_back(self):
        with mock.patch.object(session, 'proxy_https', return_value=b'{"Status":2}') as probe:
            with self.assertRaises(ValueError):
                session.health()
            self.assertEqual(probe.call_count, 1)
        response = b'{"Status":0,"Answer":[{"type":1,"data":"93.184.215.14"}]}'
        with mock.patch.object(session, 'proxy_https', side_effect=[response, b'OK']) as probe:
            session.health()
            self.assertEqual(probe.call_args_list[1].args[:2], ('93.184.215.14', 'example.com'))


if __name__ == '__main__':
    unittest.main()


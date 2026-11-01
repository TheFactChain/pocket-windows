import sys
import unittest
from pathlib import Path
from unittest import mock
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gateway_profile as gateway


class GatewayProfileTests(unittest.TestCase):
    def session(self, protocol='hysteria2'):
        profile = {'protocol': protocol, 'host': 'proxy.example', 'port': 443}
        if protocol == 'hysteria2':
            profile['credentials'] = {'auth': 'secret'}
        return dict(schema=1, nonce='a' * 32, revision='b' * 32, gateway_version='1',
                    endpoint_ipv4='203.0.113.7', profile=profile)

    def test_each_protocol_is_pinned_and_has_no_direct_outbound(self):
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            config = gateway.sing_box_config(self.session(protocol))
            self.assertEqual(config['outbounds'][0]['server'], '203.0.113.7')
            self.assertEqual(len(config['outbounds']), 1)
            self.assertEqual(config['route']['final'], 'proxy')
            self.assertEqual(config['dns']['servers'][0]['detour'], 'proxy')
            if protocol in ('https', 'hysteria2'):
                self.assertEqual(config['outbounds'][0]['tls']['server_name'], 'proxy.example')

    def test_local_proxy_uses_the_qemu_host_address_in_config_and_firewall(self):
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            with self.subTest(protocol=protocol):
                session = self.session(protocol)
                session['profile']['host'] = '127.0.0.1'
                session['endpoint_ipv4'] = '127.0.0.1'
                normalized = gateway.validate_session(session)
                self.assertEqual(normalized['endpoint_ipv4'], '10.0.2.2')
                self.assertEqual(normalized['profile']['host'], '127.0.0.1')
                self.assertEqual(gateway.sing_box_config(session)['outbounds'][0]['server'], '10.0.2.2')
                rules = gateway.firewall(session)
                self.assertIn('ip daddr 10.0.2.2 ', rules)
                self.assertNotIn('ip daddr 127.0.0.1 ', rules)
                session['endpoint_ipv4'] = '10.0.2.2'
                self.assertEqual(gateway.validate_session(session)['endpoint_ipv4'], '10.0.2.2')

    def test_local_alias_still_must_match_a_literal_profile_address(self):
        session = self.session('socks5')
        session['profile']['host'] = '203.0.113.7'
        session['endpoint_ipv4'] = '127.0.0.1'
        with self.assertRaisesRegex(ValueError, 'match'):
            gateway.validate_session(session)
        session['profile']['host'] = '127.0.0.1'
        session['endpoint_ipv4'] = '203.0.113.7'
        with self.assertRaisesRegex(ValueError, 'match'):
            gateway.validate_session(session)

    def test_imported_local_proxy_reaches_the_host_local_uplink_branch(self):
        import gateway_controller
        from connection_profile import import_profile_uri
        profile = import_profile_uri('socks5://127.0.0.1:1080')
        store = mock.Mock()
        store.selected_summary.return_value = {'profile_id': 'a' * 32, 'revision_id': 'b' * 32}
        store.secret_config.return_value = profile
        store.session_profile.return_value = nullcontext((store.selected_summary.return_value, profile))
        _, session = gateway_controller.make_session(store, 'test')
        self.assertEqual(session['endpoint_ipv4'], '10.0.2.2')
        with mock.patch.object(gateway_controller, 'select_physical_ipv4') as physical:
            uplink = gateway_controller.select_uplink(session['endpoint_ipv4'])
        physical.assert_not_called()
        self.assertEqual(uplink['mode'], 'host-local-proxy')

    def test_other_loopback_addresses_cannot_be_redirected_to_a_different_service(self):
        from connection_profile import import_profile_uri
        for address in ('127.0.0.2', '127.1.2.3'):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    import_profile_uri('socks5://' + address + ':1080')
                session = self.session('socks5')
                session['endpoint_ipv4'] = address
                with self.assertRaises(ValueError):
                    gateway.validate_session(session)

    def test_firewall_has_no_lan_wan_forward_or_dns_egress(self):
        rules = gateway.firewall(self.session('socks5'))
        self.assertIn('policy drop', rules)
        self.assertIn('ip daddr 203.0.113.7 tcp dport 443', rules)
        self.assertNotIn('udp dport 53', gateway.closed_firewall())
        self.assertNotIn('iifname "lan0" oifname "wan0"', rules)

    def test_offline_rules_keep_only_proxy_loopback_and_dhcp(self):
        for protocol, transport in (('hysteria2','udp'),('socks5','tcp'),('http','tcp'),('https','tcp')):
            rules=gateway.firewall(self.session(protocol),connected=False)
            self.assertIn('oifname "wan0" ip daddr 203.0.113.7 '+transport+' dport 443 accept',rules)
            self.assertIn('iifname "lo" accept',rules)
            self.assertIn('oifname "lo" accept',rules)
            self.assertIn('udp sport 68 udp dport 67 accept',rules)
            self.assertIn('udp sport 67 udp dport 68 accept',rules)
            self.assertNotIn('wc-tun0',rules)
            self.assertNotIn('dport 53',rules)
            self.assertNotIn('sport 53',rules)
            self.assertNotIn('iifname "lan0" oifname "wan0"',rules)
            self.assertEqual(rules.count('policy drop;'),3)
        for invalid in (None,0,1,'false'):
            with self.assertRaises(ValueError):
                gateway.firewall(self.session(),connected=invalid)

    def test_ipv4_egress_keeps_local_ipv6_and_dns_detour(self):
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            config = gateway.sing_box_config(self.session(protocol))
            tun = config['inbounds'][0]
            self.assertEqual(tun['address'], ['172.19.0.1/30', 'fd77:19::1/126'])
            self.assertEqual(tun['route_exclude_address'], [
                '192.168.233.0/24', 'fd77:233::/64', 'fe80::/10', 'ff00::/8'])
            self.assertTrue(tun['strict_route'])
            self.assertEqual(config['dns']['strategy'], 'ipv4_only')
            self.assertIn({'type': 'direct', 'tag': 'lan-dns6', 'listen': 'fd77:233::1',
                           'listen_port': 53}, config['inbounds'])
            self.assertEqual(config['outbounds'][0]['server'], '203.0.113.7')
            rules = config['route']['rules']
            self.assertEqual(rules[0], {'port': 53, 'action': 'hijack-dns'})
            self.assertEqual(rules[1], {'ip_version': 6, 'action': 'reject'})
            self.assertEqual({'network': 'udp', 'action': 'reject'} in rules, protocol != 'hysteria2')

    def test_dual_stack_local_control_stays_up_without_allowing_offline_lan_or_wan6(self):
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            offline = gateway.firewall(self.session(protocol), connected=False)
            online = gateway.firewall(self.session(protocol))
            for rules in (offline, online):
                self.assertIn('icmpv6 type 133', rules)
                self.assertIn('icmpv6 type 134', rules)
                self.assertIn('icmpv6 type 135', rules)
                self.assertIn('icmpv6 type 136', rules)
                self.assertIn('ip6 hoplimit 255', rules)
                self.assertIn('udp sport 546 udp dport 547', rules)
                self.assertIn('udp sport 547 udp dport 546', rules)
                wan_accepts = [line for line in rules.splitlines() if '"wan0"' in line and 'accept' in line]
                self.assertTrue(all(' ip ' in line and 'ip6' not in line for line in wan_accepts))
                self.assertNotIn('icmpv6 type 137', rules)
                self.assertNotIn('iifname "lan0" oifname "wan0"', rules)
            self.assertNotIn('wc-tun0', offline)
            self.assertNotIn('dport 53', offline)
            self.assertNotIn('sport 53', offline)
            self.assertNotIn('ct state established,related accept', offline)
            self.assertNotIn('oifname "wc-tun0" meta nfproto ipv6', online)
            forwards = online.split(' chain forward {', 1)[1].split(' chain output {', 1)[0]
            self.assertIn('iifname "lan0" ip6 saddr fd77:233::/64 reject with icmpv6 type no-route', forwards)
            for line in forwards.splitlines():
                if 'accept' in line:
                    self.assertNotIn('ip6', line)
                    self.assertTrue(('iifname "lan0" oifname "wc-tun0"' in line)
                                    or ('iifname "wc-tun0" oifname "lan0"' in line))
            self.assertEqual('meta l4proto { tcp, udp }' in forwards, protocol == 'hysteria2')
            if protocol != 'hysteria2':
                self.assertNotIn('udp', forwards)

    def test_system_stack_tcp_listeners_are_reachable_only_from_owned_tun_pairs(self):
        expected = {
            'iifname "wc-tun0" ip saddr 172.19.0.2 ip daddr 172.19.0.1 meta l4proto tcp accept',
        }
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            online = gateway.firewall(self.session(protocol), connected=True)
            input_rules = online.split(' chain input {', 1)[1].split(' chain forward {', 1)[0]
            tcp_accepts = {line.strip() for line in input_rules.splitlines()
                           if 'iifname "wc-tun0"' in line and 'tcp' in line}
            self.assertEqual(tcp_accepts, expected)
            self.assertNotIn('wc-tun0', gateway.firewall(self.session(protocol), connected=False))

    def test_ipv4_pmtu_errors_from_gateway_reach_lan_only_while_connected(self):
        rule = ('oifname "lan0" ip saddr 192.168.233.1 ip daddr 192.168.233.0/24 '
                'ip protocol icmp ct state related accept')
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            online = gateway.firewall(self.session(protocol)).split(' chain output {', 1)[1]
            self.assertIn(rule, online)
            self.assertNotIn(rule, gateway.firewall(self.session(protocol), connected=False))

    RU = gateway.load_ru_ipv4((Path(__file__).resolve().parents[1] / 'gateway/ru-ipv4.txt').read_text(encoding='ascii'))

    def test_shipped_russian_list_holds_russian_services_and_never_foreign_or_proxy_addresses(self):
        import ipaddress
        networks = [ipaddress.IPv4Network(n) for n in self.RU]
        inside = lambda ip: any(ipaddress.IPv4Address(ip) in n for n in networks)
        for ip in ('77.88.55.88', '87.250.250.242', '95.213.11.180', '178.248.237.68'):  # Yandex, VK, Mail.ru
            self.assertTrue(inside(ip), ip)
        for ip in ('160.79.104.10', '104.18.32.47', '1.1.1.1', '8.8.8.8', '142.250.74.46',  # Anthropic, Cloudflare, Google
                   '188.130.234.243', '109.248.199.149', '46.8.43.165',  # the proxy VPS
                   '77.88.8.8', '77.88.8.1'):  # Yandex DNS: DoH must not reveal visited sites from the real IP
            self.assertFalse(inside(ip), ip)
        self.assertTrue(all(n.is_global for n in networks))

    def test_russian_list_parser_fails_closed(self):
        for text in ('10.0.0.0/8\n' + '5.0.0.0/24\n' * 1000, '5.0.0.0/24\n', '0.0.0.0/0\n' * 1000, 'not-a-network\n'):
            with self.assertRaises(ValueError):
                gateway.load_ru_ipv4(text)
        import ipaddress
        overlapping = '5.0.0.0/16\n' + ''.join(f'5.{i // 250}.{i % 250}.0/24\n' for i in range(1000))
        merged = [ipaddress.IPv4Network(n) for n in gateway.load_ru_ipv4(overlapping)]
        self.assertEqual(str(merged[0]), '5.0.0.0/16')
        self.assertFalse(any(a.overlaps(b) for a, b in zip(merged, merged[1:])))  # nft needs disjoint intervals

    def test_direct_russian_routing_is_off_unless_the_session_asks(self):
        for protocol in ('hysteria2', 'socks5'):
            config = gateway.sing_box_config(self.session(protocol), self.RU)
            self.assertEqual([o['tag'] for o in config['outbounds']], ['proxy'])
            self.assertNotIn('rule_set', config['route'])
            self.assertNotIn('ru_ipv4', gateway.firewall(self.session(protocol), ru_ipv4=self.RU))
        for flag in (1, 'true', None):
            session = self.session()
            session['direct_ru'] = flag
            with self.assertRaises(ValueError):
                gateway.validate_session(session)

    def test_enabled_without_the_list_fails_closed(self):
        session = dict(self.session(), direct_ru=True)
        with self.assertRaisesRegex(ValueError, 'missing'):
            gateway.sing_box_config(session)
        with self.assertRaisesRegex(ValueError, 'missing'):
            gateway.firewall(session)
        self.assertNotIn('ru_ipv4', gateway.firewall(session, connected=False))

    def test_only_tcp_to_russian_addresses_leaves_directly(self):
        for protocol in ('hysteria2', 'socks5', 'http', 'https'):
            session = dict(self.session(protocol), direct_ru=True)
            config = gateway.sing_box_config(session, self.RU)
            direct, = [o for o in config['outbounds'] if o['tag'] == 'direct']
            self.assertEqual(direct, {'type': 'direct', 'tag': 'direct', 'bind_interface': 'wan0'})
            rules = config['route']['rules']
            self.assertEqual(rules[0], {'action': 'sniff'})
            self.assertEqual(rules[1], {'port': 53, 'action': 'hijack-dns'})
            to_direct = [r for r in rules if r.get('outbound') == 'direct']
            self.assertEqual(to_direct, [{'network': 'tcp', 'rule_set': 'ru-ipv4', 'outbound': 'direct'}])
            self.assertEqual(rules[-1], to_direct[0])  # every proxy-forcing rule comes first
            self.assertIn({'port': [53, 853], 'outbound': 'proxy'}, rules)
            self.assertIn('2ip.ru', next(r for r in rules if 'domain_suffix' in r)['domain_suffix'])
            self.assertEqual(config['route']['final'], 'proxy')
            self.assertEqual(config['route']['rule_set'], [{'type': 'inline', 'tag': 'ru-ipv4', 'rules': [{'ip_cidr': self.RU}]}])
            self.assertEqual(config['dns']['servers'][0]['detour'], 'proxy')

    def test_gateway_firewall_itself_allows_only_tcp_to_russian_addresses(self):
        for protocol, transport in (('hysteria2', 'udp'), ('socks5', 'tcp')):
            session = dict(self.session(protocol), direct_ru=True)
            rules = gateway.firewall(session, ru_ipv4=self.RU)
            self.assertIn(' set ru_ipv4 {\n  type ipv4_addr; flags interval;\n  elements = { ' + self.RU[0] + ', ', rules)
            output = rules.split(' chain output {', 1)[1]
            wan_out = sorted(line.strip() for line in output.splitlines() if 'oifname "wan0"' in line)
            self.assertEqual(wan_out, sorted([
                f'oifname "wan0" ip daddr 203.0.113.7 {transport} dport 443 accept',
                'oifname "wan0" ip daddr @ru_ipv4 tcp dport != { 53, 853 } accept']))
            input_rules = rules.split(' chain input {', 1)[1].split(' chain forward {', 1)[0]
            self.assertIn('iifname "wan0" ip saddr @ru_ipv4 tcp sport != { 53, 853 } ct state established accept', input_rules)
            self.assertNotIn('iifname "lan0" oifname "wan0"', rules)
            offline = gateway.firewall(session, connected=False, ru_ipv4=self.RU)
            self.assertNotIn('ru_ipv4', offline)

    def test_rejects_injection_unknown_fields_and_missing_auth(self):
        for key, value in [('endpoint_ipv4', '1.2.3.4; accept'), ('nonce', 'bad'), ('revision', True), ('surprise', 1)]:
            session = self.session()
            session[key] = value
            with self.assertRaises(ValueError):
                gateway.sing_box_config(session)
        session = self.session()
        del session['profile']['credentials']
        with self.assertRaises(ValueError):
            gateway.sing_box_config(session)


if __name__ == '__main__':
    unittest.main()


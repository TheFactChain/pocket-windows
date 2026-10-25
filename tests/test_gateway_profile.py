import sys
import unittest
from pathlib import Path

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


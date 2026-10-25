"""Pure generation of the pinned, fail-closed gateway session configuration."""
import ipaddress
import re
from connection_profile import validate_profile


def validate_session(value):
    fields = {'schema', 'nonce', 'revision', 'gateway_version', 'endpoint_ipv4', 'profile'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Invalid session fields')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unsupported session schema')
    if not isinstance(value['nonce'], str) or not re.fullmatch('[a-f0-9]{32,64}', value['nonce']):
        raise ValueError('Invalid session nonce')
    if not isinstance(value['revision'], str) or not re.fullmatch('[a-f0-9]{32}', value['revision']):
        raise ValueError('Invalid profile revision')
    if not isinstance(value['gateway_version'], str) or not re.fullmatch('[A-Za-z0-9_.-]{1,64}', value['gateway_version']):
        raise ValueError('Invalid gateway version')
    endpoint = str(ipaddress.IPv4Address(value['endpoint_ipv4']))
    address = ipaddress.IPv4Address(endpoint)
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError('Invalid endpoint address')
    profile = validate_profile(value['profile'])
    if profile['protocol'] == 'hysteria2' and not profile.get('credentials', {}).get('auth'):
        raise ValueError('Hysteria2 password required')
    # Hostname resolution is performed by the controller before the NIC is activated.
    try:
        if str(ipaddress.IPv4Address(profile['host'])) != endpoint:
            raise ValueError('Endpoint does not match profile')
    except ipaddress.AddressValueError:
        pass
    return {**value, 'profile': profile, 'endpoint_ipv4': endpoint}


def sing_box_config(value):
    session = validate_session(value)
    p = session['profile']
    protocol = p['protocol']
    outbound = {'type': 'http' if protocol == 'https' else 'socks' if protocol == 'socks5' else protocol,
                'tag': 'proxy', 'server': session['endpoint_ipv4'], 'server_port': p['port'],
                'bind_interface': 'wan0'}
    credentials = p.get('credentials', {})
    if protocol == 'hysteria2':
        outbound['password'] = credentials['auth']
        if p.get('obfs'):
            outbound['obfs'] = {'type': 'salamander', 'password': credentials['obfs_password']}
    elif credentials:
        outbound.update(username=credentials['username'], password=credentials['password'])
    if protocol == 'socks5':
        outbound.update(version='5', network='tcp')
    if protocol in ('hysteria2', 'https'):
        outbound['tls'] = {'enabled': True, 'server_name': p.get('tls_server_name', p['host']), 'insecure': False}
    rules = [{'port': 53, 'action': 'hijack-dns'}, {'ip_version': 6, 'action': 'reject'}]
    if protocol != 'hysteria2':
        rules.append({'network': 'udp', 'action': 'reject'})
    return {'log': {'disabled': True},
            'dns': {'servers': [{'type': 'https', 'tag': 'secure-dns', 'server': '1.1.1.1',
                                  'server_port': 443, 'path': '/dns-query', 'detour': 'proxy',
                                  'tls': {'enabled': True, 'server_name': 'cloudflare-dns.com'}}],
                    'final': 'secure-dns', 'strategy': 'ipv4_only'},
            'inbounds': [{'type': 'tun', 'tag': 'tun', 'interface_name': 'wc-tun0',
                          'address': ['172.19.0.1/30', 'fd77:19::1/126'],
                          'route_exclude_address': ['192.168.233.0/24', 'fd77:233::/64', 'fe80::/10', 'ff00::/8'],
                          'mtu': 1280, 'auto_route': True, 'strict_route': True,
                          'stack': 'system'},
                         {'type': 'direct', 'tag': 'lan-dns', 'listen': '192.168.233.1',
                          'listen_port': 53},
                         {'type': 'direct', 'tag': 'lan-dns6', 'listen': 'fd77:233::1', 'listen_port': 53},
                         {'type': 'mixed', 'tag': 'health', 'listen': '127.0.0.1', 'listen_port': 18080}],
            'outbounds': [outbound],
            'route': {'rules': rules, 'final': 'proxy', 'auto_detect_interface': False}}


def closed_firewall():
    return '''flush ruleset
table inet windowsclaude {
 chain input { type filter hook input priority filter; policy drop; iifname "lo" accept; }
 chain forward { type filter hook forward priority filter; policy drop; }
 chain output { type filter hook output priority filter; policy drop; oifname "lo" accept; }
}
'''


def firewall(value, connected=True):
    if type(connected) is not bool:
        raise ValueError('A boolean connection state is required')
    session = validate_session(value)
    p = session['profile']
    transport = 'udp' if p['protocol'] == 'hysteria2' else 'tcp'
    networks = '{ tcp, udp }' if p['protocol'] == 'hysteria2' else 'tcp'
    lan_input, forwarding, lan_output = '', '', ''
    if connected:
        # The system TUN stack reinjects TCP into these local listener pairs.
        lan_input += '  iifname "wc-tun0" ip saddr 172.19.0.2 ip daddr 172.19.0.1 meta l4proto tcp accept\n'
        for family, network, gateway in (('ip', '192.168.233.0/24', '192.168.233.1'),
                                          ('ip6', 'fd77:233::/64', 'fd77:233::1')):
            for dns_transport in ('udp', 'tcp'):
                lan_input += f'  iifname "lan0" {family} saddr {network} {family} daddr {gateway} {dns_transport} dport 53 accept\n'
                lan_output += f'  oifname "lan0" {family} saddr {gateway} {family} daddr {network} {dns_transport} sport 53 accept\n'
        forwarding += f'  iifname "lan0" oifname "wc-tun0" ip saddr 192.168.233.0/24 meta l4proto {networks} accept\n'
        forwarding += f'  iifname "wc-tun0" oifname "lan0" ip daddr 192.168.233.0/24 meta l4proto {networks} ct state established,related accept\n'
        # Fail cached IPv6 destinations immediately; local IPv6 remains available.
        forwarding += '  iifname "lan0" ip6 saddr fd77:233::/64 reject with icmpv6 type no-route\n'
        lan_output += f'  oifname "wc-tun0" meta nfproto ipv4 meta l4proto {networks} accept\n'
        # Preserve PMTU/error delivery only along the owned TUN/LAN path.
        forwarding += '  iifname "wc-tun0" oifname "lan0" ip daddr 192.168.233.0/24 ip protocol icmp ct state related accept\n'
        lan_output += '  oifname "lan0" ip saddr 192.168.233.1 ip daddr 192.168.233.0/24 ip protocol icmp ct state related accept\n'
        lan_output += '  oifname "lan0" ip6 daddr fd77:233::/64 meta l4proto ipv6-icmp icmpv6 type { 1, 2, 3, 4 } accept\n'
    # These link-local controls remain available while forwarding/DNS are closed.
    local_input = '''  iifname "lan0" ip6 saddr { ::/128, fe80::/10 } ip6 daddr ff02::2 ip6 hoplimit 255 meta l4proto ipv6-icmp icmpv6 type 133 accept
  iifname "lan0" ip6 saddr { ::/128, fe80::/10, fd77:233::/64 } ip6 daddr { fe80::/10, fd77:233::1, ff02::1:ff00:0/104 } ip6 hoplimit 255 meta l4proto ipv6-icmp icmpv6 type 135 accept
  iifname "lan0" ip6 saddr { fe80::/10, fd77:233::/64 } ip6 daddr { fe80::/10, fd77:233::1, ff02::1 } ip6 hoplimit 255 meta l4proto ipv6-icmp icmpv6 type 136 accept
  iifname "lan0" ip6 saddr { ::/128, fe80::/10 } ip6 daddr ff02::/16 ip6 hoplimit 1 meta l4proto ipv6-icmp icmpv6 type { 130, 131, 132, 143 } accept
  iifname "lan0" ip6 saddr { fe80::/10, fd77:233::/64 } ip6 daddr { ff02::1:2, fe80::/10, fd77:233::1 } udp sport 546 udp dport 547 accept
'''
    local_output = '''  oifname "lan0" ip6 saddr fe80::/10 ip6 daddr { ff02::1, fe80::/10 } ip6 hoplimit 255 meta l4proto ipv6-icmp icmpv6 type 134 accept
  oifname "lan0" ip6 saddr { ::/128, fe80::/10, fd77:233::1 } ip6 daddr { fe80::/10, fd77:233::/64, ff02::1:ff00:0/104 } ip6 hoplimit 255 meta l4proto ipv6-icmp icmpv6 type 135 accept
  oifname "lan0" ip6 saddr { fe80::/10, fd77:233::1 } ip6 daddr { fe80::/10, fd77:233::/64, ff02::1 } ip6 hoplimit 255 meta l4proto ipv6-icmp icmpv6 type 136 accept
  oifname "lan0" ip6 saddr { ::/128, fe80::/10 } ip6 daddr ff02::/16 ip6 hoplimit 1 meta l4proto ipv6-icmp icmpv6 type { 130, 131, 132, 143 } accept
  oifname "lan0" ip6 saddr { fe80::/10, fd77:233::1 } ip6 daddr { fe80::/10, fd77:233::/64, ff02::1:2 } udp sport 547 udp dport 546 accept
'''
    return f'''flush ruleset
table inet windowsclaude {{
 chain input {{
  type filter hook input priority filter; policy drop;
  iifname "lo" accept
  ct state invalid drop
  iifname "wan0" ip saddr {session['endpoint_ipv4']} {transport} sport {p['port']} ct state established accept
  iifname "wan0" ip protocol icmp ct state related accept
  iifname "lan0" ip saddr {{ 0.0.0.0, 192.168.233.0/24 }} udp sport 68 udp dport 67 accept
{local_input}{lan_input} }}
 chain forward {{
  type filter hook forward priority filter; policy drop;
{forwarding} }}
 chain output {{
  type filter hook output priority filter; policy drop;
  oifname "lo" accept
  oifname "wan0" ip daddr {session['endpoint_ipv4']} {transport} dport {p['port']} accept
  oifname "lan0" ip daddr {{ 192.168.233.0/24, 255.255.255.255 }} udp sport 67 udp dport 68 accept
{local_output}{lan_output} }}
}}
'''

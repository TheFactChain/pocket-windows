"""Pure generation of the pinned, fail-closed gateway session configuration."""
import ipaddress
import re
from connection_profile import validate_profile, validate_ipv4_endpoint

# Russian services that report the caller's address back: a foreign page could ask them for the real IP.
# shortcut: a known list only, any other Russian echo service still answers with the real IP.
IP_ECHO = ['2ip.ru', '2ip.io', '2ip.me', 'myip.ru', 'ip-ping.ru', 'whatismyip.ru',
           'internet.yandex.ru', 'ipinfo.ru', 'ip.ru', 'ip2geo.ru']
NEVER_DIRECT_PORTS = (53, 853)


def load_ru_ipv4(text):
    """Strict list of Russian IPv4 networks (one CIDR per line, # comments); anything odd fails closed."""
    networks = []
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        network = ipaddress.IPv4Network(line)
        if not network.is_global or network.prefixlen < 8:
            raise ValueError('Unexpected network in the Russian list')
        networks.append(network)
    if len(networks) < 1000:
        raise ValueError('The Russian list is incomplete')
    # nft refuses overlapping interval elements; merging keeps the gateway up whatever the file holds.
    return [str(n) for n in ipaddress.collapse_addresses(networks)]


def endpoint_ipv4(value):
    """Use the QEMU host alias for a proxy running on this PC, before generating any rules."""
    address = validate_ipv4_endpoint(value)
    return '10.0.2.2' if address == '127.0.0.1' else address


def validate_session(value):
    fields = {'schema', 'nonce', 'revision', 'gateway_version', 'endpoint_ipv4', 'profile'}
    if not isinstance(value, dict) or not fields <= set(value) <= fields | {'direct_ru'}:
        raise ValueError('Invalid session fields')
    if type(value.get('direct_ru', False)) is not bool:
        raise ValueError('Invalid direct Russian routing flag')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unsupported session schema')
    if not isinstance(value['nonce'], str) or not re.fullmatch('[a-f0-9]{32,64}', value['nonce']):
        raise ValueError('Invalid session nonce')
    if not isinstance(value['revision'], str) or not re.fullmatch('[a-f0-9]{32}', value['revision']):
        raise ValueError('Invalid profile revision')
    if not isinstance(value['gateway_version'], str) or not re.fullmatch('[A-Za-z0-9_.-]{1,64}', value['gateway_version']):
        raise ValueError('Invalid gateway version')
    endpoint = endpoint_ipv4(value['endpoint_ipv4'])
    profile = validate_profile(value['profile'])
    if profile['protocol'] == 'hysteria2' and not profile.get('credentials', {}).get('auth'):
        raise ValueError('Hysteria2 password required')
    # Hostname resolution is performed by the controller before the NIC is activated.
    try:
        literal = str(ipaddress.IPv4Address(profile['host']))
    except ipaddress.AddressValueError:
        pass
    else:
        if endpoint_ipv4(literal) != endpoint:
            raise ValueError('Endpoint does not match profile')
    return {**value, 'profile': profile, 'endpoint_ipv4': endpoint}


def direct_networks(session, ru_ipv4):
    """Russian networks reachable around the proxy, or none: an enabled flag without the list fails closed."""
    if not session.get('direct_ru'):
        return []
    if not ru_ipv4:
        raise ValueError('The Russian network list is missing')
    return ru_ipv4


def sing_box_config(value, ru_ipv4=()):
    session = validate_session(value)
    direct = direct_networks(session, ru_ipv4)
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
    outbounds, rule_sets = [outbound], []
    if direct:
        # Only TCP to a Russian address leaves directly; UDP (WebRTC, QUIC), DNS ports and IP echo services keep the proxy.
        rules = [{'action': 'sniff'}] + rules + [
            {'port': list(NEVER_DIRECT_PORTS), 'outbound': 'proxy'},
            {'domain_suffix': IP_ECHO, 'outbound': 'proxy'},
            {'network': 'tcp', 'rule_set': 'ru-ipv4', 'outbound': 'direct'}]
        outbounds.append({'type': 'direct', 'tag': 'direct', 'bind_interface': 'wan0'})
        rule_sets = [{'type': 'inline', 'tag': 'ru-ipv4', 'rules': [{'ip_cidr': direct}]}]
    route = {'rules': rules, 'final': 'proxy', 'auto_detect_interface': False}
    if rule_sets:
        route['rule_set'] = rule_sets
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
            'outbounds': outbounds,
            'route': route}


def closed_firewall():
    return '''flush ruleset
table inet windowsclaude {
 chain input { type filter hook input priority filter; policy drop; iifname "lo" accept; }
 chain forward { type filter hook forward priority filter; policy drop; }
 chain output { type filter hook output priority filter; policy drop; oifname "lo" accept; }
}
'''


def firewall(value, connected=True, ru_ipv4=()):
    if type(connected) is not bool:
        raise ValueError('A boolean connection state is required')
    session = validate_session(value)
    direct = direct_networks(session, ru_ipv4) if connected else []
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
    ru_set, wan_input, wan_output = '', '', ''
    if direct:
        # Independent of sing-box: the gateway itself can reach no address but the proxy and Russian networks over TCP.
        ru_set = ' set ru_ipv4 {\n  type ipv4_addr; flags interval;\n  elements = { ' + ', '.join(direct) + ' }\n }\n'
        ports = ', '.join(map(str, NEVER_DIRECT_PORTS))
        wan_input = f'  iifname "wan0" ip saddr @ru_ipv4 tcp sport != {{ {ports} }} ct state established accept\n'
        wan_output = f'  oifname "wan0" ip daddr @ru_ipv4 tcp dport != {{ {ports} }} accept\n'
    return f'''flush ruleset
table inet windowsclaude {{
{ru_set} chain input {{
  type filter hook input priority filter; policy drop;
  iifname "lo" accept
  ct state invalid drop
  iifname "wan0" ip saddr {session['endpoint_ipv4']} {transport} sport {p['port']} ct state established accept
{wan_input}  iifname "wan0" ip protocol icmp ct state related accept
  iifname "lan0" ip saddr {{ 0.0.0.0, 192.168.233.0/24 }} udp sport 68 udp dport 67 accept
{local_input}{lan_input} }}
 chain forward {{
  type filter hook forward priority filter; policy drop;
{forwarding} }}
 chain output {{
  type filter hook output priority filter; policy drop;
  oifname "lo" accept
  oifname "wan0" ip daddr {session['endpoint_ipv4']} {transport} dport {p['port']} accept
{wan_output}  oifname "lan0" ip daddr {{ 192.168.233.0/24, 255.255.255.255 }} udp sport 67 udp dport 68 accept
{local_output}{lan_output} }}
}}
'''

"""Build gateway/ru-ipv4.txt: IPv4 networks that are both geolocated to Russia and registered to Russia.

Build tool only. Usage: python -I -B scripts/build_ru_ipv4.py <sing-box.exe>
Sources: SagerNet geoip-ru rule set (decompiled by sing-box) and the RIPE NCC delegated statistics.
Requiring both keeps a foreign server out of the list when one source is wrong.
"""
import datetime
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GEOIP = 'https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-ru.srs'
RIPE = 'https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest'
# Never direct: the proxy VPS ranges, and Yandex DNS so DNS-over-HTTPS from Windows cannot reveal visited sites.
NEVER = ('188.130.234.0/24', '109.248.199.0/24', '46.8.43.0/24', '77.88.8.0/24')


def intersect(first, second):
    a = [(int(n.network_address), int(n.broadcast_address)) for n in ipaddress.collapse_addresses(first)]
    b = [(int(n.network_address), int(n.broadcast_address)) for n in ipaddress.collapse_addresses(second)]
    i = j = 0
    result = []
    while i < len(a) and j < len(b):
        low, high = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if low <= high:
            result += ipaddress.summarize_address_range(ipaddress.IPv4Address(low), ipaddress.IPv4Address(high))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return list(ipaddress.collapse_addresses(result))


def ripe_ru(text):
    for line in text.splitlines():
        p = line.split('|')
        if len(p) > 6 and p[1] == 'RU' and p[2] == 'ipv4' and p[6] in ('allocated', 'assigned'):
            start = ipaddress.IPv4Address(p[3])
            yield from ipaddress.summarize_address_range(start, start + int(p[4]) - 1)


def build(geo, registered):
    networks = [n for n in intersect(geo, registered) if n.is_global]
    for excluded in map(ipaddress.IPv4Network, NEVER):
        networks = [part for n in networks for part in
                    (n.address_exclude(excluded) if n.overlaps(excluded) and n.supernet_of(excluded) else
                     [] if n.overlaps(excluded) else [n])]
    return sorted(ipaddress.collapse_addresses(networks))


def self_test():
    geo = [ipaddress.IPv4Network('5.0.0.0/16'), ipaddress.IPv4Network('46.8.0.0/16')]
    reg = [ipaddress.IPv4Network('5.0.128.0/17'), ipaddress.IPv4Network('46.8.0.0/16'), ipaddress.IPv4Network('8.8.8.0/24')]
    assert build(geo, reg) == list(ipaddress.collapse_addresses(
        [ipaddress.IPv4Network('5.0.128.0/17')] + list(ipaddress.IPv4Network('46.8.0.0/16').address_exclude(
            ipaddress.IPv4Network('46.8.43.0/24')))))
    assert list(ripe_ru('ripencc|RU|ipv4|5.0.0.0|512|20100101|allocated|x\nripencc|DE|ipv4|8.0.0.0|256|1|assigned|y')) == \
        [ipaddress.IPv4Network('5.0.0.0/23')]


def main():
    self_test()
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    with tempfile.TemporaryDirectory() as folder:
        srs, decompiled = Path(folder) / 'geoip-ru.srs', Path(folder) / 'geoip-ru.json'
        srs.write_bytes(urllib.request.urlopen(GEOIP, timeout=60).read())
        subprocess.run([sys.argv[1], 'rule-set', 'decompile', str(srs), '-o', str(decompiled)], check=True)
        geo = [ipaddress.IPv4Network(c) for rule in json.loads(decompiled.read_text())['rules']
               for c in rule.get('ip_cidr', []) if ':' not in c]
    registered = list(ripe_ru(urllib.request.urlopen(RIPE, timeout=120).read().decode('ascii')))
    networks = build(geo, registered)
    header = (f'# Russian IPv4 networks reached around the proxy when direct Russian routing is on.\n'
              f'# Built {datetime.date.today()} by scripts/build_ru_ipv4.py: geoip-ru AND RIPE NCC RU, '
              f'minus {", ".join(NEVER)}.\n')
    (ROOT / 'gateway/ru-ipv4.txt').write_text(header + ''.join(f'{n}\n' for n in networks), encoding='ascii', newline='\n')
    print(len(networks), 'networks,', sum(n.num_addresses for n in networks), 'addresses')


if __name__ == '__main__':
    main()

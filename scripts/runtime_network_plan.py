"""Fixed guest-only topology, with no host adapters, bridges or forwards."""
from pathlib import PurePosixPath


def arguments(role, log_directory, lan_port=31861):
    path=PurePosixPath(log_directory)
    if path.is_absolute() or '..' in path.parts or any(c in log_directory for c in ', :\\'):
        raise ValueError('Safe relative ASCII log directory required')
    if type(lan_port) is not int or not 1024 <= lan_port <= 65535:
        raise ValueError('Unprivileged loopback LAN port required')
    lan=f'addr.type=inet,addr.host=127.0.0.1,addr.port={lan_port},addr.numeric=on,addr.ipv4=on,addr.ipv6=off'
    if role=='work':
        return ['-netdev','stream,id=wclan,server=off,'+lan+',reconnect-ms=1000',
                '-device','e1000,id=wcwork,netdev=wclan,mac=52:54:00:77:00:03']
    if role=='gateway':
        return ['-netdev','stream,id=wclan,server=on,'+lan,
                '-netdev','user,id=wcwan,ipv6=off,net=10.0.2.0/24,host=10.0.2.2',
                '-object','filter-dump,id=wc-wan-audit,netdev=wcwan,file='+str(path/'wan.pcap'),
                '-device','virtio-net-pci,id=wcnicwan,netdev=wcwan,mac=52:54:00:77:00:01',
                '-device','virtio-net-pci,id=wcniclan,netdev=wclan,mac=52:54:00:77:00:02']
    raise ValueError('Known guest role required')

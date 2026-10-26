"""Select a physical Windows IPv4 uplink without changing host networking."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess


_QUERY = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$rows = @(
    foreach ($adapter in Get-NetAdapter -Physical | Where-Object Status -eq 'Up') {
        $index = $adapter.ifIndex
        $ipInterface = Get-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction SilentlyContinue
        if (-not $ipInterface) { continue }
        $addresses = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
            Where-Object { $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource })
        foreach ($route in Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -PolicyStore ActiveStore -ErrorAction SilentlyContinue) {
            foreach ($address in $addresses) {
                [pscustomobject]@{
                    interface_index = [long]$index
                    local_ipv4 = [string]$address.IPAddress
                    gateway_ipv4 = [string]$route.NextHop
                    prefix_length = [int]$address.PrefixLength
                    route_metric = [long]$route.RouteMetric
                    interface_metric = [long]$ipInterface.InterfaceMetric
                    physical = [bool]$adapter.HardwareInterface
                    up = $true
                }
            }
        }
    }
)
ConvertTo-Json -InputObject @($rows) -Compress
'''


def choose_physical_ipv4(rows):
    candidates = []
    fields = {'interface_index', 'local_ipv4', 'gateway_ipv4', 'prefix_length',
              'route_metric', 'interface_metric', 'physical', 'up'}
    if not isinstance(rows, list):
        raise RuntimeError('DIRECT_NETWORK_UNAVAILABLE')
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields or row['physical'] is not True or row['up'] is not True:
            continue
        if any(type(row[key]) is not int or not 0 <= row[key] < 2**32
               for key in ('interface_index', 'route_metric', 'interface_metric')):
            continue
        if not 1 <= row['interface_index'] < 2**24 or type(row['prefix_length']) is not int or not 1 <= row['prefix_length'] <= 32:
            continue
        try:
            local = ipaddress.IPv4Address(row['local_ipv4']) if isinstance(row['local_ipv4'], str) else None
            gateway = ipaddress.IPv4Address(row['gateway_ipv4']) if isinstance(row['gateway_ipv4'], str) else None
            if any(address is None or address.is_loopback or address.is_link_local or address.is_multicast
                   or address.is_unspecified or address.is_reserved for address in (local, gateway)):
                continue
            if local == gateway:
                continue
        except (ValueError, TypeError):
            continue
        candidates.append((row['route_metric'] + row['interface_metric'], row['interface_index'],
                           int(local), int(gateway)))
    if not candidates:
        raise RuntimeError('DIRECT_NETWORK_UNAVAILABLE')
    _, index, local, gateway = min(candidates)
    return {'interface_index': index, 'local_ipv4': str(ipaddress.IPv4Address(local)),
            'gateway_ipv4': str(ipaddress.IPv4Address(gateway))}


def select_physical_ipv4():
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not powershell.is_absolute():
        raise RuntimeError('DIRECT_NETWORK_UNAVAILABLE')
    try:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', _QUERY],
                                check=True, capture_output=True, encoding='utf-8', timeout=12,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if len(result.stdout) > 65536:
            raise ValueError('Oversized network result')
        return choose_physical_ipv4(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, UnicodeError):
        raise RuntimeError('DIRECT_NETWORK_UNAVAILABLE') from None



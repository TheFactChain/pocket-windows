"""Render guest files from explicit public endpoint metadata; never activate them."""
import ipaddress
import json


def validate_public_profile(value):
    allowed = {'schema', 'endpoint_ipv4', 'endpoint_port', 'expected_egress_ipv4'}
    if not isinstance(value, dict) or set(value) - allowed or value.get('schema') != 1:
        raise ValueError('Public profile schema 1 required')
    try:
        address = ipaddress.IPv4Address(value['endpoint_ipv4'])
        port = value['endpoint_port']
        egress = ipaddress.IPv4Address(value.get('expected_egress_ipv4', str(address)))
    except (KeyError, ValueError, TypeError, ipaddress.AddressValueError) as error:
        raise ValueError('A numeric public IPv4 endpoint is required') from error
    if not address.is_global or not egress.is_global or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('A global IPv4 endpoint and valid UDP port are required')
    return {'schema': 1, 'endpoint_ipv4': str(address), 'endpoint_port': port,
            'expected_egress_ipv4': str(egress)}


def render_guest_files(templates, value):
    profile = validate_public_profile(value)
    result = {}
    for template, name in [('nftables.vpn.template.nft', 'nftables.vpn.nft'),
                           ('interfaces.template', 'interfaces')]:
        text = (templates / template).read_text(encoding='utf-8')
        text = text.replace('{{VPS_IPV4}}', profile['endpoint_ipv4'])
        text = text.replace('{{VPS_PORT}}', str(profile['endpoint_port']))
        if '{{' in text or '}}' in text:
            raise ValueError('Unresolved guest template field')
        result[name] = text
    result['profile-public.json'] = json.dumps(profile, indent=2) + '\n'
    return result

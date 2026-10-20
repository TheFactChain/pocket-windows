"""Non-secret host-independent runtime settings; profile/VPS data belongs elsewhere."""
import json

DEFAULTS = {'schema': 1, 'work_memory_mib': 4096, 'gateway_memory_mib': 1024,
            'vcpus': 2, 'lan_port': 31861}
RANGES = {'work_memory_mib': (1024, 32768), 'gateway_memory_mib': (256, 4096),
          'vcpus': (1, 32), 'lan_port': (1024, 65535)}


def load_settings(root):
    path = root / 'config/runtime.json'
    value = {}
    if path.exists():
        if path.stat().st_size > 64 * 1024:
            raise ValueError('Runtime settings file is too large')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Runtime settings object required')
    if set(value) - set(DEFAULTS):
        raise ValueError('Unknown runtime setting; profiles and secrets must be separate')
    result = DEFAULTS | value
    if type(result['schema']) is not int or result['schema'] != 1:
        raise ValueError('Unsupported runtime settings schema')
    for key, (low, high) in RANGES.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid runtime setting: ' + key)
    return result

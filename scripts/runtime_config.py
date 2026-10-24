"""Non-secret host-independent runtime settings; profile/VPS data belongs elsewhere."""
import json

DEFAULTS = {'schema': 1, 'work_memory_mib': 4096, 'gateway_memory_mib': 1024,
            'vcpus': 2, 'lan_port': 31861}
RANGES = {'work_memory_mib': (1024, 32768), 'gateway_memory_mib': (256, 4096),
          'vcpus': (1, 32), 'lan_port': (1024, 65535)}
VIEWER_DEFAULTS = {'schema': 1, 'start_fullscreen': False, 'remember_window': True, 'share_clipboard': True}
DISPLAY_UPDATE_MS = 16  # picture update period of the SDL window, ~62 Hz: the guest display never exceeds about 60


def validate_viewer_settings(value):
    if not isinstance(value, dict) or set(value) - set(VIEWER_DEFAULTS):
        raise ValueError('Unknown viewer setting')
    result = VIEWER_DEFAULTS | value
    if type(result['schema']) is not int or result['schema'] != 1:
        raise ValueError('Unsupported viewer settings schema')
    if any(type(result[key]) is not bool for key in ('start_fullscreen', 'remember_window', 'share_clipboard')):
        raise ValueError('Boolean viewer settings required')
    return result


def load_viewer_settings(root):
    from file_snapshot import locked_read
    from profile_store import _pinned_directory, _plain

    folder = root / 'config'
    with _pinned_directory(root, create=False):
        if not folder.exists():
            return dict(VIEWER_DEFAULTS)
        with _pinned_directory(folder, create=False):
            path = folder / 'viewer.json'
            _plain(path)
            if not path.exists():
                return dict(VIEWER_DEFAULTS)
            with locked_read(path) as source:
                value = source.read(16385)
            if len(value) > 16384:
                raise ValueError('Viewer settings file is too large')
            return validate_viewer_settings(json.loads(value))


def load_settings(root):
    path = root / 'config/runtime.json'
    value = {}
    if path.exists():
        if path.stat().st_size > 64 * 1024:
            raise ValueError('Runtime settings file is too large')
        value = json.loads(path.read_text(encoding='utf-8'))
    return validate_settings(value)


def validate_settings(value):
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

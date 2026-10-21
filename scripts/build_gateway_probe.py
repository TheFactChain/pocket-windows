"""Build a disposable acceptance-only Debian disk using the verified gateway installer."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_gateway as builder

VERSION = 'acceptance-probe-v1'


@contextmanager
def acceptance_delivery():
    original_writer, original_version = builder.write_delivery, builder.GATEWAY_VERSION

    def write_probe(files, destination):
        # Reuse the public delivery whitelist, locked readers and SHA256SUMS writer.
        files = dict(files)
        source = files['install.sh'].read_bytes()
        marker = f"printf 'WC_BUILD_COMPLETE {original_version}\\n' > /dev/ttyS0".encode('ascii')
        if source.count(marker) != 1:
            raise ValueError('Gateway installer completion marker changed')
        addition = (builder.ROOT / 'gateway/acceptance/install-probe.sh').read_bytes()
        payload = source.replace(original_version.encode('ascii'), VERSION.encode('ascii'))
        new_marker = marker.replace(original_version.encode('ascii'), VERSION.encode('ascii'))
        payload = payload.replace(new_marker, addition + b'\n' + new_marker)
        copy = destination.parent / 'acceptance-install.sh'
        builder._write_bytes(copy, payload)
        files['install.sh'] = copy
        files['session.py'] = builder.ROOT / 'gateway/acceptance/probe.py'
        files['wc-session.service'] = builder.ROOT / 'gateway/acceptance/probe.service'
        return original_writer(files, destination)

    builder.GATEWAY_VERSION = VERSION
    builder.write_delivery = write_probe
    try:
        yield
    finally:
        builder.write_delivery = original_writer
        builder.GATEWAY_VERSION = original_version


def install_probe():
    # install_gateway owns ProjectLock, the no-running-VM check and every pinned source check.
    with acceptance_delivery():
        result = builder.install_gateway()
    operation = Path(result['operation'])
    disk = operation / 'gateway.qcow2'
    report = {'schema': 1, 'version': VERSION, 'operation': str(operation),
              'disk_bytes': result['disk_bytes'], 'disk_sha256': builder._hash(disk, 'sha256'),
              'virtual_bytes': 4 * 1024**3}
    builder.runtime.atomic_json(operation / 'probe-result.json', report)
    return result


if __name__ == '__main__':
    print(json.dumps(install_probe(), ensure_ascii=True))

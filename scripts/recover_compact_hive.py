"""Replay copied maintenance-hive transactions; never write the mounted guest."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_compact_master import plain_path
from launch_runtime import atomic_json

ARCHIVE_SHA256 = '5bc3a06a0f9ece64b3b4273f3c22bcb8e07251806474aeba393f3850d98ebe39'


def clean_header(data):
    if len(data) < 48 or data[:4] != b'regf':
        raise ValueError('Invalid SYSTEM hive header')
    first, second = struct.unpack_from('<II', data, 4)
    return first == second


def recover(operation, phase):
    operation = plain_path(operation, directory=True)
    if (not re.fullmatch('automatic-maintenance-[a-f0-9]{32}', operation.name)
            or not operation.parent.name.startswith('compact-master-')):
        raise ValueError('An isolated compact-master maintenance operation is required')
    archive = plain_path(operation / 'external-yarp-1.0.33/yarp-1.0.33.zip')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('Registry recovery tool archive differs from its approved source')
    directory = plain_path(operation / ('registry-before-' + phase), directory=True)
    records = {}
    for name in ('SYSTEM', 'SYSTEM.LOG', 'SYSTEM.LOG1', 'SYSTEM.LOG2'):
        path = directory / name
        if path.exists():
            plain_path(path)
            if path.stat().st_size > 64 * 1024**2:
                raise ValueError('Maintenance registry input is unexpectedly large')
            records[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if 'SYSTEM' not in records:
        raise ValueError('SYSTEM snapshot is missing')
    output = directory / 'SYSTEM.recovered'
    proof = directory / 'recovery.json'
    if output.exists() or proof.exists():
        raise FileExistsError('Recovery evidence already exists; inspect before retrying')
    # Execute the pinned archive directly, not an editable unpacked library.
    sys.path.insert(0, str(archive) + '/yarp-1.0.33')
    from yarp import Registry
    streams = []
    try:
        for name in ('SYSTEM', 'SYSTEM.LOG', 'SYSTEM.LOG1', 'SYSTEM.LOG2'):
            streams.append((directory / name).open('rb') if name in records else None)
        hive = Registry.RegistryHive(streams[0])
        result = hive.recover_auto(*streams[1:])
        hive.walk_everywhere()
        if result.recovered:
            hive.save_recovered_hive(str(output))
        else:
            output.write_bytes((directory / 'SYSTEM').read_bytes())
        if not clean_header(output.read_bytes()[:48]):
            raise ValueError('SYSTEM remains dirty after transaction replay')
        value = {'schema': 1, 'inputs': records, 'recovered': result.recovered,
                 'tool_sha256': ARCHIVE_SHA256,
                 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
        atomic_json(proof, value)
        return value
    finally:
        for stream in streams:
            if stream is not None:
                stream.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', type=Path, required=True)
    parser.add_argument('--phase', choices=('install', 'collect'), required=True)
    args = parser.parse_args()
    print(json.dumps(recover(args.operation, args.phase)))

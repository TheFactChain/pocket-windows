"""Prepare an explicit disposable maintenance VM; boot only with --run.

No original/master discovery, network, guest executor, or automatic shutdown.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compact_windows import _plain, _validate_info
from file_snapshot import QEMU_IMG_SHA256
from runtime_config import load_settings
import launch_runtime as runtime
import windows_hardware


def plain_path(value, directory=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path).startswith('\\\\'):
        raise ValueError('An absolute local path without parent traversal is required')
    for parent in reversed(path.parents):
        _plain(parent, directory=True)
    _plain(path, directory=directory)
    return path


def validate_master(value):
    master = plain_path(value, directory=True)
    if not master.name.startswith('compact-master-'):
        raise ValueError('Only an explicitly named compact-master-* copy is allowed')
    disk = plain_path(master / 'candidate.qcow2')
    firmware = plain_path(master / 'vars.fd')
    return master, disk, firmware


def arguments(project, master, settings):
    master, disk, firmware = validate_master(master)
    media = plain_path(master / 'maintenance-media', directory=True)
    for name in ('Compact-Windows.ps1', 'RUN.CMD'):
        plain_path(media / name)
    escape = lambda path: str(path).replace('\\', '/').replace(',', ',,')
    share = project / 'runtime/qemu-11.1.0/share'
    return ['-name', 'WindowsClaude-MAINTENANCE-COPY', '-machine', 'q35', '-accel', 'whpx',
            '-cpu', windows_hardware.CPU_MODEL, '-smp', str(settings['vcpus']),
            '-m', str(settings['work_memory_mib']), '-L', str(share),
            '-nic', 'none', '-display', 'sdl,gl=off,window-close=off', '-vga', 'std',
            '-serial', 'none', '-parallel', 'none', '-monitor', 'none',
            *windows_hardware.arguments(serial='WC-MAINTENANCE-COPY'),
            '-drive', 'if=pflash,unit=0,format=raw,readonly=on,file=' + escape(share / 'edk2-x86_64-code.fd'),
            '-drive', 'if=pflash,unit=1,format=raw,file=' + escape(firmware),
            '-drive', 'if=ide,index=0,format=qcow2,discard=unmap,file=' + escape(disk),
            '-device', 'qemu-xhci,id=maintenanceusb',
            '-drive', 'if=none,id=maintenance,format=raw,readonly=on,file=fat:' + escape(media),
            '-device', 'usb-storage,drive=maintenance,bus=maintenanceusb.0,removable=on',
            '-boot', 'order=c,strict=on']


def checked_disk(project, disk):
    tool = plain_path(project / 'runtime/qemu-11.1.0/qemu-img.exe')
    with tool.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != QEMU_IMG_SHA256:
            raise ValueError('qemu-img does not match the approved runtime')
    info = subprocess.run([str(tool), 'info', '--output=json', str(disk)],
                          check=True, capture_output=True, cwd=project,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    value = json.loads(info.stdout)
    _validate_info(value)
    if value.get('snapshots'):
        raise ValueError('Maintenance copy must not contain snapshots')


def prepare(project, master, script):
    project = plain_path(project, directory=True)
    master, disk, firmware = validate_master(master)
    script = plain_path(script)
    checked_disk(project, disk)
    media = master / 'maintenance-media'
    media.mkdir(exist_ok=False)
    shutil.copyfile(script, media / 'Compact-Windows.ps1')
    # The owner invokes UAC inside the guest; this never executes guest commands from the host.
    (media / 'RUN.CMD').write_text(
        '@echo off\r\n'
        'powershell.exe -NoProfile -Command "Start-Process powershell.exe -Verb RunAs '
        '-ArgumentList \'-NoProfile -NoExit -ExecutionPolicy Bypass -File '
        '%~d0\\Compact-Windows.ps1 -ConfirmMaintenanceCopy\'"\r\n', encoding='ascii')
    return arguments(project, master, load_settings(project))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--master', required=True, help='New standalone compact-master-* copy only')
    parser.add_argument('--maintenance-script', required=True)
    parser.add_argument('--run', action='store_true', help='Explicit owner action: open the maintenance VM')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows host required')
    project = plain_path(args.project, directory=True)
    if not args.run:
        print(json.dumps(prepare(project, args.master, args.maintenance_script), ensure_ascii=False, indent=2))
        return
    master, disk, firmware = validate_master(args.master)
    checked_disk(project, disk)
    source_script = plain_path(args.maintenance_script)
    attached_script = plain_path(master / 'maintenance-media/Compact-Windows.ps1')
    if source_script.read_bytes() != attached_script.read_bytes():
        raise ValueError('Maintenance script changed; prepare a fresh copy')
    command = [str(runtime.engine_path(project)), *arguments(project, master, load_settings(project))]
    # No timeout, QMP quit, terminate, or kill: the owner shuts Windows down normally.
    raise SystemExit(subprocess.call(command, cwd=project, env=runtime.qemu_environment(project)))


if __name__ == '__main__':
    main()

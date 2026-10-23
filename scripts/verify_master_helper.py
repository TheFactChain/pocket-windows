"""Offline acceptance of a compact master: boot it twice on a throwaway overlay without network.

Each boot must report the requested Windows mode through the guest helper receipt on COM1 and then
shut down on an ACPI power button press. The master itself is only read as a backing file.
"""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_runtime as runtime
import run_compact_vm as compact
import windows_guest_settings as display
from file_share_carrier import Medium
from reference_vm import read_qmp_stream

RECEIPT_TIMEOUT = 180


def take_receipt(text, validate):
    """Return (unparsed tail, receipt). Firmware and kernel text shares COM1, so only JSON lines are checked."""
    receipt = None
    while '\n' in text:
        line, text = text.split('\n', 1)
        if line.strip().startswith('{'):
            receipt = validate(line.strip())
    return text, receipt


class Qmp:
    def __init__(self, process):
        self.process, self.inbox, self.events, self.sequence = process, queue.Queue(), deque(maxlen=256), 0
        threading.Thread(target=read_qmp_stream, args=(process.stdout, self.inbox, self.events, 1), daemon=True).start()
        if 'QMP' not in self.inbox.get(timeout=15):
            raise RuntimeError('QMP greeting missing')
        self.command('qmp_capabilities')

    def command(self, name, parameters=None):
        self.sequence += 1
        request = {'execute': name, 'id': self.sequence}
        if parameters:
            request['arguments'] = parameters
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()
        until = time.monotonic() + 15
        while time.monotonic() < until:
            value = self.inbox.get(timeout=max(.01, until - time.monotonic()))
            if value.get('eof'):
                raise RuntimeError('QMP disconnected')
            if value.get('id') == self.sequence:
                if 'error' in value:
                    raise RuntimeError(str(value['error']))
                return value.get('return')
        raise TimeoutError(name)


def guest_arguments(project, operation, settings, settings_folder):
    args = compact.arguments(project, operation, 'verification')
    args[args.index('-serial') + 1] = 'chardev:wcdisplay'
    args[args.index('-global') + 1] = 'VGA.xres=' + str(settings['width'])
    args[args.index('VGA.yres=1080')] = 'VGA.yres=' + str(settings['height'])
    return [*args, *display.media_arguments(compact.qemu_path(settings_folder)),
            '-chardev', 'ringbuf,id=wcdisplay,size=8192', '-chardev', 'ringbuf,id=wcfilesreceipt,size=32768',
            '-device', 'isa-serial,index=1,chardev=wcfilesreceipt']


def boot(project, operation, settings, helper_bytes, index, idle, shutdown_timeout):
    folder = operation / ('boot-' + str(index))
    settings_folder = folder / 'settings'
    settings_folder.mkdir(parents=True)
    nonce = uuid.uuid4().hex
    display.write_settings(settings_folder, settings, nonce)
    medium = Medium({'schema': 1, 'session_nonce': nonce, 'username': 'WCP_test',
                     'password': secrets.token_urlsafe(32), 'shares': []}, helper_bytes)
    engine = project / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
    result = {'boot': index, 'requested': settings, 'network': 'none', 'display_receipt': None, 'shutdown_confirmed': False}
    process = None
    try:
        with medium.hold() as file_args, (folder / 'stderr.txt').open('w') as errors:
            process = subprocess.Popen([str(engine), *guest_arguments(project, operation, settings, settings_folder), *file_args],
                cwd=project, env=runtime.qemu_environment(project), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=errors, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            qmp = Qmp(process)
            validate = lambda line: display.validate_receipt(line, settings, nonce)
            pending, started = '', time.monotonic()
            while result['display_receipt'] is None and process.poll() is None and time.monotonic() - started < RECEIPT_TIMEOUT:
                pending, result['display_receipt'] = take_receipt(pending + (qmp.command(
                    'ringbuf-read', {'device': 'wcdisplay', 'size': 4096, 'format': 'utf8'}) or ''), validate)
                time.sleep(1)
            qmp.command('screendump', {'filename': compact.qemu_path(folder / 'desktop.ppm')})
            with (folder / 'desktop.ppm').open('rb') as frame:
                result['frame_header'] = frame.read(64).decode('ascii', errors='replace').split('\n')[:3]
            if not (result['display_receipt'] or {}).get('success'):
                raise RuntimeError('Guest did not confirm the selected mode')
            if result['frame_header'] != ['P6', str(settings['width']) + ' ' + str(settings['height']), '255']:
                raise RuntimeError('Framebuffer dimensions differ from the selected Windows mode')
            # Windows ignores the power button while logon is still finishing, so let the desktop settle first.
            time.sleep(idle)
            qmp.command('system_powerdown')
            try:
                result['exit_code'] = process.wait(timeout=shutdown_timeout)
            except subprocess.TimeoutExpired:
                qmp.command('screendump', {'filename': compact.qemu_path(folder / 'shutdown-hung.ppm')})
                process.kill()  # throwaway overlay: cutting power cannot damage the master
                process.wait()
                raise RuntimeError('Guest did not shut down within ' + str(shutdown_timeout) + ' s')
            result['events'] = list(qmp.events)
            result['shutdown_confirmed'] = compact.shutdown_confirmed(qmp.events, result['exit_code'])
            if not result['shutdown_confirmed']:
                raise RuntimeError('Guest shutdown was not confirmed')
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if process is None or process.poll() is not None:
            medium.cleanup(guest_stopped=True)
        runtime.atomic_json(folder / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='project with the prepared runtime and helper')
    parser.add_argument('--master', type=Path, required=True, help='compact-master folder with candidate.qcow2 and vars.fd')
    parser.add_argument('--output', type=Path, required=True, help='new folder for the evidence and the overlay')
    parser.add_argument('--helper-sha256', required=True, help='SHA256 of the HostFilesGuest.exe installed in the master')
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--idle', type=int, default=45, help='seconds on the desktop before the power button')
    parser.add_argument('--shutdown-timeout', type=int, default=240)
    args = parser.parse_args(argv)
    project, operation = args.source.resolve(), args.output.resolve()
    settings = display.validate_settings({'width': args.width, 'height': args.height})
    helper_bytes = (project / 'runtime/host-files/HostFilesGuest.exe').read_bytes()
    if hashlib.sha256(helper_bytes).hexdigest() != args.helper_sha256.lower():
        raise ValueError('The helper in the runtime is not the one installed in the master')
    engine = project / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
    with engine.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != compact.ENGINE_SHA256:
            raise ValueError('Unexpected QEMU engine')
    operation.mkdir(parents=True)
    disk, firmware = operation / 'verification.qcow2', operation / 'verification-vars.fd'
    subprocess.run([str(project / 'runtime/qemu-11.1.0/qemu-img.exe'), 'create', '-f', 'qcow2', '-F', 'qcow2', '-b',
                    compact.qemu_path(args.master / 'candidate.qcow2'), compact.qemu_path(disk)],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL)
    shutil.copyfile(args.master / 'vars.fd', firmware)
    results = [boot(project, operation, settings, helper_bytes, index, args.idle, args.shutdown_timeout) for index in (1, 2)]
    runtime.atomic_json(operation / 'ACCEPTANCE.json', {'passed': True, 'master': str(args.master), 'results': results})
    for name in ('verification.qcow2', 'verification-vars.fd'):
        (operation / name).unlink()
    for frame in operation.glob('boot-*/*.ppm'):
        frame.unlink()
    print(json.dumps({'passed': True, 'acceptance': str(operation / 'ACCEPTANCE.json')}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

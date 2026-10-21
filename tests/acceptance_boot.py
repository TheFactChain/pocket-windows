"""Explicit offline boot/ACPI acceptance on a disposable overlay, never the source."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import struct
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from file_snapshot import locked_read, QEMU_IMG_SHA256
import launch_runtime as runtime
from portable_app import ENGINE_SHA256, host_facts
from runtime_config import load_settings


def run_cold_generations(command, report, save, cold_restarts, run):
    if type(cold_restarts) is not int or not 0 <= cold_restarts <= 3:
        raise ValueError('Cold restart limit must be between 0 and 3')
    if '-no-reboot' not in command:
        raise ValueError('Cold restarts require QEMU -no-reboot')
    for index in range(1, cold_restarts + 2):
        generation = run(list(command), index)
        if not report['generations'] or report['generations'][-1] is not generation:
            report['generations'].append(generation)
        save()
        if type(generation.get('exit_code')) is not int or generation['exit_code'] != 0 or generation.get('process_gone') is not True:
            raise RuntimeError('Guest exit and native process disappearance were not confirmed')
        shutdowns = [event.get('data') for event in generation.get('events', []) if event.get('event') == 'SHUTDOWN']
        if generation.get('outcome') == 'shutdown' and shutdowns and all(
                data == {'guest': True, 'reason': 'guest-shutdown'} for data in shutdowns):
            return generation
        if generation.get('outcome') != 'reset' or not shutdowns or not all(
                data == {'guest': True, 'reason': 'guest-reset'} for data in shutdowns):
            raise RuntimeError('No verified guest-requested reset; cold restart refused')
        if index > cold_restarts:
            report['restart_requested'] = True
            raise RuntimeError('Guest reset exceeded the bounded cold restart allowance')
        report['phase'] = 'cold_restarting'
        save()


def run_generation(process, generation, report, save, boot_seconds, *, receive_message=None, directory=None):
    messages = queue.Queue()
    if receive_message is None:
        def read_messages():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    messages.put({'invalid_qmp': True})
            messages.put({'eof': True})
        threading.Thread(target=read_messages, daemon=True).start()
        receive_message = lambda timeout: messages.get(timeout=timeout)
    sequence, eof = 0, False
    def receive(timeout):
        nonlocal eof
        value = receive_message(timeout)
        if value.get('eof'):
            eof = True
        if 'event' in value:
            generation['events'].append(value)
            report['events'].append(value)
            save()
        return value
    def qmp(name):
        nonlocal sequence
        sequence += 1
        process.stdin.write(json.dumps({'execute': name, 'id': sequence}) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = receive(max(.01, deadline - time.monotonic()))
            if value.get('id') == sequence:
                if 'error' in value:
                    raise RuntimeError('QMP operation failed')
                return value.get('return')
            if value.get('eof'):
                raise RuntimeError('QEMU exited before the command result')
        raise TimeoutError(name)
    def exit_proof():
        generation['exit_code'] = process.wait(timeout=10)
        generation['process_gone'] = not runtime.same_identity(
            generation['process'], runtime.process_identity(generation['process']['pid']))
        if generation['exit_code'] != 0 or not generation['process_gone']:
            raise RuntimeError('Guest process did not exit cleanly')
    generation['boot_seconds'] = boot_seconds
    try:
        assert 'QMP' in receive(20), 'QMP greeting missing'
        qmp('qmp_capabilities')
        assert qmp('query-status')['running']
        report['phase'] = 'waiting_for_guest_boot'
        generation['boot_deadline_monotonic'] = time.monotonic() + boot_seconds
        save()
        while time.monotonic() < generation['boot_deadline_monotonic']:
            try:
                value = receive(min(1, max(.01, generation['boot_deadline_monotonic'] - time.monotonic())))
                assert not value.get('eof'), 'QMP closed during boot'
            except queue.Empty:
                assert process.poll() is None, 'Guest exited during boot'
        report['block_stats'] = qmp('query-blockstats')
        report['phase'] = 'waiting_for_guest_acpi_shutdown'
        save()
        qmp('system_powerdown')
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline and not eof:
            try:
                receive(1)
            except queue.Empty:
                pass
        assert any(event.get('event') == 'SHUTDOWN' and event.get('data') ==
                   {'guest': True, 'reason': 'guest-shutdown'} for event in generation['events']), 'Guest shutdown not confirmed'
        exit_proof()
        generation['outcome'] = 'shutdown'
        return generation
    except BaseException as error:
        if process.poll() is not None and not eof:
            deadline = time.monotonic() + 10
            while not eof and time.monotonic() < deadline:
                try:
                    receive(max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
        reset = any(event.get('event') == 'SHUTDOWN' and event.get('data') ==
                    {'guest': True, 'reason': 'guest-reset'} for event in generation['events'])
        if reset and not isinstance(error, (KeyboardInterrupt, SystemExit)):
            exit_proof()
            generation['outcome'] = 'reset'
            return generation
        generation.update(outcome='failed', error=str(error))
        report.update(phase='failed', error=str(error), process_still_running=process.poll() is None)
        save()
        # Retain QMP and source locks; never force-stop a test Windows.
        if directory is not None and process.poll() is None:
            control_path = directory / 'control.json'
            report['control_file'] = str(control_path.relative_to(ROOT))
            print(json.dumps({'retained_pid': process.pid, 'report': str(directory / 'boot-result.json')}), flush=True)
            save()
            while process.poll() is None:
                try:
                    if not control_path.exists():
                        time.sleep(.5)
                        continue
                    assert not control_path.is_symlink() and control_path.stat().st_size < 1024
                    control = json.loads(control_path.read_text(encoding='utf-8'))
                    control_path.unlink()
                    if control == {'action': 'powerdown'}:
                        qmp('system_powerdown')
                except (ValueError, OSError, queue.Empty, TimeoutError):
                    time.sleep(1)
        raise
    finally:
        generation['finished_utc'] = datetime.now(timezone.utc).isoformat()
        generation['process_still_running'] = process.poll() is None
        report['exit_code'] = process.poll()
        save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--boot-seconds", type=int, default=150)
    parser.add_argument("--backing-evidence")
    parser.add_argument("--exit-on-reboot", action="store_true")
    parser.add_argument("--cold-restarts", type=int, default=0)
    args = parser.parse_args()
    assert 0 <= args.cold_restarts <= 3, "Cold restart limit must be 0..3"
    assert not (args.cold_restarts and args.backing_evidence is not None), "Cold mode requires a fresh direct candidate overlay"
    source = (ROOT / args.source).resolve(strict=True)
    directory = (ROOT / args.evidence).resolve()
    assert source.is_relative_to(ROOT / "images"), "Only a project image is allowed"
    assert directory.is_relative_to(ROOT / "logs"), "Evidence must stay in this project"
    assert not directory.exists(), "A fresh evidence directory is required"
    assert 90 <= args.boot_seconds <= 300
    assert not runtime.engine_processes(ROOT), "A project QEMU is already running"
    settings = load_settings(ROOT)
    facts = host_facts()
    assert facts["whpx"], "WHPX is unavailable in this execution context"
    assert facts["ram_available_mib"] >= settings["work_memory_mib"] + 1024
    assert shutil.disk_usage(ROOT).free >= 12 * 1024 ** 3
    engine = runtime.engine_path(ROOT)
    image_tool = engine.with_name("qemu-img.exe")
    directory.mkdir()
    report_path = directory / "boot-result.json"
    report = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "source": str(source.relative_to(ROOT)), "network": "none",
              "source_write_denied": True, "host": facts, "passed": False,
              "desktop_or_applications_verified": False, "events": [], "generations": []}

    def save():
        runtime.atomic_json(report_path, report)

    def rel(path):
        return path.relative_to(ROOT).as_posix()

    with ExitStack() as stack:
        for path, expected in ((engine, ENGINE_SHA256), (image_tool, QEMU_IMG_SHA256)):
            reader = stack.enter_context(locked_read(path))
            assert hashlib.file_digest(reader, "sha256").hexdigest() == expected, "Verified QEMU component changed"
        stack.enter_context(locked_read(source))
        firmware = ROOT / "state/workvm-vars.fd"
        backing = source
        if args.backing_evidence:
            prior = (ROOT / args.backing_evidence).resolve(strict=True)
            assert prior.is_relative_to(ROOT / "logs"), "Only owned test evidence is allowed"
            previous = json.loads((prior / "boot-result.json").read_text(encoding="utf-8"))
            assert (ROOT / previous["source"]).resolve(strict=True) == source
            backing = prior / "work-test.qcow2"
            current, seen = backing, set()
            while current != source:
                assert current not in seen and len(seen) < 8, "Unexpected test backing chain"
                assert current.is_relative_to(ROOT / "logs") and current.name == "work-test.qcow2"
                seen.add(current)
                backing_lock = stack.enter_context(locked_read(current))
                magic, version, offset, length = struct.unpack(">IIQI", backing_lock.read(20))
                assert magic == 0x514649FB and version in (2, 3) and 0 < length <= 32768
                assert 20 <= offset < current.stat().st_size - length
                backing_lock.seek(offset)
                encoded = backing_lock.read(length)
                try:
                    name = encoded.decode("utf-8")
                except UnicodeDecodeError:
                    name = encoded.decode("mbcs")
                current = (current.parent / name).resolve(strict=True)
            firmware = prior / "work-test-vars.fd"
            report["backing_evidence"] = str(prior.relative_to(ROOT))
        firmware_lock = stack.enter_context(locked_read(firmware))
        before = source.stat()
        info = json.loads(subprocess.check_output(
            [str(image_tool), "info", "--output=json", str(source)], cwd=ROOT))
        assert not info.get("backing-filename") and not info.get("dirty-flag")
        overlay = directory / "work-test.qcow2"
        subprocess.run([str(image_tool), "create", "-f", "qcow2", "-F", "qcow2",
                        "-b", str(backing), str(overlay)], cwd=ROOT, check=True,
                       capture_output=True)
        variables = directory / "work-test-vars.fd"
        with variables.open("xb") as output:
            shutil.copyfileobj(firmware_lock, output)
        command = [str(engine), "-name", "WindowsClaude-offline-acceptance",
                   "-machine", "q35", "-accel", "whpx", "-cpu", "Westmere",
                   "-smp", str(settings["vcpus"]), "-m", str(settings["work_memory_mib"]),
                   "-L", "runtime/qemu-11.1.0/share", "-nic", "none",
                   "-display", "none", "-vga", "std", "-serial", "none",
                   "-parallel", "none", "-monitor", "none", "-qmp", "stdio",
                   "-drive", "if=pflash,unit=0,format=raw,readonly=on,file=runtime/qemu-11.1.0/share/edk2-x86_64-code.fd",
                   "-drive", "if=pflash,unit=1,format=raw,file=" + rel(variables),
                   "-drive", "if=ide,index=0,format=qcow2,file=" + rel(overlay),
                   "-boot", "order=c,strict=on"]
        command.append("-no-reboot")
        report["command"] = command
        def run(command, index):
            with (directory / "qemu-stderr.log").open("a", encoding="utf-8") as stderr:
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW)
                generation = {"index": index, "started_utc": datetime.now(timezone.utc).isoformat(),
                              "command": command, "process": runtime.process_identity(process.pid), "events": []}
                assert generation["process"], "Cannot confirm native test QEMU identity"
                report["process"] = generation["process"]
                report["generations"].append(generation)
                save()
                try:
                    return run_generation(process, generation, report, save, args.boot_seconds, directory=directory)
                finally:
                    if process.poll() is not None:
                        process.stdin.close()
                        process.stdout.close()
        try:
            run_cold_generations(command, report, save, args.cold_restarts, run)
            after = source.stat()
            assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            report.update(passed=True, phase="completed", overlay_bytes=overlay.stat().st_size,
                          source_metadata_unchanged=True)
        except BaseException as error:
            report.update(passed=False, phase="failed", error=str(error))
            raise
        finally:
            after = source.stat()
            report["source_metadata_unchanged"] = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            report["process_still_running"] = any(generation.get("process_still_running", True)
                                                  for generation in report["generations"])
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            save()
    print(json.dumps({"passed": report["passed"], "report": str(report_path),
                      "overlay_bytes": report.get("overlay_bytes")}), flush=True)


if __name__ == "__main__":
    main()

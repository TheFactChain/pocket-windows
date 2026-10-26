"""Build-only sources for a clean gateway; never reads a user Windows disk."""
import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from file_snapshot import locked_read, QEMU_IMG_SHA256
from profile_store import _pinned_directory, _plain
import launch_runtime as runtime

GATEWAY_VERSION = "2026.10.26.2"
DELIVERY_NAMES = ("install.sh", "sing-box", "session.py", "gateway_profile.py",
                  "connection_profile.py", "wc-session.service")
DEBIAN_NAME = "debian-13.7.0-amd64-netinst.iso"
DEBIAN_SHA512 = (
    "ef04d0276850d70e4aaec0b55003628660e9631b26e35215a70ab3c8c707294a4b127"
    "31bc73c75a932902f703a7c278310ca99df28d6b36fb06d5618d8d0f678")
SINGBOX_NAME = "sing-box-1.14.3-linux-amd64.tar.gz"
SINGBOX_SHA256 = "e2bdf179c15a3652955dc44867e33ca0965c9c9b91b34267dcc5a5d639a5feee"
DEBIAN_KEYS = {
    "DF9B9C49EAA9298432589D76DA87E80D6294BE9B",
    "10460DAD76165AD81FBC0CE9988021A964E6EA7D",
}


def _hash(path, algorithm):
    with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
        _plain(path)
        return hashlib.file_digest(stream, algorithm).hexdigest()


@contextmanager
def _atomic_output(destination):
    destination = Path(destination)
    with _pinned_directory(destination.parent, create=False):
        _plain(destination)
        temporary = destination.with_name(destination.name + '.part-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as output:
                yield output
                output.flush()
                os.fsync(output.fileno())
            _plain(temporary)
            _plain(destination)
            temporary.replace(destination)
        finally:
            _plain(temporary)
            temporary.unlink(missing_ok=True)


def _write_bytes(destination, payload):
    with _atomic_output(destination) as output:
        output.write(payload)


def _download(url, destination, max_bytes, digest=None, algorithm="sha256"):
    if not url.startswith("https://"):
        raise ValueError("Build sources require HTTPS")
    _plain(destination)
    if destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError("A plain, existing build directory is required")
    count = 0
    hasher = hashlib.new(algorithm)
    with _atomic_output(destination) as output:
        request = urllib.request.Request(url, headers={"User-Agent": "WindowsClaudePortable-build"})
        with urllib.request.urlopen(request, timeout=60) as response:
            if hasattr(response, "geturl") and not response.geturl().startswith("https://"):
                raise ValueError("Insecure download redirect")
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > max_bytes:
                    raise ValueError("Build source exceeds the expected size limit")
                output.write(chunk)
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if not count or (digest is not None and actual != digest):
            raise ValueError("Build source checksum mismatch")
    return {"url": url, "bytes": count, "algorithm": algorithm, "digest": actual}


def fetch_verified(url, destination, digest, algorithm, max_bytes):
    destination = Path(destination)
    _plain(destination)
    if (destination.is_file() and not destination.is_symlink()
            and destination.stat().st_size <= max_bytes and _hash(destination, algorithm) == digest):
        return {"url": url, "bytes": destination.stat().st_size, "algorithm": algorithm, "digest": digest}
    return _download(url, destination, max_bytes, digest, algorithm)


def require_debian_signature(status, returncode):
    bad = ("BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG")
    valid = False
    for line in status.splitlines():
        words = line.split()
        if len(words) < 2 or words[0] != "[GNUPG:]":
            continue
        if words[1] in bad:
            raise ValueError("Debian signature is invalid or expired")
        if words[1] == "VALIDSIG" and len(words) >= 10:
            valid |= words[2] in DEBIAN_KEYS or words[-1] in DEBIAN_KEYS
    if returncode != 0 or not valid:
        raise ValueError("No valid signature from a pinned Debian CD signing key")


def prepare_sources(folder):
    folder = Path(folder).absolute()
    if folder != ROOT / "state/gateway-build":
        raise ValueError("Use the owned state/gateway-build directory")
    with _pinned_directory(folder):
        return _prepare_sources(folder)


def _prepare_sources(folder):
    with ExitStack() as stack:
        for directory in (folder, folder / 'sources', folder / 'keys', folder / 'boot', folder / 'boot/install.amd'):
            stack.enter_context(_pinned_directory(directory))
        outputs = [folder / 'sing-box', folder / 'source-manifest.json',
                   folder / 'sources/debian-signature-status.txt',
                   *[folder / 'keys' / ('debian-' + key + '.gpg') for key in ('DA87E80D6294BE9B', '988021A964E6EA7D')],
                   *[folder / 'boot/install.amd' / name for name in ('vmlinuz', 'initrd.gz')]]
        for path in outputs:
            _plain(path)
        return _prepare_pinned_sources(folder, stack)


def _prepare_pinned_sources(folder, stack):
    if shutil.disk_usage(folder).free < 8 * 1024**3:
        raise ValueError("At least 8 GiB of free build space is required")
    source = folder / "sources"
    gpg = Path("C:/Program Files/Git/usr/bin/gpg.exe")
    gpgv = gpg.with_name("gpgv.exe")
    if not gpg.is_file() or not gpgv.is_file():
        raise RuntimeError("The existing GnuPG verifier is unavailable")
    key_directory = folder / "keys"
    base = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/"
    for name in ("SHA512SUMS", "SHA512SUMS.sign"):
        _download(base + name, source / name, 1024 * 1024)
        stack.enter_context(locked_read(source / name))
    # Git's GnuPG interprets backslash absolute paths as relative POSIX paths.
    common = [str(gpg), "--no-options", "--batch", "--yes", "--no-tty", "--homedir", "keys"]
    keyrings = []
    for key_id in ("DA87E80D6294BE9B", "988021A964E6EA7D"):
        key = source / ("key-" + key_id + ".txt")
        _download("https://www.debian.org/CD/" + key.name, key, 1024 * 1024)
        keyring = "keys/debian-" + key_id + ".gpg"
        # gpgv verifies public signatures without an agent or a user's trust database.
        with locked_read(key) as reader:
            imported = subprocess.run(common + ["--dearmor"], input=reader.read(),
                                      cwd=folder, capture_output=True, timeout=30)
        if imported.returncode:
            raise RuntimeError("Could not import the official public Debian signing key: "
                               + imported.stderr.decode("utf-8", errors="replace")[-1600:])
        _write_bytes(folder / keyring, imported.stdout)
        stack.enter_context(locked_read(folder / keyring))
        keyrings.extend(["--keyring", "./" + keyring])
    verified = subprocess.run([str(gpgv), "--homedir", "keys", *keyrings, "--status-fd", "1",
                              "sources/SHA512SUMS.sign", "sources/SHA512SUMS"],
                              cwd=folder, capture_output=True, timeout=30)
    status = verified.stdout.decode("utf-8", errors="replace")
    require_debian_signature(status, verified.returncode)
    _write_bytes(source / "debian-signature-status.txt", status.encode('utf-8'))
    entries = dict((line.split()[1].lstrip("*"), line.split()[0])
                   for line in (source / "SHA512SUMS").read_text().splitlines() if line.strip())
    if entries.get(DEBIAN_NAME) != DEBIAN_SHA512:
        raise ValueError("The signed Debian source differs from the approved version")
    sources = {
        "debian": fetch_verified(base + DEBIAN_NAME, source / DEBIAN_NAME,
                                 DEBIAN_SHA512, "sha512", 1024**3),
        "sing_box": fetch_verified(
            "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/" + SINGBOX_NAME,
            source / SINGBOX_NAME, SINGBOX_SHA256, "sha256", 64 * 1024**2),
    }
    for name, algorithm, expected in ((DEBIAN_NAME, 'sha512', DEBIAN_SHA512),
                                      (SINGBOX_NAME, 'sha256', SINGBOX_SHA256)):
        reader = stack.enter_context(locked_read(source / name))
        _plain(source / name)
        if hashlib.file_digest(reader, algorithm).hexdigest() != expected:
            raise ValueError('A verified build source has changed')
    boot = folder / "boot"
    tar = Path("C:/Windows/System32/tar.exe")
    for name in ("vmlinuz", "initrd.gz"):
        path = boot / "install.amd" / name
        # stdout extraction cannot overwrite an existing link or archive-controlled directory.
        with locked_read(source / DEBIAN_NAME):
            extraction = subprocess.run([str(tar), '-xOf', str(source / DEBIAN_NAME), 'install.amd/' + name],
                                        capture_output=True, timeout=60)
        if extraction.returncode or not 0 < len(extraction.stdout) <= 128 * 1024**2:
            raise RuntimeError("Could not extract the verified Debian kernel and installer")
        _write_bytes(path, extraction.stdout)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Verified installer boot file is missing")
        sources[name] = {"bytes": path.stat().st_size, "sha256": _hash(path, "sha256")}
    binary = folder / "sing-box"
    with locked_read(source / SINGBOX_NAME) as held, tarfile.open(fileobj=held, mode="r:gz") as archive:
        member = archive.getmember("sing-box-1.14.3-linux-amd64/sing-box")
        if not member.isreg() or not 0 < member.size <= 128 * 1024**2:
            raise ValueError("Unexpected sing-box archive entry")
        with archive.extractfile(member) as reader, _atomic_output(binary) as output:
            shutil.copyfileobj(reader, output, 1024 * 1024)
    sources["sing_box_binary"] = {"bytes": binary.stat().st_size, "sha256": _hash(binary, "sha256")}
    manifest = {"schema": 1, "debian": "13.7.0", "sing_box": "1.14.3",
                "debian_signature_verified": True, "sources": sources}
    _write_bytes(folder / "source-manifest.json", json.dumps(manifest, indent=2).encode('utf-8'))
    return manifest


def write_delivery(files, destination):
    if set(files) != set(DELIVERY_NAMES):
        raise ValueError("Only the explicit public runtime delivery is allowed")
    _plain(destination)
    with ExitStack() as stack:
        held = {name: stack.enter_context(locked_read(path)) for name, path in files.items()}
        sums = "".join(hashlib.file_digest(stream, "sha256").hexdigest() + "  " + name + "\n"
                       for name, stream in held.items()).encode("ascii")
        with destination.open("xb") as output, tarfile.open(fileobj=output, mode="w") as archive:
            for name, stream in held.items():
                stream.seek(0)
                entry = tarfile.TarInfo(name)
                entry.size = files[name].stat().st_size
                entry.mode = 0o755 if name in ("install.sh", "sing-box", "session.py") else 0o644
                archive.addfile(entry, stream)
            entry = tarfile.TarInfo("SHA256SUMS")
            entry.size, entry.mode = len(sums), 0o644
            archive.addfile(entry, io.BytesIO(sums))
    return {"bytes": destination.stat().st_size, "sha256": _hash(destination, "sha256")}


def install_command(operation):
    operation = Path(operation).absolute()
    if (operation.parent != ROOT / "state/gateway-build"
            or not operation.name.startswith("install-")
            or not all(c.isascii() and (c.isalnum() or c == "-") for c in operation.name)):
        raise ValueError("Use a fresh owned gateway build operation")
    base = "state/gateway-build/"
    relative = base + operation.name + "/"
    return [str(runtime.engine_path(ROOT)), "-name", "WindowsClaude-gateway-build",
            "-machine", "pc", "-accel", "whpx", "-cpu", "Westmere", "-smp", "2", "-m", "1024",
            "-L", "runtime/qemu-11.1.0/share", "-display", "none", "-vga", "none",
            "-monitor", "none", "-qmp", "stdio", "-parallel", "none",
            "-serial", "file:" + relative + "installer-serial.log",
            "-serial", "file:" + relative + "installer-syslog.log",
            "-drive", "if=none,id=wcroot,format=qcow2,file=" + relative + "gateway.qcow2",
            "-device", "virtio-blk-pci,drive=wcroot,serial=WCROOT,bootindex=1",
            "-drive", "if=none,id=wcdelivery,format=raw,readonly=on,file=" + relative + "delivery.tar",
            "-device", "virtio-blk-pci,drive=wcdelivery,serial=WCDELIVERY",
            "-cdrom", base + "sources/" + DEBIAN_NAME,
            "-kernel", base + "boot/install.amd/vmlinuz",
            "-initrd", base + "boot/install.amd/initrd.gz",
            "-append", "auto=true priority=critical preseed/url=tftp://10.0.2.2/preseed.cfg "
                       "debian-installer/locale=en_US.UTF-8 keyboard-configuration/xkb-keymap=us "
                       "netcfg/get_hostname=wc-gateway netcfg/get_domain=invalid "
                       "console=ttyS0,115200n8 ipv6.disable=1 --- quiet",
            "-netdev", "user,id=buildnet,ipv6=off,tftp=" + relative + "tftp",
            "-device", "virtio-net-pci,netdev=buildnet,mac=52:54:00:77:10:01",
            "-boot", "order=c,strict=on", "-no-reboot"]


def install_gateway():
    from portable_app import host_facts, ENGINE_SHA256
    folder = ROOT / "state/gateway-build"
    with _pinned_directory(folder, create=False), runtime.ProjectLock(ROOT) as acquired, ExitStack() as inputs:
        if not acquired or runtime.engine_processes(ROOT):
            raise RuntimeError("Stop the portable virtual machines before a gateway build")
        host = host_facts()
        if not host["whpx"] or host["ram_available_mib"] < 2048:
            raise RuntimeError("Gateway build requires WHPX and 2048 MiB of available memory")
        if shutil.disk_usage(folder).free < 8 * 1024**3:
            raise RuntimeError("Gateway build requires 8 GiB of free space")
        for directory in (folder / 'sources', folder / 'boot', folder / 'boot/install.amd', runtime.engine_path(ROOT).parent):
            inputs.enter_context(_pinned_directory(directory, create=False))
        manifest_path = folder / 'source-manifest.json'
        _plain(manifest_path)
        manifest_stream = inputs.enter_context(locked_read(manifest_path))
        sources = json.load(manifest_stream)
        if not sources.get("debian_signature_verified"):
            raise ValueError("Prepare and verify the official sources first")
        for path, digest, algorithm in (
                (folder / "sources" / DEBIAN_NAME, DEBIAN_SHA512, "sha512"),
                (folder / "sing-box", sources["sources"]["sing_box_binary"]["sha256"], "sha256"),
                (folder / "boot/install.amd/vmlinuz", sources['sources']['vmlinuz']['sha256'], 'sha256'),
                (folder / "boot/install.amd/initrd.gz", sources['sources']['initrd.gz']['sha256'], 'sha256'),
                (runtime.engine_path(ROOT), ENGINE_SHA256, "sha256"),
                (runtime.engine_path(ROOT).with_name("qemu-img.exe"), QEMU_IMG_SHA256, "sha256")):
            _plain(path)
            reader = inputs.enter_context(locked_read(path))
            _plain(path)
            if hashlib.file_digest(reader, algorithm).hexdigest() != digest:
                raise ValueError("A verified build component has changed")
        files = {name: ROOT / path for name, path in {
            "install.sh": "gateway/build/install.sh",
            "session.py": "gateway/runtime/session.py",
            "wc-session.service": "gateway/runtime/wc-session.service",
            "gateway_profile.py": "scripts/gateway_profile.py",
            "connection_profile.py": "scripts/connection_profile.py"}.items()}
        files["sing-box"] = folder / "sing-box"
        for path in files.values():
            _plain(path)
            if not path.is_file():
                raise FileNotFoundError("The public gateway runtime is incomplete: " + path.name)
        operation = folder / ("install-" + uuid.uuid4().hex)
        operation.mkdir()
        with _pinned_directory(operation, create=False):
            delivery = write_delivery(files, operation / "delivery.tar")
            (operation / "tftp").mkdir()
            shutil.copyfile(ROOT / "gateway/build/preseed.cfg", operation / "tftp/preseed.cfg")
            disk = operation / "gateway.qcow2"
            subprocess.run([str(runtime.engine_path(ROOT).with_name("qemu-img.exe")), "create",
                            "-f", "qcow2", str(disk), "4G"], check=True, capture_output=True)
            report = {"schema": 1, "version": GATEWAY_VERSION, "started_utc": runtime.utc_now(),
                      "phase": "installing", "delivery": delivery, "accepted": False,
                      "windows_attached": False, "user_secrets_attached": False}
            def save():
                runtime.atomic_json(operation / "build-result.json", report)
            save()
            command = install_command(operation)
            with (operation / "qemu-stderr.log").open("w", encoding="utf-8") as error_log:
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=error_log, text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW)
                report.update(process=runtime.process_identity(process.pid), command=command)
                save()
                messages = queue.Queue()
                def read_messages():
                    for line in process.stdout:
                        try:
                            messages.put(json.loads(line))
                        except ValueError:
                            pass
                reader = threading.Thread(target=read_messages, daemon=True)
                reader.start()
                def send(name):
                    process.stdin.write(json.dumps({"execute": name}) + "\n")
                    process.stdin.flush()
                deadline = time.monotonic() + 1800
                next_update = time.monotonic()
                events = []
                try:
                    greeting = messages.get(timeout=20)
                    if "QMP" not in greeting:
                        raise RuntimeError("Build QMP greeting is missing")
                    send("qmp_capabilities")
                    while process.poll() is None and time.monotonic() < deadline:
                        if (operation / "cancel-request.json").exists():
                            raise RuntimeError("Gateway build cancelled; its disposable disk was not installed")
                        try:
                            value = messages.get(timeout=1)
                            if "event" in value:
                                events.append(value)
                        except queue.Empty:
                            pass
                        if time.monotonic() >= next_update:
                            serial = operation / "installer-serial.log"
                            report["serial_bytes"] = serial.stat().st_size if serial.exists() else 0
                            save()
                            print(json.dumps({"phase": "installing", "operation": operation.name,
                                              "serial_bytes": report["serial_bytes"]}), flush=True)
                            next_update = time.monotonic() + 30
                    if process.poll() is None:
                        raise TimeoutError("Gateway installation exceeded 30 minutes")
                finally:
                    if process.poll() is None:
                        # Only this newly created build disk is attached, never user data.
                        send("quit")
                        process.wait(timeout=30)
                        report["discarded_build"] = True
                    reader.join(timeout=5)
                    while not messages.empty():
                        value = messages.get_nowait()
                        if "event" in value:
                            events.append(value)
                    report.update(exit_code=process.returncode, events=events,
                                  ended_utc=runtime.utc_now())
                    save()
            serial_text = (operation / "installer-serial.log").read_text(errors="replace")
            passed = (process.returncode == 0 and not report.get("discarded_build")
                      and "WC_BUILD_COMPLETE " + GATEWAY_VERSION in serial_text
                      and any(event.get("event") == "SHUTDOWN"
                              and event.get("data", {}).get("reason") == "guest-shutdown"
                              for event in events))
            report.update(phase="installed" if passed else "failed", accepted=passed)
            if passed:
                result = subprocess.run([str(runtime.engine_path(ROOT).with_name("qemu-img.exe")),
                                         "check", "--output=json", str(disk)],
                                        capture_output=True, timeout=60)
                report["disk_check"] = json.loads(result.stdout)
                passed = result.returncode == 0
                report.update(accepted=passed, phase="installed" if passed else "failed",
                              disk_bytes=disk.stat().st_size)
            save()
            if not passed:
                raise RuntimeError("Gateway installation was not confirmed; see " + str(operation))
            return {"operation": str(operation), "phase": report["phase"], "disk_bytes": report["disk_bytes"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare-sources", action="store_true")
    actions.add_argument("--install", action="store_true")
    args = parser.parse_args()
    result = prepare_sources(ROOT / "state/gateway-build") if args.prepare_sources else install_gateway()
    print(json.dumps(result, ensure_ascii=True))

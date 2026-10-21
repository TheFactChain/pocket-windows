import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_gateway.py"
builder = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location("gateway_builder", SCRIPT)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)


class GatewaySourceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(builder, "Gateway builder is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.payload = b"verified source fixture"
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def test_downloaded_bytes_are_verified_before_becoming_a_source(self):
        target = self.folder / "source.bin"
        with mock.patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(self.payload)):
            result = builder.fetch_verified("https://example.com/source", target, self.digest, "sha256", 100)
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(result["bytes"], 23)
        self.assertEqual(result["digest"], self.digest)
        with mock.patch.object(builder.urllib.request, "urlopen", side_effect=AssertionError("Unneeded download")):
            builder.fetch_verified("https://example.com/source", target, self.digest, "sha256", 100)

    def test_bad_download_does_not_replace_a_previous_file_or_leave_a_partial(self):
        target = self.folder / "source.bin"
        target.write_bytes(b"previous source")
        for body in (b"tampered", b"x" * 101):
            with mock.patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(ValueError):
                    builder.fetch_verified("https://example.com/source", target, self.digest, "sha256", 100)
            self.assertEqual(target.read_bytes(), b"previous source")
            self.assertEqual(list(self.folder.glob("*.part-*")), [])

    def test_only_valid_signature_from_the_pinned_debian_key_is_accepted(self):
        valid = "[GNUPG:] VALIDSIG DF9B9C49EAA9298432589D76DA87E80D6294BE9B 2026-10-10 1 0 4 0 1 8 00\n"
        builder.require_debian_signature(valid, 0)
        for text, code in [(valid, 1), ("[GNUPG:] GOODSIG DA87E80D6294BE9B Debian\n", 0),
                           (valid.replace("DF9B9C49EAA9298432589D76DA87E80D6294BE9B", "0" * 40), 0),
                           (valid + "[GNUPG:] BADSIG DA87E80D6294BE9B test\n", 0)]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                builder.require_debian_signature(text, code)

    def test_hard_link_outputs_never_change_the_sentinel(self):
        sentinel = self.folder / 'sentinel'
        sentinel.write_bytes(b'unchanged')
        target = self.folder / 'sing-box'
        os.link(sentinel, target)
        with self.assertRaises(ValueError):
            with builder._atomic_output(target) as output:
                output.write(b'overwritten')
        self.assertEqual(sentinel.read_bytes(), b'unchanged')
        with mock.patch.object(builder.urllib.request, 'urlopen', side_effect=AssertionError('Network forbidden')):
            with self.assertRaises(ValueError):
                builder.fetch_verified('https://example.com', target, self.digest, 'sha256', 100)

    def test_prepare_rejects_linked_boot_keyring_status_and_manifest_outputs(self):
        for name in ('sing-box', 'source-manifest.json', 'sources/debian-signature-status.txt',
                     'keys/debian-DA87E80D6294BE9B.gpg', 'boot/install.amd/vmlinuz',
                     'boot/install.amd/initrd.gz'):
            with self.subTest(name=name):
                build = self.folder / ('build-' + str(len(list(self.folder.iterdir()))))
                for child in ('sources', 'keys', 'boot/install.amd'):
                    (build / child).mkdir(parents=True, exist_ok=True)
                sentinel = build / 'sentinel'
                sentinel.write_bytes(b'unchanged')
                os.link(sentinel, build / name)
                with mock.patch.object(builder, '_download', side_effect=AssertionError('Network forbidden')):
                    with self.assertRaises(ValueError):
                        builder._prepare_sources(build)
                self.assertEqual(sentinel.read_bytes(), b'unchanged')

    def test_junction_sources_are_rejected_before_network_or_extraction(self):
        elsewhere = self.folder / 'elsewhere'
        elsewhere.mkdir()
        sentinel = elsewhere / 'sentinel'
        sentinel.write_bytes(b'unchanged')
        build = self.folder / 'build'
        build.mkdir()
        link = build / 'sources'
        result = builder.subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(elsewhere)],
                                        capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(link))
        with mock.patch.object(builder, '_download', side_effect=AssertionError('Network forbidden')):
            with self.assertRaises(ValueError):
                builder._prepare_sources(build)
        self.assertEqual(sentinel.read_bytes(), b'unchanged')

    def test_mutated_bootstrap_is_rejected_before_process_creation(self):
        import portable_app
        folder = self.folder / 'state/gateway-build'
        boot = folder / 'boot/install.amd'
        boot.mkdir(parents=True)
        source = folder / 'sources'
        source.mkdir()
        engine = self.folder / 'qemu.exe'
        engine.write_bytes(b'engine')
        image_tool = engine.with_name('qemu-img.exe')
        image_tool.write_bytes(b'image-tool')
        (source / builder.DEBIAN_NAME).write_bytes(b'iso')
        (folder / 'sing-box').write_bytes(b'binary')
        hashes = {}
        for name in ('vmlinuz', 'initrd.gz'):
            payload = name.encode()
            (boot / name).write_bytes(payload)
            hashes[name] = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        hashes['sing_box_binary'] = {'sha256': hashlib.sha256(b'binary').hexdigest()}
        (folder / 'source-manifest.json').write_text(json.dumps({'debian_signature_verified': True, 'sources': hashes}))
        for path in (boot / 'vmlinuz', boot / 'initrd.gz', image_tool):
            with self.subTest(name=path.name):
                original = path.read_bytes()
                path.write_bytes(b'corrupt')
                with mock.patch.object(builder, 'ROOT', self.folder), \
                     mock.patch.object(builder.runtime, 'ProjectLock') as lock, \
                     mock.patch.object(builder.runtime, 'engine_processes', return_value=[]), \
                     mock.patch.object(builder.runtime, 'engine_path', return_value=engine), \
                     mock.patch.object(portable_app, 'host_facts', return_value={'whpx': True, 'ram_available_mib': 4096}), \
                     mock.patch.object(portable_app, 'ENGINE_SHA256', hashlib.sha256(b'engine').hexdigest()), \
                     mock.patch.object(builder, 'QEMU_IMG_SHA256', hashlib.sha256(b'image-tool').hexdigest()), \
                     mock.patch.object(builder, 'DEBIAN_SHA512', hashlib.sha512(b'iso').hexdigest()), \
                     mock.patch.object(builder.subprocess, 'Popen') as launch:
                    lock.return_value.__enter__.return_value = True
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        builder.install_gateway()
                    launch.assert_not_called()
                path.write_bytes(original)

    def test_build_command_can_only_attach_its_blank_disk_and_public_sources(self):
        operation = builder.ROOT / "state/gateway-build/install-test"
        command = builder.install_command(operation)
        joined = " ".join(command)
        self.assertNotIn("images/", joined)
        self.assertNotIn("private", joined)
        self.assertNotIn("hostfwd", joined)
        self.assertNotIn("smb=", joined)
        self.assertIn("serial=WCDELIVERY", joined)
        self.assertIn("readonly=on", joined)
        self.assertEqual(command[command.index("-display") + 1], "none")
        with self.assertRaises(ValueError):
            builder.install_command(builder.ROOT / "images")

    def test_public_delivery_has_explicit_members_and_checksums(self):
        files = {}
        for name in builder.DELIVERY_NAMES:
            path = self.folder / name
            path.write_bytes(("fixture-" + name).encode())
            files[name] = path
        archive = self.folder / "delivery.tar"
        result = builder.write_delivery(files, archive)
        self.assertEqual(result["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        with builder.tarfile.open(archive) as tar:
            self.assertEqual(set(tar.getnames()), set(builder.DELIVERY_NAMES) | {"SHA256SUMS"})
            sums = tar.extractfile("SHA256SUMS").read().decode()
            for name, source in files.items():
                self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest() + "  " + name, sums)
        with self.assertRaises(FileExistsError):
            builder.write_delivery(files, archive)
        with self.assertRaises(ValueError):
            builder.write_delivery(files | {"../private.json": files["session.py"]},
                                   self.folder / "bad.tar")
        self.assertFalse((self.folder / "bad.tar").exists())


if __name__ == "__main__":
    unittest.main()

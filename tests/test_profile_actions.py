"""Exercise the real JSON CLI boundary, including secret redaction and saved state."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "scripts/portable_app.py"


class ProfileActionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="portable-profile-api-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def call(self, action, value=None, raw=None):
        process = subprocess.run(
            [sys.executable, "-X", "utf8", "-I", "-S", "-B", str(BACKEND),
             "--root", str(self.root), "--action", action],
            input=raw if raw is not None else json.dumps(value or {}).encode(),
            capture_output=True, timeout=15)
        self.assertEqual(process.stderr, b"", "The public CLI must not print raw exceptions")
        result = json.loads(process.stdout)
        self.assertIs(type(result["ok"]), bool)
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        self.assertNotIn("fixture-private-token", process.stdout.decode())
        return result

    def test_import_select_and_edit_never_claim_the_profile_was_applied(self):
        created = self.call("import-profile", {"uri": "hy2://fixture-private-token@example.com:443#Home"})
        self.assertTrue(created["ok"])
        profile = created["profile"]
        self.assertEqual(profile["state"], "saved")
        self.assertIsNone(profile["applied_revision_id"])
        self.assertFalse(profile["verified"])
        selected = self.call("select-profile", {"profile_id": profile["profile_id"]})
        self.assertTrue(selected["profile"]["selected"])
        self.assertEqual(selected["profile"]["state"], "pending")
        edited = self.call("save-profile", {
            "profile_id": profile["profile_id"],
            "profile": {"name": "New", "protocol": "hysteria2", "host": "new.example.com", "port": 443}})
        self.assertTrue(edited["ok"])
        self.assertTrue(edited["profile"]["auth_present"])
        self.assertNotEqual(profile["revision_id"], edited["profile"]["revision_id"])
        listed = self.call("list-profiles")["profiles"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["state"], "pending")
        self.assertFalse((self.root / "state/runtime-active.json").exists())

    def test_malformed_requests_preserve_the_selected_profile(self):
        result = self.call("save-profile", {"profile": {
            "protocol": "socks5", "host": "example.com", "port": 1080}})
        self.assertTrue(result["ok"])
        profile_id = result["profile"]["profile_id"]
        self.call("select-profile", {"profile_id": profile_id})
        for action, request in [
                ("import-profile", {"uri": "hy2://fixture-private-token@example.com?insecure=1"}),
                ("save-profile", {"profile_id": profile_id, "profile": {
                    "protocol": "socks5", "host": "example.com", "port": True}}),
                ("select-profile", {"profile_id": "../private"}),
                ("save-profile", {"profile": {}, "unknown": True})]:
            with self.subTest(action=action):
                self.assertFalse(self.call(action, request)["ok"])
        for raw in (b'{"profile":{},"profile":{}}', b"x" * 65537, b'{"profile":"fixture-private-token"'):
            self.assertFalse(self.call("save-profile", raw=raw)["ok"])
        current = self.call("get-profile-view", {"profile_id": profile_id})["profile"]
        self.assertEqual(current["revision_id"], result["profile"]["revision_id"])
        self.assertTrue(current["selected"])

    def test_delete_removes_the_profile_and_clears_only_its_own_selection(self):
        kept = self.call("import-profile", {"uri": "hy2://fixture-private-token@kept.example.com#Kept"})["profile"]
        old = self.call("import-profile", {"uri": "hy2://fixture-private-token@old.example.com#Old"})["profile"]
        self.call("select-profile", {"profile_id": kept["profile_id"]})
        removed = self.call("delete-profile", {"profile_id": old["profile_id"]})
        self.assertTrue(removed["ok"])
        self.assertFalse(removed["was_selected"])
        listed = self.call("list-profiles")["profiles"]
        self.assertEqual([item["profile_id"] for item in listed], [kept["profile_id"]])
        self.assertTrue(listed[0]["selected"])
        for request in ({"profile_id": old["profile_id"]}, {"profile_id": "../private"},
                        {"profile_id": kept["profile_id"], "unknown": True}, {}):
            with self.subTest(request=request):
                self.assertFalse(self.call("delete-profile", request)["ok"])
        self.assertEqual(len(self.call("list-profiles")["profiles"]), 1)
        active = self.call("delete-profile", {"profile_id": kept["profile_id"]})
        self.assertTrue(active["ok"])
        self.assertTrue(active["was_selected"])
        self.assertEqual(self.call("list-profiles")["profiles"], [])
        self.assertNotIn(kept["profile_id"], (self.root / "profiles/profiles.json").read_text())

    def test_export_is_explicit_never_overwrites_and_keeps_secrets_out_of_reply(self):
        result = self.call("import-profile", {"uri": "hy2://fixture-private-token@example.com"})
        profile_id = result["profile"]["profile_id"]
        destination = self.root / "export.json"
        exported = self.call("export-profile", {"profile_id": profile_id, "destination": str(destination)})
        self.assertTrue(exported["ok"])
        self.assertNotIn("fixture-private-token", destination.read_text())
        self.assertFalse(self.call("export-profile", {
            "profile_id": profile_id, "destination": str(destination), "include_secrets": True})["ok"])
        secret_file = self.root / "explicit-secrets.json"
        self.assertTrue(self.call("export-profile", {
            "profile_id": profile_id, "destination": str(secret_file), "include_secrets": True})["ok"])
        self.assertEqual(json.loads(secret_file.read_text())["credentials"]["auth"], "fixture-private-token")


if __name__ == "__main__":
    unittest.main()

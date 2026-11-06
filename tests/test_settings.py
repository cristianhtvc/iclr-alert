"""Configuration precedence and encrypted-session boundaries without real DPAPI."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from iclr_alert import settings as config_module
from iclr_alert.core import ProbeError


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.private = Path(directory.name)
        self.session_path = self.private / "openreview-session.json"
        for name, value in (("PRIVATE_DIR", self.private), ("SESSION_PATH", self.session_path)):
            override = patch.object(config_module, name, value)
            override.start()
            self.addCleanup(override.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def local(self, values):
        (self.private / "config.json").write_text(json.dumps(values), encoding="utf-8")

    def test_environment_secrets_override_local_and_vault_without_logging(self):
        self.local({"openreview_token": "local-token", "bark_key": "local-key", "paper": "local-paper"})
        env = {
            "OPENREVIEW_TOKEN": "private-env-token", "BARK_DEVICE_KEY": "private-env-key",
            "OPENREVIEW_PAPER": "https://openreview.net/forum?id=DemoPaper01",
            "DATA_DIR": str(self.private / "cloud-data"), "CHECK_INTERVAL_SECONDS": "180",
        }
        output = io.StringIO()
        with patch.dict(os.environ, env), patch.object(config_module, "load_session") as vault, redirect_stdout(output):
            config = config_module.settings()
        self.assertEqual(config["token"], env["OPENREVIEW_TOKEN"])
        self.assertEqual(config["bark_key"], env["BARK_DEVICE_KEY"])
        self.assertEqual(config["paper"], env["OPENREVIEW_PAPER"])
        self.assertEqual(config["interval"], 180)
        self.assertEqual(config["db_path"], Path(env["DATA_DIR"]) / "monitor.sqlite3")
        vault.assert_not_called()
        self.assertNotIn(env["OPENREVIEW_TOKEN"], output.getvalue())
        self.assertNotIn(env["BARK_DEVICE_KEY"], output.getvalue())

    def test_protected_vault_precedes_legacy_plaintext_token(self):
        self.local({"openreview_token": "local-token"})
        with patch.object(config_module, "load_session", return_value="vault-token") as vault:
            self.assertEqual(config_module.settings()["token"], "vault-token")
            self.local({})
            self.assertEqual(config_module.settings()["token"], "vault-token")
            self.assertEqual(vault.call_count, 2)
        self.local({"openreview_token": "legacy-local-token"})
        with patch.object(config_module, "load_session", return_value=""):
            self.assertEqual(config_module.settings()["token"], "legacy-local-token")

    def test_invalid_intervals_are_safe_configuration_errors(self):
        for value in ("private-invalid-number", "120.5", "59", "86401", "0"):
            with self.subTest(value=value), patch.dict(os.environ, {"CHECK_INTERVAL_SECONDS": value}):
                with self.assertRaises(ProbeError) as captured:
                    config_module.settings()
                self.assertEqual(captured.exception.code, "invalid_config")
                self.assertNotIn("private-invalid-number", str(captured.exception))
        for value in ("60", "86400"):
            with patch.dict(os.environ, {"CHECK_INTERVAL_SECONDS": value}):
                self.assertEqual(config_module.settings()["interval"], int(value))

    def test_session_file_contains_protected_bytes_instead_of_plaintext(self):
        token = "private-session-token-do-not-save-in-plaintext"
        protected = b"opaque-ciphertext-for-test"
        with patch.object(config_module, "_dpapi", return_value=protected) as encrypt:
            config_module.save_session(token)
        encrypt.assert_called_once_with(token.encode("utf-8"), protect=True)
        saved = self.session_path.read_text(encoding="utf-8")
        self.assertNotIn(token, saved)
        self.assertEqual(json.loads(saved)["protection"], "windows-dpapi-current-user")
        with patch.object(config_module, "_dpapi", return_value=token.encode("utf-8")) as decrypt:
            self.assertEqual(config_module.load_session(), token)
        decrypt.assert_called_once_with(protected, protect=False)

    def test_bad_sessions_fail_safely_without_calling_dpapi(self):
        malformed_sessions = (
            {"protected_token": "not valid base64"}, {"other": "private-value"},
            {"protected_token": 123}, ["private-value"],
        )
        for malformed in malformed_sessions:
            with self.subTest(value=malformed):
                self.session_path.write_text(json.dumps(malformed), encoding="utf-8")
                with patch.object(config_module, "_dpapi") as decrypt:
                    with self.assertRaises(ProbeError) as captured:
                        config_module.load_session()
                self.assertEqual(captured.exception.code, "invalid_session")
                self.assertNotIn("private-value", str(captured.exception))
                decrypt.assert_not_called()

    def test_local_configuration_must_be_an_object(self):
        self.local(["private-config-value"])
        with self.assertRaises(ProbeError) as captured:
            config_module.settings()
        self.assertEqual(captured.exception.code, "invalid_config")
        self.assertNotIn("private-config-value", str(captured.exception))

    def test_fresh_installation_has_no_default_paper(self):
        config = config_module.settings()
        self.assertEqual(config["paper"], "")
        with self.assertRaises(ProbeError) as captured:
            config_module.require_paper(config)
        self.assertEqual(captured.exception.code, "missing_paper")


if __name__ == "__main__":
    unittest.main()

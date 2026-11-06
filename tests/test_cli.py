"""CLI outcomes and explicit private cloud export, with no authentication or pushes."""

from contextlib import redirect_stdout
from contextlib import closing
from copy import deepcopy
import io
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from iclr_alert import __main__ as cli
from iclr_alert import monitor, runner, settings as config_module, web


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.private = Path(directory.name)
        self.config = {
            "paper": "https://openreview.net/forum?id=DemoPaper01",
            "token": "private-session-token-do-not-print",
            "bark_key": "private-bark-key-do-not-print",
            "interval": 120, "data_dir": self.private,
            "db_path": self.private / "monitor.sqlite3",
            "waiting_interval_seconds": 7200, "waiting_message": "自定义等待", "update_message": "自定义更新",
        }
        for override in (
            patch.object(config_module, "settings", return_value=self.config),
            patch.object(web, "PRIVATE_DIR", self.private),
        ):
            override.start()
            self.addCleanup(override.stop)

    def invoke(self, *arguments):
        output = io.StringIO()
        with patch.object(cli.sys, "argv", ["iclr_alert", *arguments]), redirect_stdout(output):
            code = cli.main()
        return code, output.getvalue()

    def assert_no_secrets(self, output):
        self.assertNotIn(self.config["token"], output)
        self.assertNotIn(self.config["bark_key"], output)

    def test_watch_human_action_pauses_exit_cleanly_without_crash_restart(self):
        for action in ("needs_auth", "access_verification"):
            failure = {"status": "error", "action": action, "notification_status": "none"}
            with self.subTest(action=action), patch.object(runner, "run", return_value=failure) as run:
                code, output = self.invoke("watch")
            self.assertEqual(code, 0)
            run.assert_called_once_with(watch=True)
            self.assert_no_secrets(output)

    def test_watch_with_legacy_delivered_scores_exits_zero_without_network(self):
        monitor.Monitor(self.config["db_path"])
        with closing(sqlite3.connect(self.config["db_path"])) as db, db:
            db.execute(
                "INSERT INTO paper_state VALUES(?,?,?)",
                ("DemoPaper01", json.dumps({"R1": {"id": "R1", "rating": 6}}), "2026-01-01T00:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?)",
                ("legacy-score", "DemoPaper01", "{}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
            )
        self.config["token"] = ""
        with (
            patch.object(runner, "settings", return_value=self.config),
            patch.object(monitor, "probe_openreview") as probe,
            patch.object(monitor, "send_notification") as push,
        ):
            code, output = self.invoke("watch")
        self.assertEqual(code, 0)
        probe.assert_not_called()
        push.assert_not_called()
        self.assertIn("action=completed", output)
        self.assertEqual(json.loads((self.private / "monitor-status.json").read_text())["action"], "completed")
        self.assertNotIn(self.config["bark_key"], output)

    def test_check_failure_exits_two_and_reports_safe_result(self):
        failure = {
            "status": "error", "code": "unauthorized", "message": "请重新登录。",
            "action": "needs_auth", "notification_status": "none",
        }
        with patch.object(runner, "run", return_value=deepcopy(failure)) as run:
            code, output = self.invoke("check")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output), failure)
        run.assert_called_once_with(watch=False)
        self.assert_no_secrets(output)

    def test_paper_command_saves_normalized_target_without_credentials(self):
        code, output = self.invoke("paper", "DemoPaper01")
        self.assertEqual(code, 0)
        configured = json.loads((self.private / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(configured, {"paper": "https://openreview.net/forum?id=DemoPaper01"})
        self.assertEqual(json.loads(output)["paper"], configured["paper"])
        self.assert_no_secrets(output)

    def test_unconfigured_login_stops_before_requesting_credentials(self):
        self.config["paper"] = ""
        with patch.object(cli, "require_windows_login"), patch("builtins.input") as prompt:
            code, output = self.invoke("login")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["code"], "missing_paper")
        prompt.assert_not_called()

    def test_non_windows_login_stops_before_requesting_credentials(self):
        with patch.object(cli.sys, "platform", "linux"), patch("builtins.input") as prompt:
            code, output = self.invoke("login")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["code"], "unsupported_session_store")
        prompt.assert_not_called()

    def test_explicit_bark_test_sends_one_acceptance_only_notification(self):
        from iclr_alert import core
        accepted = {"status": "accepted", "test_id": "example-test-id", "accepted_at": "2026-01-01T00:00:00+00:00"}
        with patch.object(core, "send_bark_test", return_value=accepted) as send:
            code, output = self.invoke("bark-test")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), accepted)
        self.assertNotIn("delivered", json.loads(output))
        send.assert_called_once_with(self.config["bark_key"])
        self.assert_no_secrets(output)

    def test_probe_explicit_target_works_without_default_paper(self):
        self.config["paper"] = ""
        with patch.object(cli, "probe_openreview", return_value={"status": "no_visible_reviews"}) as probe:
            code, output = self.invoke("probe", "--paper", "DemoPaper01", "--anonymous")
        self.assertEqual(code, 0)
        probe.assert_called_once_with("https://openreview.net/forum?id=DemoPaper01", "")
        self.assertEqual(json.loads(output)["status"], "no_visible_reviews")

    def test_unexpected_watch_error_still_has_nonzero_exit(self):
        with patch.object(runner, "run", return_value={"status": "error", "action": "retry"}):
            code, output = self.invoke("watch")
        self.assertEqual(code, 2)
        self.assert_no_secrets(output)

    def test_cloud_config_writes_expected_private_values_without_printing_or_uploading(self):
        code, output = self.invoke("cloud-config")
        self.assertEqual(code, 0)
        public = json.loads(output)
        self.assertEqual(public["status"], "prepared")
        self.assertIs(public["uploaded"], False)
        target = self.private / "railway-variables.json"
        self.assertEqual(Path(public["path"]), target)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {
            "OPENREVIEW_PAPER": self.config["paper"],
            "OPENREVIEW_TOKEN": self.config["token"],
            "BARK_DEVICE_KEY": self.config["bark_key"],
            "CHECK_INTERVAL_SECONDS": "120",
            "DATA_DIR": "/data",
            "WAITING_REMINDER_SECONDS": "7200",
            "WAITING_MESSAGE": "自定义等待",
            "SCORE_UPDATE_MESSAGE": "自定义更新",
        })
        self.assert_no_secrets(output)

    def test_cloud_config_missing_credentials_does_not_write_partial_export(self):
        self.config["token"] = ""
        code, output = self.invoke("cloud-config")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["code"], "missing_credentials")
        self.assertFalse((self.private / "railway-variables.json").exists())
        self.assertNotIn(self.config["bark_key"], output)

    def test_status_shows_configuration_flags_without_secret_values(self):
        (self.private / "monitor-status.json").write_text(
            json.dumps({"status": "ok", "notification_status": "none"}), encoding="utf-8"
        )
        (self.private / "auth-status.json").write_text(
            json.dumps({"status": "authenticated"}), encoding="utf-8"
        )
        code, output = self.invoke("status")
        self.assertEqual(code, 0)
        public = json.loads(output)
        self.assertTrue(public["bark_configured"])
        self.assertTrue(public["authenticated"])
        self.assertEqual(public["paper"], self.config["paper"])
        self.assertEqual(public["waiting_reminder_seconds"], 7200)
        self.assertEqual(public["monitor"]["status"], "ok")
        self.assertEqual(public["login"]["status"], "authenticated")
        self.assert_no_secrets(output)


if __name__ == "__main__":
    unittest.main()

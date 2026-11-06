"""Polling/backoff/lock behavior with fake monitor results and no actual waiting."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

from iclr_alert import runner
from iclr_alert.core import ProbeError


def result(code=None, action=None, retry_after=None):
    value = {
        "status": "error" if code else "ok", "data_changed": False,
        "notification_status": "none",
    }
    if code:
        value.update(code=code, message="safe status message")
    if action:
        value["action"] = action
    if retry_after is not None:
        value["retry_after"] = retry_after
    return value


class BackoffTests(unittest.TestCase):
    def test_backoff_and_server_retry_after_are_honored(self):
        self.assertEqual(runner.next_delay(120, 0), 120)
        self.assertEqual(runner.next_delay(120, 1), 240)
        self.assertEqual(runner.next_delay(120, 20), 1800)
        self.assertEqual(runner.next_delay(120, 1, retry_after=900), 900)
        self.assertEqual(runner.next_delay(120, 20, retry_after=3600), 3600)

    def test_long_configured_intervals_are_never_shortened_by_backoff_cap(self):
        self.assertEqual(runner.next_delay(86400, 0), 86400)
        self.assertGreaterEqual(runner.next_delay(86400, 2), 86400)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.config = {
            "paper": "https://openreview.net/forum?id=DemoPaper01",
            "token": "private-session-token", "bark_key": "private-bark-key",
            "interval": 120, "data_dir": self.data_dir,
            "db_path": self.data_dir / "monitor.sqlite3",
            "waiting_interval_seconds": 3600, "waiting_message": "等待文案", "update_message": "更新文案",
        }
        self.monitor = Mock()
        self.settings_patch = patch.object(runner, "settings", return_value=self.config)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.monitor_patch = patch.object(runner, "Monitor", return_value=self.monitor)
        self.monitor_factory = self.monitor_patch.start()
        self.addCleanup(self.monitor_patch.stop)

    def test_challenge_stops_polling_and_restart_deduplicates_health_alert(self):
        failure = result("challenge_required", "access_verification")
        self.monitor.check_once.side_effect = lambda *args, **kwargs: deepcopy(failure)
        sleep = Mock()
        output = io.StringIO()
        with patch.object(runner, "send_notification") as send, redirect_stdout(output):
            first = runner.run(watch=True, sleep=sleep)
            restarted = runner.run(watch=True, sleep=sleep)
        self.assertEqual(first["health_alert"], "accepted")
        self.assertEqual(restarted["health_alert"], "accepted")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.monitor.check_once.call_count, 2)
        sleep.assert_not_called()
        for secret in (self.config["token"], self.config["bark_key"]):
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, (self.data_dir / "monitor-status.json").read_text(encoding="utf-8"))

    def test_missing_target_stops_before_creating_monitor_or_network_requests(self):
        self.config["paper"] = ""
        with self.assertRaises(ProbeError) as captured:
            runner.run(watch=True)
        self.assertEqual(captured.exception.code, "missing_paper")
        self.monitor_factory.assert_not_called()
        self.monitor.check_once.assert_not_called()

    def test_recoverable_failure_backs_off_then_success_resets_delay(self):
        self.monitor.check_once.side_effect = [
            result("rate_limited", "retry", retry_after=300),
            result(),
            result("unauthorized", "needs_auth"),
        ]
        sleep = Mock()
        with patch.object(runner, "send_notification"), redirect_stdout(io.StringIO()):
            returned = runner.run(watch=True, sleep=sleep)
        self.assertEqual(sleep.call_args_list, [call(300), call(120)])
        self.assertEqual(self.monitor.check_once.call_count, 3)
        self.assertEqual(returned["action"], "needs_auth")

    def test_failed_health_alert_can_retry_on_restart_with_same_event_id(self):
        self.monitor.check_once.side_effect = lambda *args, **kwargs: result("unauthorized", "needs_auth")
        with patch.object(runner, "send_notification", side_effect=ProbeError("network_error", "safe")) as failed:
            with redirect_stdout(io.StringIO()):
                first = runner.run(watch=True, sleep=Mock())
        self.assertEqual(first["health_alert"], "pending")
        first_id = failed.call_args.args[1]["id"]
        with patch.object(runner, "send_notification") as retried, redirect_stdout(io.StringIO()):
            second = runner.run(watch=True, sleep=Mock())
        self.assertEqual(second["health_alert"], "accepted")
        self.assertEqual(retried.call_args.args[1]["id"], first_id)

    def test_push_failures_do_not_slow_score_polling(self):
        self.monitor.check_once.side_effect = [
            result("network_error", "retry_push"),
            result("bark_rejected", "retry_push"),
            result("unauthorized", "needs_auth"),
        ]
        sleep = Mock()
        with patch.object(runner, "send_notification"), redirect_stdout(io.StringIO()):
            returned = runner.run(watch=True, sleep=sleep)
        self.assertEqual(sleep.call_args_list, [call(120), call(120)])
        self.assertEqual(returned["action"], "needs_auth")

    def test_one_shot_checks_once_without_sleep_or_health_notification(self):
        self.monitor.check_once.return_value = result("unauthorized", "needs_auth")
        sleep = Mock()
        with patch.object(runner, "send_notification") as send, redirect_stdout(io.StringIO()):
            returned = runner.run(watch=False, sleep=sleep)
        self.assertEqual(returned["status"], "error")
        self.monitor.check_once.assert_called_once_with(
            self.config["paper"], self.config["token"], self.config["bark_key"],
            waiting_interval_seconds=3600, waiting_message="等待文案", update_message="更新文案",
        )
        sleep.assert_not_called()
        send.assert_not_called()

    def test_missing_bark_configuration_stops_before_polling(self):
        self.config["bark_key"] = ""
        with self.assertRaises(ProbeError) as captured:
            runner.run(watch=True, sleep=Mock())
        self.assertEqual(captured.exception.code, "invalid_bark_key")
        self.monitor_factory.assert_not_called()

    def test_worker_lock_excludes_second_worker_and_releases_on_exit(self):
        path = self.data_dir / "worker.lock"
        with runner.worker_lock(path):
            with self.assertRaises(ProbeError) as captured:
                with runner.worker_lock(path):
                    self.fail("A second worker must not acquire the same state directory")
            self.assertEqual(captured.exception.code, "already_running")
        with runner.worker_lock(path):
            pass


if __name__ == "__main__":
    unittest.main()

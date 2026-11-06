"""Monitor lifecycle tests with fake external reads and pushes."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from iclr_alert import core, monitor


PAPER = "https://openreview.net/forum?id=DemoPaper01"
KEY = "private-example-bark-key"


def review(review_id="R1", rating="6: Weak Accept", confidence="4: Confident"):
    return {"id": review_id, "reviewer": "Reviewer_" + review_id, "rating": rating, "confidence": confidence}


def observation(reviews):
    numeric = [core._numeric_rating(item["rating"]) for item in reviews]
    scores = [value for value in numeric if value is not None]
    return {
        "forum_id": "DemoPaper01", "title": "Example paper", "reviews": reviews,
        "review_count": len(reviews), "scored_review_count": len(scores),
        "average_rating": sum(scores) / len(scores) if scores else None,
        "status": "reviews_visible" if scores else "ratings_unavailable" if reviews else "no_visible_reviews",
        "checked_at": datetime.now(timezone.utc).isoformat(), "source_url": PAPER,
    }


class MonitorTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "monitor.sqlite3"
        self.engine = monitor.Monitor(self.db_path)

    def check(self, reviews):
        with patch.object(monitor, "probe_openreview", return_value=observation(reviews)):
            return self.engine.check_once(PAPER, "", KEY)

    def test_waiting_then_first_scores_notifies_with_actual_scores_and_link(self):
        with patch.object(monitor, "send_notification") as send:
            self.assertFalse(self.check([])["data_changed"])
            result = self.check([review()])
        self.assertTrue(result["data_changed"])
        self.assertEqual(result["notification_status"], "accepted")
        event = send.call_args.args[1]
        self.assertIn("6: Weak Accept", event["body"])
        self.assertIn("4: Confident", event["body"])
        self.assertEqual(event["url"], PAPER)
        self.assertNotIn("ttl", event)

    def test_startup_with_scores_notifies_once_and_restart_keeps_baseline(self):
        with patch.object(monitor, "send_notification") as send:
            self.assertEqual(self.check([review()])["notification_status"], "accepted")
            self.engine = monitor.Monitor(self.db_path)
            self.assertEqual(self.check([review()])["notification_status"], "none")
        self.assertEqual(send.call_count, 1)

    def test_new_review_rating_and_confidence_changes_are_separate_events(self):
        with patch.object(monitor, "send_notification") as send:
            self.check([review()])
            added = self.check([review(), review("R2", "8: Accept")])
            self.assertTrue(added["data_changed"])
            changed = self.check([review(rating="7: Accept"), review("R2", "8: Accept")])
            self.assertTrue(changed["data_changed"])
            confidence = self.check([review(rating="7: Accept", confidence="5: Very Confident"), review("R2", "8: Accept")])
            self.assertTrue(confidence["data_changed"])
        self.assertEqual(send.call_count, 4)
        self.assertIn("新增", send.call_args_list[1].args[1]["title"])
        self.assertIn("置信度", send.call_args_list[3].args[1]["title"])

    def test_reordering_and_label_formatting_do_not_duplicate_score_alerts(self):
        with patch.object(monitor, "send_notification") as send:
            self.check([review(), review("R2", "8: Accept")])
            result = self.check([review("R2", 8), review(rating=6, confidence=4)])
        self.assertFalse(result["data_changed"])
        self.assertEqual(send.call_count, 1)

    def test_empty_and_unscored_observations_do_not_reset_known_scores(self):
        with patch.object(monitor, "send_notification") as send:
            self.check([review()])
            self.assertFalse(self.check([])["data_changed"])
            self.assertFalse(self.check([review(rating=None)])["data_changed"])
            self.assertFalse(self.check([review(confidence=None)])["data_changed"])
            self.assertFalse(self.check([review()])["data_changed"])
        self.assertEqual(send.call_count, 1)

    def test_auth_or_access_failure_preserves_state_and_requires_attention(self):
        with patch.object(monitor, "send_notification") as send:
            self.check([review()])
            for code, action in (("unauthorized", "needs_auth"), ("challenge_required", "access_verification")):
                with patch.object(monitor, "probe_openreview", side_effect=core.ProbeError(code, "safe message")):
                    result = self.engine.check_once(PAPER, "private-token", KEY)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["action"], action)
                self.assertNotIn("private-token", json.dumps(result))
            self.assertFalse(self.check([review()])["data_changed"])
        self.assertEqual(send.call_count, 1)

    def test_push_failure_keeps_outbox_and_retry_reuses_event_id_after_restart(self):
        with patch.object(monitor, "send_notification", side_effect=core.ProbeError("network_error", "safe")) as failed:
            result = self.check([review()])
        self.assertEqual(result["notification_status"], "pending")
        self.assertEqual(result["pending_count"], 1)
        original_event = deepcopy(failed.call_args.args[1])
        self.engine = monitor.Monitor(self.db_path)
        with patch.object(monitor, "send_notification") as retried:
            result = self.check([review()])
            self.assertEqual(retried.call_args.args[1], original_event)
            self.assertFalse(result["data_changed"])
            self.assertEqual(result["notification_status"], "accepted")
            self.assertEqual(result["pending_count"], 0)
            self.check([review()])
        self.assertEqual(retried.call_count, 1)

    def test_snapshot_and_pending_event_are_committed_before_transmission(self):
        def inspect_before_acceptance(key, event):
            with closing(sqlite3.connect(self.db_path)) as db:
                stored = db.execute("SELECT reviews_json FROM paper_state").fetchone()[0]
                pending = db.execute("SELECT event_id,accepted_at FROM outbox").fetchone()
            self.assertIn("R1", json.loads(stored))
            self.assertEqual(pending, (event["id"], None))
        with patch.object(monitor, "send_notification", side_effect=inspect_before_acceptance):
            self.check([review()])

    def test_auth_failure_does_not_send_a_pending_event_or_clobber_it(self):
        with patch.object(monitor, "send_notification", side_effect=core.ProbeError("network_error", "safe")):
            self.check([review()])
        with (
            patch.object(monitor, "probe_openreview", side_effect=core.ProbeError("forbidden", "safe")),
            patch.object(monitor, "send_notification") as send,
        ):
            self.engine.check_once(PAPER, "", KEY)
        send.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE accepted_at IS NULL").fetchone()[0], 1)


class NotificationTransportTests(unittest.TestCase):
    def test_real_alert_payload_has_no_short_test_ttl_and_keeps_stable_id(self):
        event = {"id": "stable-event-id", "title": "评分变化", "body": "Reviewer R1:6", "url": PAPER}
        with patch.object(core, "_request_json", return_value={"code": 200}) as transport:
            monitor.send_notification(KEY, event)
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.day.app/push")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["id"], event["id"])
        self.assertEqual(payload["device_key"], KEY)
        self.assertNotIn("ttl", payload)
        self.assertNotIn(KEY, request.full_url)

    def test_rejection_is_safe_and_is_not_retried_in_the_same_check(self):
        with patch.object(core, "_request_json", return_value={"code": 400, "message": KEY}) as transport:
            with self.assertRaises(core.ProbeError) as captured:
                monitor.send_notification(KEY, {"id": "event", "body": "score"})
        self.assertNotIn(KEY, str(captured.exception))
        self.assertEqual(transport.call_count, 1)


if __name__ == "__main__":
    unittest.main()

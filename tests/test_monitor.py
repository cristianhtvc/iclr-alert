"""Monitor lifecycle tests with fake external reads and pushes."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
        self.assertEqual(result["action"], "completed")
        event = send.call_args.args[1]
        self.assertIn("6: Weak Accept", event["body"])
        self.assertIn("4: Confident", event["body"])
        self.assertEqual(event["url"], PAPER)
        self.assertNotIn("ttl", event)

    def test_startup_with_scores_completes_once_and_restart_does_not_query_api(self):
        with patch.object(monitor, "send_notification") as send:
            first = self.check([review()])
            self.assertEqual(first["notification_status"], "accepted")
            self.assertEqual(first["action"], "completed")
            self.engine = monitor.Monitor(self.db_path)
            with patch.object(monitor, "probe_openreview", side_effect=AssertionError("Completed tasks must not query OpenReview")) as probe:
                restarted = self.engine.check_once(PAPER, "expired-token", KEY)
            self.assertEqual(restarted["status"], "ok")
            self.assertEqual(restarted["notification_status"], "none")
            self.assertEqual(restarted["action"], "completed")
            probe.assert_not_called()
        self.assertEqual(send.call_count, 1)

    def test_completed_task_does_not_resume_for_later_review_changes(self):
        with patch.object(monitor, "send_notification") as send:
            self.check([review()])
            for observed in (
                [review(), review("R2", "8: Accept")],
                [review(rating="7: Accept")],
                [review(confidence="5: Very Confident")],
            ):
                with patch.object(monitor, "probe_openreview", return_value=observation(observed)) as probe:
                    result = self.engine.check_once(PAPER, "", KEY)
                self.assertFalse(result["data_changed"])
                self.assertEqual(result["action"], "completed")
                probe.assert_not_called()
        self.assertEqual(send.call_count, 1)

    def test_unscored_reviews_do_not_complete_the_task(self):
        with patch.object(monitor, "send_notification") as send:
            for observed in ([], [review(rating=None)], [review(rating="not published")]):
                result = self.check(observed)
                self.assertEqual(result["status"], "ok")
                self.assertNotEqual(result.get("action"), "completed")
                self.assertFalse(result["data_changed"])
        send.assert_not_called()

    def test_auth_or_access_failure_preserves_state_and_requires_attention(self):
        with patch.object(monitor, "send_notification") as send:
            self.check([])
            for code, action in (("unauthorized", "needs_auth"), ("challenge_required", "access_verification")):
                with patch.object(monitor, "probe_openreview", side_effect=core.ProbeError(code, "safe message")):
                    result = self.engine.check_once(PAPER, "private-token", KEY)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["action"], action)
                self.assertNotIn("private-token", json.dumps(result))
            self.assertFalse(self.check([])["data_changed"])
        send.assert_not_called()

    def test_push_failure_keeps_outbox_and_retry_reuses_event_id_after_restart(self):
        with patch.object(monitor, "send_notification", side_effect=core.ProbeError("network_error", "safe")) as failed:
            result = self.check([review()])
        self.assertEqual(result["notification_status"], "pending")
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["action"], "retry_push")
        original_event = deepcopy(failed.call_args.args[1])
        self.engine = monitor.Monitor(self.db_path)
        with patch.object(monitor, "send_notification") as retried, patch.object(
            monitor, "probe_openreview", side_effect=core.ProbeError("unauthorized", "expired session"),
        ) as probe:
            result = self.engine.check_once(PAPER, "expired-token", KEY)
            self.assertEqual(retried.call_args.args[1], original_event)
            self.assertFalse(result["data_changed"])
            self.assertEqual(result["notification_status"], "accepted")
            self.assertEqual(result["pending_count"], 0)
            self.assertEqual(result["action"], "completed")
            self.engine.check_once(PAPER, "expired-token", KEY)
            probe.assert_not_called()
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

    def test_pending_score_retry_still_fails_safely_without_querying_api(self):
        with patch.object(monitor, "send_notification", side_effect=core.ProbeError("network_error", "safe")):
            self.check([review()])
        with (
            patch.object(monitor, "probe_openreview", side_effect=AssertionError("Already observed scores")) as probe,
            patch.object(monitor, "send_notification", side_effect=core.ProbeError("bark_rejected", "safe")) as send,
        ):
            result = self.engine.check_once(PAPER, "", KEY)
        send.assert_called_once()
        probe.assert_not_called()
        self.assertEqual(result["action"], "retry_push")
        self.assertEqual(result["notification_status"], "pending")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE accepted_at IS NULL").fetchone()[0], 1)

    def test_legacy_pending_scores_are_drained_in_order_before_completion(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        events = [
            {"id": "legacy-first", "title": "first score", "body": "R1:6", "url": PAPER},
            {"id": "legacy-second", "title": "updated score", "body": "R1:7", "url": PAPER},
        ]
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO paper_state VALUES(?,?,?)", ("DemoPaper01", json.dumps({"R1": review()}), timestamp))
            for event in events:
                db.execute("INSERT INTO outbox VALUES(?,?,?,?,NULL)", (event["id"], "DemoPaper01", json.dumps(event), timestamp))
        with patch.object(monitor, "probe_openreview", side_effect=AssertionError("Already observed scores")) as probe, patch.object(monitor, "send_notification") as send:
            first = self.engine.check_once(PAPER, "", KEY)
            self.assertEqual(first["pending_count"], 1)
            self.assertNotEqual(first.get("action"), "completed")
            second = self.engine.check_once(PAPER, "", KEY)
            self.assertEqual(second["pending_count"], 0)
            self.assertEqual(second["action"], "completed")
            self.assertEqual([call.args[1] for call in send.call_args_list], events)
            probe.assert_not_called()

    def test_completion_and_pending_scores_are_isolated_by_forum(self):
        with patch.object(monitor, "send_notification", side_effect=core.ProbeError("network_error", "safe")):
            self.check([review()])
        other_paper = "https://openreview.net/forum?id=DemoPaper02"
        other = observation([])
        other["forum_id"] = "DemoPaper02"
        with patch.object(monitor, "probe_openreview", return_value=other) as probe, patch.object(monitor, "send_notification") as send:
            result = self.engine.check_once(other_paper, "", KEY)
            self.assertNotEqual(result.get("action"), "completed")
            self.assertEqual(result["pending_count"], 0)
            probe.assert_called_once_with(other_paper, "")
            send.assert_not_called()
        with patch.object(monitor, "send_notification"):
            self.assertEqual(self.engine.check_once(PAPER, "", KEY)["action"], "completed")
        with patch.object(monitor, "probe_openreview", return_value=other) as probe:
            self.assertNotEqual(self.engine.check_once(other_paper, "", KEY).get("action"), "completed")
            probe.assert_called_once()


class ReviewMergeTests(unittest.TestCase):
    def test_new_review_rating_and_confidence_changes_are_distinct(self):
        initial = {"R1": review()}
        merged, added, modified = monitor._merge_reviews(initial, [review(), review("R2", "8: Accept")])
        self.assertEqual(added, ["R2"])
        self.assertEqual(modified, [])
        changed, added, modified = monitor._merge_reviews(merged, [review(rating="7: Accept")])
        self.assertEqual(added, [])
        self.assertEqual(modified, ["R1"])
        _, added, modified = monitor._merge_reviews(changed, [review(rating="7: Accept", confidence="5: Very Confident")])
        self.assertEqual(added, [])
        self.assertEqual(modified, ["R1"])

    def test_reordering_and_numeric_label_formatting_do_not_change_scores(self):
        initial = {"R1": review(), "R2": review("R2", "8: Accept")}
        _, added, modified = monitor._merge_reviews(initial, [review("R2", 8), review(rating=6, confidence=4)])
        self.assertEqual(added, [])
        self.assertEqual(modified, [])

    def test_visibility_gaps_do_not_reset_known_scores(self):
        initial = {"R1": review()}
        for observed in ([], [review(rating=None)], [review(confidence=None)]):
            merged, added, modified = monitor._merge_reviews(initial, observed)
            self.assertEqual(merged, initial)
            self.assertEqual(added, [])
            self.assertEqual(modified, [])


class WaitingReminderTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "monitor.sqlite3"
        self.engine = monitor.Monitor(self.db_path)
        self.time = datetime(2027, 1, 1, tzinfo=timezone.utc)
        clock = patch.object(monitor, "_now", side_effect=lambda: self.time.isoformat())
        clock.start()
        self.addCleanup(clock.stop)
        sender = patch.object(monitor, "send_notification")
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)

    def check(self, reviews=None, **options):
        with patch.object(monitor, "probe_openreview", return_value=observation(reviews or [])):
            return self.engine.check_once(PAPER, "", KEY, **options)

    def test_first_success_starts_timer_and_only_exact_interval_sends_encouragement(self):
        self.assertEqual(self.check()["notification_status"], "none")
        self.advance(3599)
        self.assertEqual(self.check()["notification_status"], "none")
        self.send.assert_not_called()
        self.advance(1)
        result = self.check()
        event = self.send.call_args.args[1]
        self.assertEqual(result["notification_kind"], "waiting")
        self.assertEqual(event["title"], "ICLR：继续等待")
        self.assertEqual(event["body"], "继续等待，不要着急，不要担心，你是最棒的！")
        self.assertEqual(event["url"], PAPER)
        self.advance(3599)
        self.assertEqual(self.check()["notification_status"], "none")
        self.advance(1)
        self.assertEqual(self.check()["notification_status"], "accepted")
        self.assertEqual(self.send.call_count, 2)

    def test_restart_keeps_timer_and_a_long_gap_sends_only_one_reminder(self):
        self.check()
        self.advance(10800)
        self.engine = monitor.Monitor(self.db_path)
        self.assertEqual(self.check()["notification_status"], "accepted")
        self.assertEqual(self.check()["notification_status"], "none")
        self.advance(3599)
        self.assertEqual(self.check()["notification_status"], "none")
        self.advance(1)
        self.assertEqual(self.check()["notification_status"], "accepted")
        self.assertEqual(self.send.call_count, 2)

    def test_api_errors_do_not_schedule_or_send_a_due_or_pending_encouragement(self):
        self.check()
        self.advance(3600)
        for code in ("network_error", "unauthorized", "challenge_required"):
            with patch.object(monitor, "probe_openreview", side_effect=core.ProbeError(code, "safe")):
                result = self.engine.check_once(PAPER, "", KEY)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["notification_status"], "none")
        self.send.assert_not_called()
        self.send.side_effect = core.ProbeError("network_error", "safe")
        self.assertEqual(self.check()["pending_count"], 1)
        pending_event = deepcopy(self.send.call_args.args[1])
        self.send.reset_mock()
        with patch.object(monitor, "probe_openreview", side_effect=core.ProbeError("forbidden", "safe")):
            self.engine.check_once(PAPER, "", KEY)
        self.send.assert_not_called()
        self.send.side_effect = None
        self.assertEqual(self.check()["notification_status"], "accepted")
        self.assertEqual(self.send.call_args.args[1], pending_event)

    def test_failed_waiting_push_reuses_one_durable_event_after_restart(self):
        self.check()
        self.advance(3600)
        self.send.side_effect = core.ProbeError("network_error", "safe")
        self.assertEqual(self.check()["pending_count"], 1)
        first_event = deepcopy(self.send.call_args.args[1])
        self.advance(7200)
        self.engine = monitor.Monitor(self.db_path)
        self.assertEqual(self.check()["pending_count"], 1)
        self.assertEqual(self.send.call_args.args[1], first_event)
        with closing(sqlite3.connect(self.db_path)) as db:
            stored = db.execute("SELECT pending_json FROM waiting_schedule").fetchall()
            self.assertEqual(len(stored), 1)
            self.assertEqual(json.loads(stored[0][0]), first_event)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.send.side_effect = None
        self.assertEqual(self.check()["pending_count"], 0)
        self.assertEqual(self.send.call_args.args[1], first_event)
        self.assertEqual(self.check()["notification_status"], "none")
        self.advance(3600)
        self.check()
        self.assertNotEqual(self.send.call_args.args[1]["id"], first_event["id"])

    def test_score_arrival_at_deadline_completes_and_permanently_stops_waiting(self):
        self.check()
        self.advance(3600)
        result = self.check([review()])
        self.assertEqual(result["notification_kind"], "update")
        self.assertEqual(result["action"], "completed")
        self.assertEqual(self.send.call_count, 1)
        event = self.send.call_args.args[1]
        self.assertEqual(event["body"].splitlines()[0], "！！！！！！！！！！重要警报！！！！！！你的ICLR分数出来了，快来看咋样")
        self.assertIn("6: Weak Accept", event["body"])
        for elapsed in (3599, 1, 86400):
            self.advance(elapsed)
            self.engine = monitor.Monitor(self.db_path)
            with patch.object(monitor, "probe_openreview", side_effect=AssertionError("Completed tasks must not query OpenReview")) as probe:
                result = self.engine.check_once(PAPER, "", KEY)
            self.assertEqual(result["action"], "completed")
            self.assertEqual(result["notification_status"], "none")
            probe.assert_not_called()
        self.assertEqual(self.send.call_count, 1)

    def test_score_arrival_cancels_failed_waiting_event_and_never_restarts_it(self):
        self.check()
        self.advance(3600)
        self.send.side_effect = core.ProbeError("network_error", "safe")
        self.check()
        waiting_id = self.send.call_args.args[1]["id"]
        self.send.side_effect = None
        self.advance(1)
        result = self.check([review()])
        self.assertEqual(result["notification_kind"], "update")
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["action"], "completed")
        self.assertNotEqual(result["event_id"], waiting_id)
        self.advance(3599)
        confidence_change = [review(confidence="5: Very Confident")]
        self.assertEqual(self.check(confidence_change)["action"], "completed")
        self.advance(3600)
        self.assertEqual(self.check(confidence_change)["notification_status"], "none")
        self.assertEqual(self.send.call_count, 2)
        self.assertNotIn(waiting_id, [call.args[1]["id"] for call in self.send.call_args_list[1:]])

    def test_pending_score_alert_prevents_encouragement_from_accumulating(self):
        self.send.side_effect = core.ProbeError("network_error", "safe")
        self.check([review()])
        score_event = deepcopy(self.send.call_args.args[1])
        self.advance(7200)
        result = self.check([review()])
        self.assertEqual(result["notification_kind"], "update")
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["action"], "retry_push")
        self.assertEqual(self.send.call_args.args[1], score_event)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM waiting_schedule WHERE pending_json IS NOT NULL").fetchone()[0], 0)

    def test_zero_interval_cancels_pending_waiting_and_never_disables_score_alerts(self):
        self.check()
        self.advance(3600)
        self.send.side_effect = core.ProbeError("network_error", "safe")
        self.check()
        self.send.side_effect = None
        self.send.reset_mock()
        self.assertEqual(self.check(waiting_interval_seconds=0)["pending_count"], 0)
        self.advance(7200)
        self.assertEqual(self.check(waiting_interval_seconds=0)["notification_status"], "none")
        self.send.assert_not_called()
        self.assertEqual(self.check([review()], waiting_interval_seconds=0)["notification_kind"], "update")
        self.advance(3600)
        self.assertEqual(self.check([review()], waiting_interval_seconds=0)["notification_status"], "none")
        self.assertEqual(self.send.call_count, 1)

    def test_custom_interval_and_messages_preserve_score_details(self):
        options = {"waiting_interval_seconds": 10, "waiting_message": "自定义等待原文！", "update_message": "自定义警报原文！"}
        self.check(**options)
        self.advance(9)
        self.assertEqual(self.check(**options)["notification_status"], "none")
        self.advance(1)
        self.check(**options)
        self.assertEqual(self.send.call_args.args[1]["body"], "自定义等待原文！")
        result = self.check([review()], **options)
        self.assertEqual(result["action"], "completed")
        event = self.send.call_args.args[1]
        self.assertTrue(event["body"].startswith("自定义警报原文！\nExample paper\n"))
        self.assertIn("置信度 4: Confident", event["body"])
        self.assertEqual(event["url"], PAPER)
        self.advance(10)
        self.assertEqual(self.check([review()], **options)["notification_status"], "none")
        self.assertEqual(self.send.call_count, 2)

    def test_each_paper_has_an_independent_persistent_waiting_timer(self):
        self.check()
        self.advance(1800)
        second = observation([])
        second["forum_id"] = "DemoPaper02"
        second_paper = "https://openreview.net/forum?id=DemoPaper02"
        with patch.object(monitor, "probe_openreview", return_value=second):
            self.engine.check_once(second_paper, "", KEY)
        self.advance(1800)
        self.assertEqual(self.check()["notification_kind"], "waiting")
        with patch.object(monitor, "probe_openreview", return_value=second):
            self.assertEqual(self.engine.check_once(second_paper, "", KEY)["notification_status"], "none")
            self.advance(1800)
            self.assertEqual(self.engine.check_once(second_paper, "", KEY)["notification_kind"], "waiting")
        self.assertEqual(self.send.call_args.args[1]["url"], second_paper)

    def test_existing_database_migrates_without_changing_snapshot_or_pending_score(self):
        original = {"R1": review()}
        pending = {"id": "legacy-score-event", "title": "旧评分通知", "body": "R1: 6", "url": PAPER}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DROP TABLE waiting_schedule")
            db.execute("INSERT INTO paper_state VALUES(?,?,?)", ("DemoPaper01", json.dumps(original), self.time.isoformat()))
            db.execute("INSERT INTO outbox VALUES(?,?,?,?,NULL)", (pending["id"], "DemoPaper01", json.dumps(pending), self.time.isoformat()))
        self.engine = monitor.Monitor(self.db_path)
        with patch.object(monitor, "probe_openreview", side_effect=AssertionError("Legacy scores must be reused")) as probe:
            result = self.engine.check_once(PAPER, "expired-token", KEY)
        probe.assert_not_called()
        self.assertFalse(result["data_changed"])
        self.assertEqual(self.send.call_args.args[1], pending)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["action"], "completed")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(json.loads(db.execute("SELECT reviews_json FROM paper_state").fetchone()[0]), original)
            self.assertIsNotNone(db.execute("SELECT accepted_at FROM outbox").fetchone()[0])
        self.advance(86400)
        self.assertEqual(self.check([])["action"], "completed")
        self.assertEqual(self.send.call_count, 1)

    def test_upgrade_with_delivered_scores_discards_legacy_pending_waiting(self):
        self.check([review()])
        waiting = {"id": "legacy-waiting", "title": "ICLR：继续等待", "body": "stale reminder", "url": PAPER}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO waiting_schedule VALUES(?,?,?)",
                ("DemoPaper01", self.time.isoformat(), json.dumps(waiting)),
            )
        self.send.reset_mock()
        self.advance(86400)
        self.engine = monitor.Monitor(self.db_path)
        with patch.object(monitor, "probe_openreview", side_effect=core.ProbeError("unauthorized", "expired")) as probe:
            result = self.engine.check_once(PAPER, "expired-token", KEY)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "completed")
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["notification_status"], "none")
        probe.assert_not_called()
        self.send.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM waiting_schedule WHERE pending_json IS NOT NULL").fetchone()[0], 0)


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

"""Offline API-boundary tests. No test sends a real notification."""

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from iclr_alert import core


FORUM_ID = "DemoPaper01"
PAPER_URL = f"https://openreview.net/forum?id={FORUM_ID}"
DEVICE_KEY = "exampledevicekey1234567890"
DEVICE_TOKEN = "abcdef0123456789" * 4
OFFICIAL_REVIEW = "ICLR.cc/2027/Conference/Submission123/-/Official_Review"


class JsonResponse(io.BytesIO):
    status = 200
    headers = {}

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))

    def getcode(self):
        return self.status


def review(note_id, rating=None, invitation=OFFICIAL_REVIEW):
    content = {"confidence": {"value": "4: Confident"}}
    if rating is not None:
        content["rating"] = {"value": rating}
    return {
        "id": note_id,
        "forum": FORUM_ID,
        "invitations": [invitation],
        "signatures": [f"ICLR.cc/2027/Conference/Submission123/Reviewer_{note_id}"],
        "content": content,
    }


class OpenReviewFixture:
    """Fake external responses while allowing real parsing and orchestration."""

    def __init__(self, notes):
        self.notes = notes
        self.requests = []

    def __call__(self, request, *args, **kwargs):
        self.requests.append(request)
        url = urlsplit(request.full_url)
        if url.scheme != "https" or url.netloc != "api2.openreview.net":
            raise AssertionError("OpenReview requests must use the official HTTPS API")
        query = parse_qs(url.query)
        if "id" in query:
            return JsonResponse({"notes": [{
                "id": FORUM_ID,
                "forum": FORUM_ID,
                "content": {"title": {"value": "A test paper"}},
            }]})
        offset = int(query.get("offset", [0])[0])
        limit = int(query.get("limit", [1000])[0])
        return JsonResponse({
            "notes": self.notes[offset:offset + limit],
            "count": len(self.notes),
        })


class ForumIdTests(unittest.TestCase):
    def test_bare_id_and_author_console_link_resolve_same_forum(self):
        self.assertEqual(core.parse_forum_id(FORUM_ID), FORUM_ID)
        self.assertEqual(
            core.parse_forum_id(PAPER_URL + "&referrer=Author%20Console"), FORUM_ID
        )

    def test_invalid_urls_cannot_change_the_api_host_or_paper(self):
        for value in (
            "https://evil.example/forum?id=" + FORUM_ID,
            "https://openreview.net.evil.example/forum?id=" + FORUM_ID,
            "https://openreview.net/forum",
            "https://openreview.net/group?id=ICLR.cc/2027/Conference",
            "",
        ):
            with self.subTest(value=value), self.assertRaises(core.ProbeError):
                core.parse_forum_id(value)


class OpenReviewProbeTests(unittest.TestCase):
    def run_probe(self, notes, token=""):
        fixture = OpenReviewFixture(notes)
        with patch.object(core, "urlopen", side_effect=fixture):
            result = core.probe_openreview(PAPER_URL, token=token)
        return result, fixture

    def test_extracts_official_reviews_and_ignores_comment_scores(self):
        notes = [
            review("R1", "6: Weak Accept"),
            review("R2", 8),
            review("R3"),
            review("C1", 10, OFFICIAL_REVIEW.replace("Official_Review", "Official_Comment")),
            review("C2", 10, OFFICIAL_REVIEW.replace("Official_Review", "Not_Official_Review")),
        ]
        result, fixture = self.run_probe(notes)
        self.assertEqual(result["status"], "reviews_visible")
        self.assertEqual(result["forum_id"], FORUM_ID)
        self.assertEqual(result["title"], "A test paper")
        self.assertEqual(result["review_count"], 3)
        self.assertEqual(result["scored_review_count"], 2)
        self.assertEqual(result["average_rating"], 7.0)
        self.assertEqual({r["id"] for r in result["reviews"]}, {"R1", "R2", "R3"})
        missing = next(r for r in result["reviews"] if r["id"] == "R3")
        self.assertIsNone(missing["rating"])
        self.assertTrue(result["checked_at"])
        self.assertTrue(all(r.get_header("Authorization") is None for r in fixture.requests))

    def test_no_reviews_is_distinct_from_hidden_rating_fields(self):
        empty, _ = self.run_probe([])
        hidden, _ = self.run_probe([review("R1")])
        self.assertEqual(empty["status"], "no_visible_reviews")
        self.assertEqual(empty["review_count"], 0)
        self.assertEqual(hidden["status"], "ratings_unavailable")
        self.assertEqual(hidden["review_count"], 1)
        self.assertEqual(hidden["scored_review_count"], 0)
        self.assertIsNone(hidden["average_rating"])

    def test_rating_prose_is_not_misread_as_a_numeric_score(self):
        result, _ = self.run_probe([review("R1", "I considered 10 approaches")])
        self.assertEqual(result["status"], "ratings_unavailable")
        self.assertEqual(result["scored_review_count"], 0)
        self.assertIsNone(result["average_rating"])

    def test_reviews_on_later_pages_are_not_missed(self):
        comments = [review(f"C{n}", 10, "ICLR.cc/2027/-/Official_Comment") for n in range(1000)]
        result, fixture = self.run_probe(comments + [review("R1", "7: Accept")])
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["average_rating"], 7.0)
        forum_requests = [r for r in fixture.requests if "forum=" in r.full_url]
        self.assertGreaterEqual(len(forum_requests), 2)

    def test_authorization_failure_is_reported_without_echoing_token(self):
        secret = "private-session-token-do-not-print"
        failure = HTTPError(
            "https://api2.openreview.net/notes", 401, secret, {}, io.BytesIO(b"{}")
        )
        with patch.object(core, "urlopen", side_effect=failure):
            with self.assertRaises(core.ProbeError) as captured:
                core.probe_openreview(PAPER_URL, token=secret)
        self.assertTrue(captured.exception.code)
        self.assertNotIn(secret, str(captured.exception))

    def test_supplied_token_is_in_headers_and_never_in_request_urls(self):
        secret = "private-session-token-do-not-print"
        _, fixture = self.run_probe([], token=secret)
        for request in fixture.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer " + secret)
            self.assertNotIn(secret, request.full_url)

    def test_openreview_access_challenge_is_distinct_from_permission_failure(self):
        error = HTTPError(
            "https://api2.openreview.net/notes", 403, "Forbidden",
            {"Content-Type": "application/json"},
            io.BytesIO(json.dumps({"name": "ChallengeRequiredError", "status": 403}).encode()),
        )
        with patch.object(core, "urlopen", side_effect=error):
            with self.assertRaises(core.ProbeError) as captured:
                core.probe_openreview(PAPER_URL)
        self.assertEqual(captured.exception.code, "challenge_required")


class BarkProbeTests(unittest.TestCase):
    def test_json_post_uses_fixed_https_endpoint_and_reports_only_acceptance(self):
        with patch.object(core, "urlopen", return_value=JsonResponse({"code": 200})) as send:
            result = core.send_bark_test(DEVICE_KEY)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.day.app/push")
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn(DEVICE_KEY, request.full_url)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["device_key"], DEVICE_KEY)
        self.assertEqual(body["ttl"], 600)
        self.assertIn("测试", body["title"])
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["test_id"])
        self.assertTrue(result["accepted_at"])
        self.assertNotIn("delivered", result)
        self.assertNotIn(DEVICE_KEY, json.dumps(result))
        self.assertGreater(send.call_args.kwargs.get("timeout", 0), 0)

    def test_provider_rejection_does_not_claim_notification_was_sent(self):
        with patch.object(core, "urlopen", return_value=JsonResponse({"code": 400, "message": DEVICE_KEY})):
            with self.assertRaises(core.ProbeError) as captured:
                core.send_bark_test(DEVICE_KEY)
        self.assertNotIn(DEVICE_KEY, str(captured.exception))

    def test_network_failure_is_safe_and_does_not_retry_a_push(self):
        with patch.object(core, "urlopen", side_effect=URLError(DEVICE_KEY)) as send:
            with self.assertRaises(core.ProbeError) as captured:
                core.send_bark_test(DEVICE_KEY)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn(DEVICE_KEY, str(captured.exception))

    def test_missing_device_key_never_attempts_a_network_request(self):
        with patch.object(core, "urlopen") as send:
            with self.assertRaises(core.ProbeError):
                core.send_bark_test("")
        send.assert_not_called()


class BarkRegistrationTests(unittest.TestCase):
    def test_registration_posts_token_to_fixed_official_endpoint(self):
        response = {"code": 200, "data": {"device_key": DEVICE_KEY}}
        with patch.object(core, "urlopen", return_value=JsonResponse(response)) as register:
            result = core.register_bark_device(DEVICE_TOKEN)
        self.assertEqual(result, DEVICE_KEY)
        request = register.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.day.app/register")
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn(DEVICE_TOKEN, request.full_url)
        self.assertEqual(json.loads(request.data.decode("utf-8")), {
            "device_token": DEVICE_TOKEN,
            "device_type": "ios",
        })
        self.assertGreater(register.call_args.kwargs.get("timeout", 0), 0)
        self.assertEqual(register.call_count, 1)

    def test_invalid_device_token_is_rejected_before_network(self):
        with patch.object(core, "urlopen") as register:
            for token in ("", "not-hexadecimal", "a" * 15, "a" * 161, "https://api.day.app/register"):
                with self.subTest(token=token), self.assertRaises(core.ProbeError):
                    core.register_bark_device(token)
        register.assert_not_called()

    def test_rejected_or_malformed_registration_never_echoes_private_response(self):
        invalid_responses = (
            {"code": 400, "message": DEVICE_TOKEN},
            {"code": 200, "message": DEVICE_TOKEN},
            {"code": 200, "data": {}},
            {"code": 200, "data": {"device_key": ""}},
            {"code": 200, "data": {"device_key": [DEVICE_TOKEN]}},
        )
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(
                core, "urlopen", return_value=JsonResponse(response)
            ):
                with self.assertRaises(core.ProbeError) as captured:
                    core.register_bark_device(DEVICE_TOKEN)
                self.assertNotIn(DEVICE_TOKEN, str(captured.exception))

    def test_registration_network_failure_is_safe_and_not_retried(self):
        with patch.object(core, "urlopen", side_effect=URLError(DEVICE_TOKEN)) as register:
            with self.assertRaises(core.ProbeError) as captured:
                core.register_bark_device(DEVICE_TOKEN)
        self.assertEqual(register.call_count, 1)
        self.assertNotIn(DEVICE_TOKEN, str(captured.exception))


if __name__ == "__main__":
    unittest.main()

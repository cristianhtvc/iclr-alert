"""Local setup behavior and HTTP authorization, without sockets or real pushes."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from email.message import Message
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from iclr_alert import web
from iclr_alert.core import ProbeError


PAPER_URL = "https://openreview.net/forum?id=DemoPaper01"
DEVICE_KEY = "private-bark-key-do-not-echo"
OPENREVIEW_TOKEN = "private-openreview-session-do-not-echo"
SETUP_TOKEN = "local-test-setup-token"
PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"


def accepted_test():
    return {
        "status": "accepted",
        "test_id": "known-test-id",
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


class PrivateDirectoryTestCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.private_dir = Path(directory.name)
        private_patch = patch.object(web, "PRIVATE_DIR", self.private_dir)
        private_patch.start()
        self.addCleanup(private_patch.stop)


class SetupStateTests(PrivateDirectoryTestCase):
    def test_credentials_persist_locally_but_are_not_in_public_status(self):
        state = web.SetupState(PAPER_URL)
        with patch.object(web, "probe_openreview", return_value={"status": "no_visible_reviews"}):
            state.probe({"paper": PAPER_URL, "token": OPENREVIEW_TOKEN})
        with patch.object(web, "send_bark_test", return_value=accepted_test()):
            state.bark({"device_key": DEVICE_KEY})
        saved = json.loads((self.private_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["bark_key"], DEVICE_KEY)
        self.assertEqual(saved["openreview_token"], OPENREVIEW_TOKEN)
        public = state.public_status()
        serialized = json.dumps(public)
        self.assertNotIn(DEVICE_KEY, serialized)
        self.assertNotIn(OPENREVIEW_TOKEN, serialized)
        self.assertNotIn("bark_key", public)
        self.assertNotIn("openreview_token", public)
        self.assertTrue(public["bark_configured"])
        self.assertTrue(public["token_configured"])
        self.assertEqual(public["bark"]["status"], "accepted")

    def test_confirmation_requires_accepted_test_with_matching_id(self):
        state = web.SetupState(PAPER_URL)
        with self.assertRaises(ProbeError):
            state.confirm({"test_id": "known-test-id"})
        state.status["bark"] = accepted_test()
        with self.assertRaises(ProbeError):
            state.confirm({"test_id": "wrong-test-id"})
        self.assertEqual(state.status["bark"]["status"], "accepted")
        state.confirm({"test_id": "known-test-id"})
        self.assertEqual(state.status["bark"]["status"], "confirmed")
        self.assertTrue(state.status["bark"]["confirmed_at"])

    def test_rejected_push_does_not_store_an_unverified_key(self):
        state = web.SetupState(PAPER_URL)
        with patch.object(web, "send_bark_test", side_effect=ProbeError("bark_rejected", "拒绝")):
            with self.assertRaises(ProbeError):
                state.bark({"device_key": DEVICE_KEY})
        self.assertNotIn("bark_key", state.config)
        self.assertEqual(state.status["bark"]["status"], "not_sent")

    def test_cooldown_blocks_accidental_second_notification(self):
        state = web.SetupState(PAPER_URL)
        with patch.object(web, "send_bark_test", return_value=accepted_test()) as send:
            state.bark({"device_key": DEVICE_KEY})
            with self.assertRaises(ProbeError) as captured:
                state.bark({"device_key": DEVICE_KEY})
        self.assertEqual(captured.exception.code, "cooldown")
        self.assertEqual(send.call_count, 1)


class HttpAuthorizationTests(PrivateDirectoryTestCase):
    def setUp(self):
        super().setUp()
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with (
            patch.object(web, "ThreadingHTTPServer", return_value=server) as create_server,
            patch.object(web.secrets, "token_urlsafe", return_value=SETUP_TOKEN),
            redirect_stdout(io.StringIO()),
        ):
            web.serve_local(PORT, PAPER_URL)
        self.handler_class = create_server.call_args.args[1]
        self.assertEqual(create_server.call_args.args[0], ("127.0.0.1", PORT))

    def request(self, method, path, body=None, overrides=None):
        """Feed HTTP handler inputs and inspect its actual serialized response."""
        payload = json.dumps(body or {}).encode("utf-8")
        headers = {
            "Host": f"127.0.0.1:{PORT}",
            "Origin": ORIGIN,
            "X-Setup-Token": SETUP_TOKEN,
            "Content-Length": str(len(payload)),
        }
        headers.update(overrides or {})
        handler = self.handler_class.__new__(self.handler_class)
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = Message()
        for name, value in headers.items():
            if value is not None:
                handler.headers[name] = value
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        getattr(handler, "do_" + method)()
        response_headers, response_body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        status = int(response_headers.split(b" ", 2)[1])
        return status, response_body.decode("utf-8"), response_headers.decode("utf-8")

    def test_post_requires_exact_host_origin_and_setup_token(self):
        invalid_headers = (
            {"Host": None},
            {"Host": "evil.example"},
            {"Host": f"localhost:{PORT}"},
            {"Origin": None},
            {"Origin": "https://evil.example"},
            {"Origin": f"http://localhost:{PORT}"},
            {"X-Setup-Token": None},
            {"X-Setup-Token": "incorrect-token"},
        )
        with patch.object(web, "send_bark_test") as send:
            for overrides in invalid_headers:
                with self.subTest(headers=overrides):
                    status, body, _ = self.request(
                        "POST", "/api/bark", {"device_key": DEVICE_KEY}, overrides
                    )
                    self.assertEqual(status, 403)
                    self.assertNotIn(DEVICE_KEY, body)
        send.assert_not_called()

    def test_authorized_probe_response_does_not_echo_private_session(self):
        with patch.object(web, "probe_openreview", return_value={"status": "no_visible_reviews"}) as probe:
            status, body, headers = self.request(
                "POST", "/api/probe", {"paper": PAPER_URL, "token": OPENREVIEW_TOKEN}
            )
        self.assertEqual(status, 200)
        probe.assert_called_once_with(PAPER_URL, OPENREVIEW_TOKEN)
        self.assertNotIn(OPENREVIEW_TOKEN, body)
        self.assertTrue(json.loads(body)["token_configured"])
        self.assertIn("Cache-Control: no-store", headers)

    def test_status_endpoint_requires_token_and_never_returns_private_config(self):
        status, _, _ = self.request("GET", "/api/status", overrides={"X-Setup-Token": None})
        self.assertNotEqual(status, 200)
        with patch.object(web, "send_bark_test", return_value=accepted_test()):
            sent_status, _, _ = self.request("POST", "/api/bark", {"device_key": DEVICE_KEY})
        self.assertEqual(sent_status, 200)
        status, body, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["bark_configured"])
        self.assertNotIn(DEVICE_KEY, body)

    def test_confirm_route_cannot_claim_receipt_before_a_successful_send(self):
        status, _, _ = self.request("POST", "/api/confirm", {"test_id": "known-test-id"})
        self.assertEqual(status, 400)
        with patch.object(web, "send_bark_test", return_value=accepted_test()):
            self.request("POST", "/api/bark", {"device_key": DEVICE_KEY})
        wrong_status, _, _ = self.request("POST", "/api/confirm", {"test_id": "wrong-id"})
        self.assertEqual(wrong_status, 400)
        status, body, _ = self.request("POST", "/api/confirm", {"test_id": "known-test-id"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["bark"]["status"], "confirmed")

    def test_bad_input_and_unexpected_errors_do_not_echo_secrets(self):
        with patch.object(web, "send_bark_test") as send:
            status, _, _ = self.request("POST", "/api/bark", {"device_key": [DEVICE_KEY]})
        self.assertEqual(status, 400)
        send.assert_not_called()
        with patch.object(web, "send_bark_test", side_effect=RuntimeError(DEVICE_KEY)):
            status, body, _ = self.request("POST", "/api/bark", {"device_key": DEVICE_KEY})
        self.assertEqual(status, 500)
        self.assertNotIn(DEVICE_KEY, body)


if __name__ == "__main__":
    unittest.main()

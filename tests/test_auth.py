"""Official login and MFA HTTP contracts, without network or real credentials."""

import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from iclr_alert import auth, core


class JsonResponse(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))


class LoginTests(unittest.TestCase):
    def test_official_login_body_and_only_session_token_returned(self):
        with patch.object(core, "urlopen", return_value=JsonResponse({
            "token": "private.session.token", "user": {"password": "must-not-return"}
        })) as transport:
            result = auth.login(" author@example.com ", " password ")
        self.assertEqual(result, {"token": "private.session.token"})
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api2.openreview.net/login")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "id": "author@example.com", "password": " password ", "expiresIn": 604800
        })
        self.assertNotIn("password", request.full_url)
        self.assertEqual(transport.call_args.kwargs["timeout"], core.REQUEST_TIMEOUT_SECONDS)

    def test_empty_credentials_do_not_attempt_network(self):
        with patch.object(core, "urlopen") as transport:
            for username, password in (("", "secret"), (" ", "secret"), ("author", "")):
                with self.subTest(username=username), self.assertRaises(core.ProbeError):
                    auth.login(username, password)
        transport.assert_not_called()

    def test_bad_credentials_and_access_challenge_remain_distinct(self):
        for code, payload, expected in (
            (401, {"message": "private.password"}, "invalid_credentials"),
            (403, {"name": "ChallengeRequiredError", "private": "private.password"}, "challenge_required"),
        ):
            error = HTTPError("https://api2.openreview.net/login", code, "private.password", {}, io.BytesIO(json.dumps(payload).encode()))
            with self.subTest(code=code), patch.object(core, "urlopen", side_effect=error) as transport:
                with self.assertRaises(core.ProbeError) as captured:
                    auth.login("author", "private.password")
                self.assertEqual(captured.exception.code, expected)
                self.assertNotIn("private.password", str(captured.exception))
                self.assertEqual(transport.call_count, 1)

    def test_network_failure_is_safe_without_retry(self):
        with patch.object(core, "urlopen", side_effect=URLError("private.password")) as transport:
            with self.assertRaises(core.ProbeError) as captured:
                auth.login("author", "private.password")
        self.assertEqual(captured.exception.code, "network_error")
        self.assertNotIn("private.password", str(captured.exception))
        self.assertEqual(transport.call_count, 1)

    def test_malformed_response_never_echoes_server_secrets(self):
        for response in ({"private": "private.password"}, {"token": ["private.password"]}, {"token": "bad\ntoken"}):
            with self.subTest(response=response), patch.object(core, "urlopen", return_value=JsonResponse(response)):
                with self.assertRaises(core.ProbeError) as captured:
                    auth.login("author", "private.password")
                self.assertEqual(captured.exception.code, "invalid_response")
                self.assertNotIn("private.password", str(captured.exception))

    def test_mfa_without_callback_does_not_trigger_email_or_echo_pending_token(self):
        pending = {"mfaPending": True, "mfaPendingToken": "private.pending", "mfaMethods": ["emailOtp"]}
        with patch.object(core, "urlopen", return_value=JsonResponse(pending)) as transport:
            with self.assertRaises(core.ProbeError) as captured:
                auth.login("author", "secret")
        self.assertEqual(captured.exception.code, "mfa_required")
        self.assertNotIn("private.pending", str(captured.exception))
        self.assertEqual(transport.call_count, 1)

    def test_totp_is_verified_without_email_challenge(self):
        pending = {"mfaPending": True, "mfaPendingToken": "private.pending", "mfaMethods": ["totp"]}
        provider = Mock(return_value="123456")
        with patch.object(core, "urlopen", side_effect=[JsonResponse(pending), JsonResponse({"token": "private.session"})]) as transport:
            self.assertEqual(auth.login("author", "secret", provider), {"token": "private.session"})
        provider.assert_called_once_with("totp")
        verify = transport.call_args_list[1].args[0]
        self.assertEqual(verify.full_url, "https://api2.openreview.net/mfa/verify")
        self.assertEqual(json.loads(verify.data), {"mfaPendingToken": "private.pending", "method": "totp", "code": "123456"})

    def test_email_challenge_precedes_code_prompt_and_verify(self):
        pending = {"mfaPending": True, "mfaPendingToken": "private.pending", "mfaMethods": ["totp", "emailOtp"], "preferredMethod": "emailOtp"}
        provider = Mock(return_value="123456")
        responses = [JsonResponse(pending), JsonResponse({"success": True}), JsonResponse({"token": "private.session"})]
        with patch.object(core, "urlopen", side_effect=responses) as transport:
            auth.login("author", "secret", provider)
        provider.assert_called_once_with("emailOtp")
        challenge = transport.call_args_list[1].args[0]
        self.assertEqual(challenge.full_url, "https://api2.openreview.net/mfa/challenge")
        self.assertEqual(json.loads(challenge.data), {"mfaPendingToken": "private.pending", "method": "emailOtp"})
        self.assertEqual(transport.call_args_list[2].args[0].full_url, "https://api2.openreview.net/mfa/verify")

    def test_passkey_only_and_cancelled_otp_do_not_attempt_verification(self):
        for methods, code in ((["passkey"], "123456"), (["totp"], ""), (["totp"], "wrong-code")):
            pending = {"mfaPending": True, "mfaPendingToken": "private.pending", "mfaMethods": methods}
            with self.subTest(methods=methods, code=code), patch.object(core, "urlopen", return_value=JsonResponse(pending)) as transport:
                with self.assertRaises(core.ProbeError):
                    auth.login("author", "secret", lambda method: code)
                self.assertEqual(transport.call_count, 1)


if __name__ == "__main__":
    unittest.main()

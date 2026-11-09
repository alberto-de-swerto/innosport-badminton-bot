import base64
import json
import time
import unittest
from unittest import mock

import api_auth


USER_ID = "0123456789abcdef01234567"


def fixture_token(claims):
    # Fake signatures are only for metadata/transport tests; the real server verifies signatures.
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "RS256"}) + "." + encode(claims) + ".fixture-signature"


def personal_token(**overrides):
    claims = {"scope": "sport:" + USER_ID, "exp": time.time() + 86400 * 300}
    claims.update(overrides)
    return fixture_token(claims)


class TokenTests(unittest.TestCase):
    def test_only_own_sport_scope_is_accepted(self):
        metadata = api_auth.service_metadata(personal_token())
        self.assertEqual(metadata["user_id"], USER_ID)
        for scope in ("sport", "users", "sport:me", "sport:" + USER_ID + " users", "sport:other", None):
            with self.subTest(scope=scope), self.assertRaises(api_auth.APIAuthError):
                api_auth.service_metadata(personal_token(scope=scope))

    def test_expired_or_corrupt_credentials_are_rejected_without_echoing_them(self):
        for token in (personal_token(exp=1), "sensitive-fixture-not-a-token", fixture_token([]),
                      personal_token(exp="forever")):
            with self.subTest(), self.assertRaises(api_auth.APIAuthError) as raised:
                api_auth.service_metadata(token)
            self.assertNotIn(token, str(raised.exception))

    def response(self, status=200, body=None, content_type="application/json"):
        response = mock.Mock(status=status, headers={"content-type": content_type})
        response.json.return_value = body
        return response

    def test_creation_requests_personal_scope_and_explicit_long_expiration(self):
        token = personal_token()
        request = mock.Mock()
        request.get.return_value = self.response(body={"access_token": token})
        returned, metadata = api_auth.generate_service_token(request, 20)
        self.assertEqual(returned, token)
        args, kwargs = request.get.call_args
        self.assertEqual(args, (api_auth.ACCOUNTS_ORIGIN + "/tokens/generate-service-token",))
        self.assertEqual(kwargs["params"]["scopes"], "sport:me")
        self.assertEqual(kwargs["params"]["expiration"], "next-14-august")
        self.assertNotIn("Authorization", kwargs["headers"])
        request.get.return_value.dispose.assert_called_once()

    def test_failures_never_follow_redirects_or_echo_credentials(self):
        for status in (302, 401, 403, 404, 500):
            with self.subTest(status=status):
                request = mock.Mock()
                request.get.return_value = self.response(status=status, body={"detail": "secret-response"})
                with self.assertRaises((api_auth.APIAuthError, api_auth.APINetworkError)) as raised:
                    api_auth.accounts_get(request, "/tokens/generate-sport-token", 20, token="private-key")
                self.assertNotIn("private-key", str(raised.exception))
                self.assertNotIn("secret-response", str(raised.exception))
                self.assertEqual(request.get.call_args.kwargs["max_redirects"], 0)

    def test_network_exception_does_not_leak_request_headers(self):
        from playwright.sync_api import Error
        request = mock.Mock()
        request.get.side_effect = Error("Authorization: Bearer private-key")
        with self.assertRaises(api_auth.APINetworkError) as raised:
            api_auth.accounts_get(request, "/tokens/generate-sport-token", 20, token="private-key")
        self.assertNotIn("private-key", str(raised.exception))

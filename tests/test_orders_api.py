import unittest

from orders_api import capture_session_auth


class SessionAuthTests(unittest.TestCase):
    def test_reads_nested_session_and_third_party_fields(self):
        payload = {
            "data": {
                "sessionId": "session-value",
                "thirdPartyToken": "third-party-value",
                "profile": {"displayName": "ignored"},
            }
        }
        self.assertEqual(
            capture_session_auth(payload),
            {
                "sessionid": "session-value",
                "thirdpartytoken": "third-party-value",
            },
        )

    def test_supports_snake_case_and_bearer_authorization(self):
        payload = {
            "result": {
                "access_token": "access-value",
                "authorization": "Bearer auth-value",
            }
        }
        self.assertEqual(
            capture_session_auth(payload),
            {
                "accesstoken": "access-value",
                "authorization": "Bearer auth-value",
            },
        )

    def test_does_not_capture_password_or_unrelated_fields(self):
        payload = {"password": "must-not-be-kept", "userName": "demo"}
        self.assertEqual(capture_session_auth(payload), {})


if __name__ == "__main__":
    unittest.main()

import json
import unittest

from orders_api import capture_session_auth, fetch_open_orders, submit_buy_order


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

    def test_login_succeeded_uses_explicit_success_flag(self):
        from orders_api import login_succeeded

        self.assertTrue(login_succeeded({"isSuccess": True}))
        self.assertTrue(login_succeeded({"isSuccess": "true"}))
        self.assertFalse(login_succeeded({"isSuccess": False}))
        self.assertFalse(login_succeeded({"errorCode": 0}))

    def test_does_not_capture_password_or_unrelated_fields(self):
        payload = {"password": "must-not-be-kept", "userName": "demo"}
        self.assertEqual(capture_session_auth(payload), {})


class _FakeResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class _FakeOpener:
    def __init__(self, post_body):
        self.requests = []
        self.post_body = post_body

    def open(self, request, timeout=20):
        self.requests.append(request)
        if request.get_method() == "OPTIONS":
            return _FakeResponse(204)
        return _FakeResponse(200, self.post_body)


class FetchOpenOrdersTests(unittest.TestCase):
    def test_sends_x_sessionid_header(self):
        class OpenOrdersOpener:
            def __init__(self):
                self.request = None

            def open(self, request, timeout=20):
                self.request = request
                return _FakeResponse(200, b'{"orders":[]}')

        opener = OpenOrdersOpener()
        result = fetch_open_orders(opener, [], {"sessionid": "test-session"})
        self.assertTrue(result["ok"])
        self.assertEqual(opener.request.get_header("X-sessionid"), "test-session")


class SubmitOrderTests(unittest.TestCase):
    def test_sell_request_uses_side_code_two(self):
        opener = _FakeOpener(b'{"isSuccess":true}')
        result = submit_buy_order(opener, [], {"sessionid": "test-session"},
                                  "IRO1IKCO0001", 12500, 100, side="sell")
        self.assertTrue(result["ok"])
        self.assertEqual(len(opener.requests), 2)
        payload = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual(payload["side"], 2)
        self.assertEqual(payload["validity"], 1)
        self.assertEqual(payload["isin"], "IRO1IKCO0001")

    def test_missing_explicit_success_is_uncertain(self):
        opener = _FakeOpener(b'{"message":"accepted"}')
        result = submit_buy_order(opener, [], {"sessionid": "test-session"},
                                  "IRO1IKCO0001", 12500, 100, side="buy")
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])


if __name__ == "__main__":
    unittest.main()

"""Read-only helpers for the Dana brokerage order API.

Authentication material is kept in the server process only and is never
returned to the browser or written to disk.
"""
import json
import urllib.error
import urllib.request


def capture_session_auth(payload):
    """Extract recognized auth fields from the login JSON without exposing them."""
    found = {}
    wanted = {
        "sessionid", "thirdpartytoken", "accesstoken",
        "token", "jwt", "authorization",
    }

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
                if normalized in wanted and isinstance(child, (str, int)) and str(child):
                    found[normalized] = str(child)
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def fetch_open_orders(opener, cookies, session_auth):
    """Fetch active orders (type=1) using the login CookieJar and auth fields."""
    url = "https://api-dana.ephoenix.ir/api/v2/orders/GetOpenOrders?type=1"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://dana.ephoenix.ir",
        "Referer": "https://dana.ephoenix.ir/",
        "User-Agent": "Mozilla/5.0",
    }

    session_id = session_auth.get("sessionid")
    third_party = session_auth.get("thirdpartytoken")
    access_token = (
        session_auth.get("accesstoken")
        or session_auth.get("token")
        or session_auth.get("jwt")
    )
    authorization = session_auth.get("authorization")

    if session_id:
        headers["sessionId"] = session_id
    if third_party:
        headers["thirdPartyToken"] = third_party
    if authorization:
        headers["Authorization"] = authorization
    elif access_token:
        headers["Authorization"] = (
            access_token
            if access_token.lower().startswith("bearer ")
            else "Bearer " + access_token
        )

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=20) as response:
            status = response.status
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(200_000)
    except Exception:
        return {
            "ok": False,
            "status": 0,
            "session_ready": bool(session_auth or list(cookies)),
            "message": "ارتباط با سرویس سفارش‌های دانا برقرار نشد.",
        }

    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw else None
    except Exception:
        data = None

    session_ready = bool(session_auth or list(cookies))
    if status < 200 or status >= 300:
        message = None
        if isinstance(data, dict):
            message = data.get("Message") or data.get("message")
        if not message:
            message = "دانا درخواست را نپذیرفت (HTTP " + str(status) + ")."
        return {
            "ok": False,
            "status": status,
            "session_ready": session_ready,
            "message": str(message)[:240],
        }

    if data is None:
        return {
            "ok": False,
            "status": status,
            "session_ready": session_ready,
            "message": "پاسخ سرویس سفارش‌های دانا JSON معتبر نیست.",
        }

    return {
        "ok": True,
        "status": status,
        "session_ready": session_ready,
        "data": data,
    }

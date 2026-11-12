"""Read-only helpers for the Dana brokerage order API.

Authentication material is kept in the server process only and is never
returned to the browser or written to disk.
"""
import json
import re
import urllib.error
import urllib.request
import uuid


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


def login_succeeded(payload):
    """Return True only when Dana explicitly marks the login as successful."""
    if not isinstance(payload, dict):
        return False
    value = payload.get("isSuccess")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")



def _sanitize_upstream_error(message, session_auth, cookies):
    """Keep useful upstream diagnostics while removing authentication secrets."""
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    text = str(message)

    text = re.sub(r"(?i)\bBearer\s+[^\s,;}\]]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?im)\b(cookie|set-cookie)\s*[:=]\s*[^\r\n]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"""(?i)(["']?(?:authorization|access[_-]?token|refresh[_-]?token|token|jwt|session[_-]?id|third[_-]?party[_-]?token|password)["']?\s*[:=]\s*["']?)[^"'\s,;}\]]+""",
        r"\1[redacted]",
        text,
    )

    for value in (session_auth or {}).values():
        value = str(value)
        if len(value) >= 4:
            text = text.replace(value, "[redacted]")
    try:
        for cookie in list(cookies):
            value = str(getattr(cookie, "value", "") or "")
            if len(value) >= 4:
                text = text.replace(value, "[redacted]")
    except Exception:
        pass
    return text[:240]

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
        # Dana order endpoints use the x-sessionid header; keep the legacy
        # casing too for gateway versions that still expect sessionId.
        headers["x-sessionid"] = session_id
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
            message = (
                data.get("Message") or data.get("message")
                or data.get("errorMessage") or data.get("detail")
                or data.get("title") or data.get("error")
            )
        if isinstance(message, (dict, list)):
            message = json.dumps(message, ensure_ascii=False)
        if not message and raw:
            # Some gateways return plain-text diagnostics instead of JSON.
            message = raw.decode("utf-8", errors="replace").strip()
        if message:
            message = _sanitize_upstream_error(message, session_auth, cookies)
        else:
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


def submit_buy_order(opener, cookies, session_auth, isin, price, volume, side="buy"):
    """Send one order to Dana once; uncertain outcomes are never retried."""
    session_id = session_auth.get("sessionid")
    side = str(side or "").strip().lower()
    side_code = {"buy": 1, "sell": 2}.get(side)
    if side_code is None:
        raise ValueError("نوع سفارش معتبر نیست")
    isin = str(isin or "").strip().upper()
    if len(isin) != 12 or not isin.isascii() or not isin.isalnum():
        raise ValueError("شناسه ISIN معتبر نیست")
    try:
        price = int(price)
        volume = int(volume)
    except (TypeError, ValueError) as exc:
        raise ValueError("قیمت و تعداد باید عدد صحیح باشند") from exc
    if price <= 0 or volume <= 0:
        raise ValueError("قیمت و تعداد باید مثبت باشند")
    session_ready = bool(session_auth or list(cookies))
    if not session_id:
        return {"ok": False, "not_sent": True, "status": 0,
                "session_ready": session_ready,
                "message": "نشست دانا موجود نیست؛ سفارش ارسال نشد."}

    url = "https://api-dana.ephoenix.ir/api/v2/orders/NewOrder"
    origin = "https://dana.ephoenix.ir"
    request_id = str(uuid.uuid4())
    preflight = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*", "Origin": origin, "Referer": origin + "/",
            "User-Agent": "Mozilla/5.0",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-request-id,x-sessionid",
        },
        method="OPTIONS",
    )
    try:
        with opener.open(preflight, timeout=20) as response:
            preflight_status = response.status
    except urllib.error.HTTPError as exc:
        return {"ok": False, "not_sent": True, "status": exc.code,
                "session_ready": session_ready,
                "message": "مرحله اولیه اتصال ناموفق بود؛ سفارش ارسال نشد."}
    except Exception:
        return {"ok": False, "not_sent": True, "status": 0,
                "session_ready": session_ready,
                "message": "ارتباط اولیه برقرار نشد؛ سفارش ارسال نشد."}
    if not 200 <= preflight_status < 300:
        return {"ok": False, "not_sent": True, "status": preflight_status,
                "session_ready": session_ready,
                "message": "مرحله اولیه نامعتبر بود؛ سفارش ارسال نشد."}

    payload = {
        "validity": 1, "validityDate": None, "price": price,
        "volume": volume, "side": side_code, "isin": isin, "accountType": 1,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": origin, "Referer": origin + "/",
            "User-Agent": "Mozilla/5.0",
            "x-request-id": request_id, "x-sessionid": session_id,
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=25) as response:
            status = response.status
            raw = response.read(200_000)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(200_000)
    except Exception:
        return {"ok": False, "uncertain": True, "status": 0,
                "session_ready": session_ready,
                "message": "پاسخ ثبت سفارش دریافت نشد؛ ممکن است درخواست به دانا رسیده باشد. پیش از هر اقدام سفارش‌های باز را بررسی کن."}

    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if status < 200 or status >= 300:
        message = None
        if isinstance(data, dict):
            message = data.get("Message") or data.get("message") or data.get("errorMessage")
        return {"ok": False, "uncertain": status >= 500, "status": status,
                "session_ready": session_ready,
                "message": str(message or f"دانا درخواست را نپذیرفت (HTTP {status}).")[:240]}
    if data is None:
        return {"ok": False, "uncertain": True, "status": status,
                "session_ready": session_ready,
                "message": "پاسخ ثبت سفارش JSON معتبر نبود؛ سفارش‌های باز را بررسی کن."}

    success_flag = None
    if isinstance(data, dict):
        for key in ("isSuccess", "success", "ok"):
            value = data.get(key)
            if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
                success_flag = True
                break
            if value is False or (isinstance(value, str) and value.strip().lower() == "false"):
                success_flag = False
                break
    if success_flag is True:
        return {"ok": True, "status": status, "session_ready": session_ready,
                "message": f"دانا ثبت سفارش {'خرید' if side == 'buy' else 'فروش'} را تأیید کرد."}
    if success_flag is False:
        message = None
        if isinstance(data, dict):
            message = data.get("Message") or data.get("message") or data.get("errorMessage")
        return {"ok": False, "status": status, "session_ready": session_ready,
                "message": str(message or "دانا سفارش را نپذیرفت.")[:240]}
    return {"ok": False, "uncertain": True, "status": status,
            "session_ready": session_ready,
            "message": "پاسخ دانا تأیید صریح ثبت سفارش نداشت؛ وضعیت سفارش‌های باز را بررسی کن."}

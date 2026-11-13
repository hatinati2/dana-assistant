#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/dana-monitor"
RAW="https://raw.githubusercontent.com/hatinati2/dana-assistant/main"

cd "$APP_DIR"
test -x "$APP_DIR/.venv/bin/python" || { echo "Python virtualenv not found: $APP_DIR/.venv"; exit 1; }
test -f "$APP_DIR/login_portal.py" || { echo "login_portal.py not found"; exit 1; }
mkdir -p "$APP_DIR/logs"

# Keep a server-local batch interval override. Existing installations inherit
# their current dashboard value once; clean installations default to 1000 ms.
INTERVAL_CONFIG="/etc/dana-assistant-batch-interval-ms"
if [[ ! -f "$INTERVAL_CONFIG" ]]; then
  INITIAL_INTERVAL="1000"
  if [[ -f "$APP_DIR/dashboard.html" ]]; then
    DETECTED_INTERVAL="$(python3 - "$APP_DIR/dashboard.html" <<'PY'
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
    match = re.search(r'<input id="batch-interval-ms"[^>]*\bvalue="(\d+)"', text)
    print(match.group(1) if match else "")
except Exception:
    print("")
PY
)"
    if [[ "$DETECTED_INTERVAL" =~ ^[0-9]+$ ]] && (( DETECTED_INTERVAL >= 1 && DETECTED_INTERVAL <= 1000 )); then
      INITIAL_INTERVAL="$DETECTED_INTERVAL"
    fi
  fi
  printf '%s\n' "$INITIAL_INTERVAL" > "$INTERVAL_CONFIG"
  chmod 0600 "$INTERVAL_CONFIG"
fi

BATCH_INTERVAL="$(tr -d '[:space:]' < "$INTERVAL_CONFIG")"
if [[ ! "$BATCH_INTERVAL" =~ ^[0-9]+$ ]] || (( BATCH_INTERVAL < 1 || BATCH_INTERVAL > 1000 )); then
  echo "فاصله نوبت‌های محلی باید عدد صحیحی بین ۱ تا ۱۰۰۰ میلی‌ثانیه باشد: $INTERVAL_CONFIG" >&2
  exit 1
fi

# Remove only the panel-specific self-signed certificate created by the previous installer.
rm -f /etc/ssl/certs/dana-panel.crt /etc/ssl/private/dana-panel.key

curl -fsSL "$RAW/dashboard.html" -o "$APP_DIR/dashboard.html.tmp"
python3 - "$APP_DIR/dashboard.html.tmp" "$BATCH_INTERVAL" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
value = sys.argv[2]
text = path.read_text(encoding="utf-8")
pattern = r'(<input id="batch-interval-ms"[^>]*\bvalue=")\d+(")'
updated, count = re.subn(pattern, lambda m: m.group(1) + value + m.group(2), text, count=1)
if count != 1:
    raise SystemExit("ناتوان در اعمال تنظیم فاصله نوبت‌ها روی داشبورد")
path.write_text(updated, encoding="utf-8")
PY
mv "$APP_DIR/dashboard.html.tmp" "$APP_DIR/dashboard.html"
curl -fsSL "$RAW/symbols.py" -o "$APP_DIR/symbols.py.tmp"
mv "$APP_DIR/symbols.py.tmp" "$APP_DIR/symbols.py"
curl -fsSL "$RAW/orders_api.py" -o "$APP_DIR/orders_api.py.tmp"
mv "$APP_DIR/orders_api.py.tmp" "$APP_DIR/orders_api.py"
curl -fsSL "$RAW/orders_store.py" -o "$APP_DIR/orders_store.py.tmp"
mv "$APP_DIR/orders_store.py.tmp" "$APP_DIR/orders_store.py"
mkdir -p "$APP_DIR/tests"
curl -fsSL "$RAW/tests/test_symbols.py" -o "$APP_DIR/tests/test_symbols.py.tmp"
mv "$APP_DIR/tests/test_symbols.py.tmp" "$APP_DIR/tests/test_symbols.py"
curl -fsSL "$RAW/tests/test_orders_api.py" -o "$APP_DIR/tests/test_orders_api.py.tmp"
mv "$APP_DIR/tests/test_orders_api.py.tmp" "$APP_DIR/tests/test_orders_api.py"
curl -fsSL "$RAW/tests/test_orders_store.py" -o "$APP_DIR/tests/test_orders_store.py.tmp"
mv "$APP_DIR/tests/test_orders_store.py.tmp" "$APP_DIR/tests/test_orders_store.py"

python3 - <<'PY'
from pathlib import Path

p = Path.home() / "dana-monitor" / "login_portal.py"
s = p.read_text(encoding="utf-8")
if "from datetime import datetime" not in s:
    s = s.replace("import html\n", "import html\nfrom datetime import datetime\n", 1)

if "import threading\n" not in s:
    s = s.replace("import html\n", "import html\nimport threading\n", 1)


if 'from pathlib import Path' not in s:
    if "import html\n" in s:
        s = s.replace("import html\n", "import html\nfrom pathlib import Path\n", 1)
    else:
        raise SystemExit("Cannot safely add pathlib import")

if "import ipaddress\n" not in s:
    s = s.replace("import html\n", "import html\nimport ipaddress\n", 1)

if "from urllib.parse import parse_qs, urlsplit" not in s:
    s = s.replace(
        "from urllib.parse import parse_qs",
        "from urllib.parse import parse_qs, urlsplit",
        1,
    )

if "from symbols import start_symbol_refresh, suggest_symbols, symbol_catalog_status" not in s:
    marker = "from urllib.parse import parse_qs, urlsplit"
    if marker not in s:
        raise SystemExit("Cannot locate URL parsing import")
    s = s.replace(
        marker,
        marker + "\nfrom symbols import start_symbol_refresh, suggest_symbols, symbol_catalog_status",
        1,
    )

orders_import_marker = "from " + "orders_api import"
if orders_import_marker not in s:
    symbols_import = "from symbols import start_symbol_refresh, suggest_symbols, symbol_catalog_status"
    if symbols_import not in s:
        raise SystemExit("Cannot locate symbols import for orders API")
    s = s.replace(
        symbols_import,
        symbols_import + "\nfrom orders_api import capture_session_auth, fetch_open_orders, login_succeeded, submit_buy_order\nsession_auth = {}\nbroker_login_ok = False",
        1,
    )
else:
    s = s.replace(
        "from orders_api import capture_session_auth, fetch_open_orders, login_succeeded, submit_buy_order\n",
        "from orders_api import capture_session_auth, fetch_open_orders, login_succeeded, submit_buy_order\n",
        1,
    )
    if "broker_login_ok = " + "False" not in s:
        s = s.replace("session_auth = {}", "session_auth = {}\nbroker_login_ok = False", 1)

if "broker_order_lock = threading.Lock()" not in s:
    if "broker_login_ok = False" in s:
        s = s.replace("broker_login_ok = False", "broker_login_ok = False\nbroker_order_lock = threading.Lock()", 1)
    elif "session_auth = {}" in s:
        s = s.replace("session_auth = {}", "session_auth = {}\nbroker_order_lock = threading.Lock()", 1)
    else:
        raise SystemExit("Cannot locate broker session globals for order lock")

orders_store_import = "from orders_store import ScheduledOrderStore, TEHRAN, start_order_scheduler, calendar_data, build_order_plan, format_jalali_datetime"
if orders_store_import not in s:
    marker = "from orders_api import capture_session_auth, fetch_open_orders, login_succeeded, submit_buy_order"
    if marker not in s:
        raise SystemExit("Cannot locate orders API import for local scheduler")
    s = s.replace(marker, marker + "\n" + orders_store_import, 1)

if "order_store = ScheduledOrderStore(" not in s:
    if orders_store_import not in s:
        raise SystemExit("Cannot locate local scheduler import")
    s = s.replace(orders_store_import, orders_store_import + "\norder_store = ScheduledOrderStore(Path(__file__).resolve().parent / \"data\" / \"scheduled_orders.json\")", 1)

json_parse_prefix = "            try:\n                result = json.loads(raw) if raw else None"
# Remove function-local declarations left by earlier installer revisions.
for indent in ("        ", "            ", "                "):
    s = s.replace(indent + "global session_auth, broker_login_ok\n", "")

json_parse_start = s.find(json_parse_prefix)
keys_line = "            keys = sorted(result.keys()) if isinstance(result, dict) else []"
json_parse_end = s.find(keys_line, json_parse_start)
if json_parse_start < 0 or json_parse_end < 0:
    raise SystemExit("Cannot locate login response parser")

login_parse_replacement = """            try:
                result = json.loads(raw) if raw else None
            except Exception:
                result = None

            globals()["session_auth"] = capture_session_auth(result)
            login_ok = login_succeeded(result)
            globals()["broker_login_ok"] = login_ok
            if login_ok:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            login_status = "ناموفق" if isinstance(result, dict) and "isSuccess" in result else "نامشخص"
            error_code = result.get("errorCode") if isinstance(result, dict) else None
            error_message = result.get("errorMessage") if isinstance(result, dict) else None
            login_step = result.get("step") if isinstance(result, dict) else None
            two_factor = result.get("twoFactorAuthentication") if isinstance(result, dict) else None

            keys = sorted(result.keys()) if isinstance(result, dict) else []"""
s = s[:json_parse_start] + login_parse_replacement + s[json_parse_end + len(keys_line):]

if "ورود دومرحله‌ای:" not in s:
    details_marker = "            self.send_html(page(details))"
    details_prefix = '''            details = (
                f"وضعیت ورود: {login_status}<br>"
                f"کد خطا: {html.escape(str(error_code))}<br>"
                f"پیام خطا: {html.escape(str(error_message))}<br>"
                f"مرحله ورود: {html.escape(str(login_step))}<br>"
                f"ورود دومرحله‌ای: {html.escape(str(two_factor))}<br>"
                + details
            )
'''
    if details_marker not in s:
        raise SystemExit("Cannot locate login diagnostic response")
    s = s.replace(details_marker, details_prefix + details_marker, 1)

if "    def send_json(self, data, status=200):" not in s:
    marker = "    def allowed_host(self):"
    json_method = '''    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

'''
    if marker not in s:
        raise SystemExit("Cannot locate Handler.allowed_host")
    s = s.replace(marker, json_method + marker, 1)

host_start = s.find("    def allowed_host(self):")
host_end = s.find("\n    def do_GET(self):", host_start)
if host_start < 0 or host_end < 0:
    raise SystemExit("Could not locate allowed_host")
host_replacement = '''    def allowed_host(self):
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit("//" + self.headers.get("Host", ""))
            return parsed.port == 8109 and bool(ipaddress.ip_address(parsed.hostname or ""))
        except (ValueError, TypeError):
            return False
'''
s = s[:host_start] + host_replacement + s[host_end:]

start = s.find("    def do_GET(self):")
end = s.find("\n    def do_POST(self):", start)
if start < 0 or end < 0:
    raise SystemExit("Could not locate Handler.do_GET")

replacement = '''    def do_GET(self):
        if not self.allowed_host():
            self.send_error(403)
            return

        parsed = urlsplit(self.path)

        if parsed.path == "/api/broker/status":
            self.send_json({
                "connected": bool(broker_login_ok and (session_auth or list(cookies))),
                "login_success": bool(broker_login_ok),
                "cookie_session": bool(list(cookies)),
                "auth_fields": sorted(session_auth.keys()),
                "server_time": datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M:%S %Z"),
            })
            return

        if parsed.path == "/api/server-time":
            now = datetime.now(TEHRAN)
            self.send_json({"unix_millis": int(now.timestamp() * 1000), "timezone": "Asia/Tehran", "jalali_date": format_jalali_datetime(now).split(" ")[0]})
            return

        if parsed.path == "/api/calendar":
            try:
                query = parse_qs(parsed.query)
                year = int(query.get("year", [""])[0])
                month = int(query.get("month", [""])[0])
                self.send_json(calendar_data(year, month))
            except (ValueError, TypeError) as exc:
                self.send_json({"error": str(exc) or "سال و ماه شمسی نامعتبر است"}, 400)
            return

        if parsed.path == "/api/order-plan":
            try:
                query = parse_qs(parsed.query)
                target_ms = int(query.get("target_ms", [""])[0])
                lead_ms = int(query.get("lead_ms", [""])[0])
                interval_ms = int(query.get("interval_ms", [""])[0])
                self.send_json(build_order_plan(target_ms, lead_ms, interval_ms))
            except (ValueError, TypeError) as exc:
                self.send_json({"error": str(exc) or "پارامترهای برنامه‌ریز نامعتبر است"}, 400)
            return

        if parsed.path == "/api/orders/local":
            view = parse_qs(parsed.query).get("view", ["active"])[0]
            if view not in ("active", "history"):
                self.send_json({"error": "نمای سفارش‌ها نامعتبر است"}, 400)
                return
            rows = order_store.list_orders(view)
            self.send_json({"ok": True, "view": view, "count": len(rows), "orders": rows})
            return

        if parsed.path == "/api/orders/open":
            try:
                self.send_json(fetch_open_orders(opener, cookies, session_auth))
            except Exception:
                self.send_json({"ok": False, "status": 0, "message": "دریافت سفارش‌های فعال ناموفق بود."}, 502)
            return

        if parsed.path == "/api/symbols/suggest":
            query = parse_qs(parsed.query).get("q", [""])[0]
            if not query.strip():
                self.send_json({"error": "برای جست‌وجوی نماد حداقل یک حرف وارد کن."}, 400)
                return
            try:
                self.send_json(suggest_symbols(query))
            except Exception:
                self.send_json({"error": "جست‌وجوی نماد در حال حاضر در دسترس نیست."}, 502)
            return

        if parsed.path == "/api/symbols/status":
            self.send_json(symbol_catalog_status())
            return

        if parsed.path == "/":
            try:
                dashboard = Path(__file__).resolve().with_name("dashboard.html")
                self.send_html(dashboard.read_text(encoding="utf-8"))
            except Exception:
                self.send_error(500, "DashboardUnavailable")
            return

        if parsed.path == "/login":
            self.send_html(page())
            return

        self.send_error(404)
'''
s = s[:start] + replacement + s[end:]


if "login_status = \"ناموفق\"" in s and "ورود دومرحله‌ای:" not in s:
    marker = "            self.send_html(page(details))"
    details_prefix = '''            details = (
                f"وضعیت ورود: {login_status}<br>"
                f"کد خطا: {html.escape(str(error_code))}<br>"
                f"پیام خطا: {html.escape(str(error_message))}<br>"
                f"مرحله ورود: {html.escape(str(login_step))}<br>"
                f"ورود دومرحله‌ای: {html.escape(str(two_factor))}<br>"
                + details
            )
'''
    if marker not in s:
        raise SystemExit("Cannot locate login diagnostic response")
    s = s.replace(marker, details_prefix + marker, 1)

post_marker = """    def do_POST(self):
        if not self.allowed_host():
            self.send_error(403)
            return
"""
if 'api_path.startswith("/api/orders/")' not in s:
    post_addition = '''        api_path = urlsplit(self.path).path
        if api_path.startswith("/api/orders/"):
            origin = self.headers.get("Origin", "")
            if origin:
                try:
                    if urlsplit(origin).netloc.lower() != self.headers.get("Host", "").lower():
                        self.send_json({"ok": False, "error": "درخواست از مبدأ نامعتبر پذیرفته نشد."}, 403)
                        return
                except ValueError:
                    self.send_json({"ok": False, "error": "مبدأ درخواست نامعتبر است."}, 403)
                    return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 262144:
                self.send_json({"ok": False, "error": "حجم درخواست سفارش معتبر نیست."}, 400)
                return
            try:
                body = self.rfile.read(length).decode("utf-8")
                parsed_fields = parse_qs(body, keep_blank_values=True)
                form = {key: values[0] for key, values in parsed_fields.items()}
                if api_path == "/api/orders/schedule":
                    if form.get("execution_mode") == "auto_order":
                        if not origin or form.get("confirmed") != "yes":
                            self.send_json({"ok": False, "error": "تأیید سفارش واقعی زمان‌بندی‌شده معتبر نیست."}, 400)
                            return
                        if form.get("side") not in ("buy", "sell") or form.get("validity", "day") != "day":
                            self.send_json({"ok": False, "error": "نوع یا اعتبار سفارش واقعی معتبر نیست."}, 400)
                            return
                        if not broker_login_ok or not session_auth.get("sessionid"):
                            self.send_json({"ok": False, "error": "ابتدا وارد دانا شو تا سفارش واقعی زمان‌بندی شود."}, 401)
                            return
                    result = order_store.create_order(form)
                    self.send_json({"ok": True, "order": result}, 201)
                elif api_path == "/api/orders/batch":
                    if form.get("execution_mode", "manual") == "auto_order":
                        if not origin or form.get("confirmed") != "yes":
                            self.send_json({"ok": False, "error": "تأیید ارسال واقعی دسته‌ای معتبر نیست؛ صفحه را تازه‌سازی و دوباره تأیید کن."}, 400)
                            return
                        if form.get("side") not in ("buy", "sell") or form.get("validity", "day") != "day":
                            self.send_json({"ok": False, "error": "نوع یا اعتبار سفارش دسته‌ای معتبر نیست."}, 400)
                            return
                        if not broker_login_ok or not session_auth.get("sessionid"):
                            self.send_json({"ok": False, "error": "برای زمان‌بندی ارسال واقعی دسته‌ای، ابتدا وارد حساب دانا شو."}, 401)
                            return
                    result = order_store.create_batch(form)
                    self.send_json({"ok": True, **result}, 201)
                elif api_path == "/api/orders/cancel":
                    order_store.cancel(form.get("id", ""))
                    self.send_json({"ok": True})
                elif api_path == "/api/orders/manual-confirm":
                    order_store.confirm_manual(form.get("id", ""))
                    self.send_json({"ok": True})
                elif api_path == "/api/orders/cancel-all":
                    self.send_json({"ok": True, "count": order_store.cancel_all_scheduled()})
                elif api_path == "/api/orders/history/clear":
                    self.send_json({"ok": True, "count": order_store.clear_history()})
                else:
                    self.send_json({"ok": False, "error": "مسیر سفارش محلی پیدا نشد."}, 404)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            except Exception:
                self.send_json({"ok": False, "error": "ذخیره یا مدیریت سفارش انجام نشد."}, 500)
            return

'''
    if post_marker not in s:
        raise SystemExit("Cannot locate Handler.do_POST host guard")
    s = s.replace(post_marker, post_marker + post_addition, 1)

# Rebuild the complete scheduler/server tail from its first generated
# function. Older installer revisions appended another copy on every run.
http_import = "from http.server import BaseHTTPRequestHandler, HTTPServer"
threaded_import = "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer"
if http_import in s:
    s = s.replace(http_import, threaded_import, 1)
elif threaded_import not in s:
    raise SystemExit("Cannot locate HTTP server import")

startup_start = s.find("\\ndef submit_scheduled_order(order):")
if startup_start < 0:
    startup_start = s.find('print("Login portal listening')
if startup_start < 0:
    raise SystemExit("Could not locate panel scheduler/server section")
s = s[:startup_start] + '''def submit_scheduled_order(order):
    """Submit each due order once; unknown outcomes are not retried."""
    if order.get("side") not in ("buy", "sell"):
        return {"not_sent": True, "message": "نوع سفارش معتبر نیست؛ سفارش ارسال نشد."}
    with broker_order_lock:
        if not broker_login_ok or not session_auth.get("sessionid"):
            return {"not_sent": True, "status": 0, "message": "در زمان اجرا نشست معتبر دانا موجود نبود؛ سفارش ارسال نشد."}
        try:
            return submit_buy_order(
                opener, cookies, session_auth,
                order.get("isin"), order.get("price"), order.get("quantity"),
                side=order.get("side"),
            )
        except ValueError as exc:
            return {"not_sent": True, "message": str(exc)[:240]}
        except Exception:
            return {"uncertain": True, "message": "نتیجه ارسال مشخص نیست؛ برای جلوگیری از سفارش تکراری، دوباره ارسال نمی‌شود."}

start_symbol_refresh()
start_order_scheduler(order_store, submit_auto_order=submit_scheduled_order)
print("Login portal listening on 0.0.0.0:8109")
ThreadingHTTPServer(("0.0.0.0", 8109), Handler).serve_forever()
'''
# Remove every global declaration that mentions broker session state.
# The login handler writes these values through globals(), so no declaration is needed.
cleaned_lines = []
for line in s.splitlines(keepends=True):
    stripped = line.lstrip()
    parts = stripped.split(None, 1)
    is_global = bool(parts and parts[0] == "global")
    touches_session = "session_auth" in stripped or "broker_login_ok" in stripped
    if not (is_global and touches_session):
        cleaned_lines.append(line)
s = "".join(cleaned_lines)
for line in s.splitlines():
    stripped = line.lstrip()
    parts = stripped.split(None, 1)
    if parts and parts[0] == "global" and ("session_auth" in stripped or "broker_login_ok" in stripped):
        raise SystemExit("Unsafe global declaration remains in login_portal.py")
compile(s, str(p), "exec")
p.write_text(s, encoding="utf-8")
PY
"$APP_DIR/.venv/bin/python" -m py_compile "$APP_DIR/login_portal.py" "$APP_DIR/symbols.py" "$APP_DIR/orders_api.py" "$APP_DIR/orders_store.py"
"$APP_DIR/.venv/bin/python" -m unittest discover -s "$APP_DIR/tests" -v

# Stop and restart this panel through systemd; never detach it from its service.
if systemctl cat dana-assistant.service >/dev/null 2>&1; then
  if ! systemctl cat dana-assistant.service | grep -Fq "$APP_DIR/.venv/bin/python $APP_DIR/login_portal.py"; then
    echo "سرویس dana-assistant.service متعلق به مسیر مورد انتظار نیست؛ عملیات متوقف شد." >&2
    exit 1
  fi
else
  cat > /etc/systemd/system/dana-assistant.service <<EOF
[Unit]
Description=Dana Assistant Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/login_portal.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
systemctl enable dana-assistant.service >/dev/null
systemctl restart dana-assistant.service
sleep 2
systemctl is-active --quiet dana-assistant.service || {
  systemctl --no-pager --full status dana-assistant.service || true
  journalctl -u dana-assistant.service -n 40 --no-pager || true
  exit 1
}
ss -lntp | grep ':8109'

# Open the port in UFW only when UFW is already active.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 8109/tcp >/dev/null
  echo "UFW rule added for TCP 8109."
else
  echo "UFW is not active; check the VPS provider firewall/security group for TCP 8109."
fi

PUBLIC_IP="$(curl -4fsS --max-time 8 https://api.ipify.org || true)"
if [ -n "$PUBLIC_IP" ]; then
  echo "DIRECT PANEL URL: http://$PUBLIC_IP:8109"
else
  echo "Could not detect public IPv4. Use your VPS public IP with port 8109 over HTTP."
fi
echo "Panel is running on plain HTTP as requested."

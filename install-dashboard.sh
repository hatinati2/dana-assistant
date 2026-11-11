#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/dana-monitor"
RAW="https://raw.githubusercontent.com/hatinati2/dana-assistant/main"

cd "$APP_DIR"
test -x "$APP_DIR/.venv/bin/python" || { echo "Python virtualenv not found: $APP_DIR/.venv"; exit 1; }
test -f "$APP_DIR/login_portal.py" || { echo "login_portal.py not found"; exit 1; }
mkdir -p "$APP_DIR/logs"

curl -fsSL "$RAW/dashboard.html" -o "$APP_DIR/dashboard.html.tmp"
mv "$APP_DIR/dashboard.html.tmp" "$APP_DIR/dashboard.html"

python3 - <<'PY'
from pathlib import Path
import re

p = Path.home() / "dana-monitor" / "login_portal.py"
s = p.read_text(encoding="utf-8")

if 'from pathlib import Path' not in s:
    if "import html\n" in s:
        s = s.replace("import html\n", "import html\nfrom pathlib import Path\n", 1)
    else:
        raise SystemExit("Cannot safely add pathlib import")

if "import ipaddress\n" not in s:
    s = s.replace("import html\n", "import html\nimport ipaddress\n", 1)

host_start = s.find("    def allowed_host(self):")
host_end = s.find("\n    def do_GET(self):", host_start)
if host_start < 0 or host_end < 0:
    raise SystemExit("Could not locate allowed_host; original file left in place")
host_replacement = '''    def allowed_host(self):
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit("//" + self.headers.get("Host", ""))
            return parsed.port == 8109 and bool(ipaddress.ip_address(parsed.hostname or ""))
        except (ValueError, TypeError):
            return False
'''
s = s[:host_start] + host_replacement + s[host_end:]
# Replace any previous loopback or TLS startup block with plain HTTP.
startup_start = s.find('print("Login portal listening')
if startup_start < 0:
    raise SystemExit("Could not locate panel server startup; installer stopped")
s = s[:startup_start] + '''print("Login portal listening on 0.0.0.0:8109")
HTTPServer(("0.0.0.0", 8109), Handler).serve_forever()
'''
s = s.replace("import ssl", "")

start = s.find("    def do_GET(self):")
end = s.find("\n    def do_POST(self):", start)
if start < 0 or end < 0:
    raise SystemExit("Could not locate Handler.do_GET; original file left in place")

replacement = '''    def do_GET(self):
        if not self.allowed_host():
            self.send_error(403)
            return

        if self.path == "/":
            try:
                dashboard = Path(__file__).resolve().with_name("dashboard.html")
                self.send_html(dashboard.read_text(encoding="utf-8"))
            except Exception:
                self.send_error(500, "DashboardUnavailable")
            return

        if self.path == "/login":
            self.send_html(page())
            return

        self.send_error(404)
'''
s = s[:start] + replacement + s[end:]
p.write_text(s, encoding="utf-8")
PY

"$APP_DIR/.venv/bin/python" -m py_compile "$APP_DIR/login_portal.py"

# Stop only the Python process currently listening on the panel port.
python3 - <<'PY'
import os, re, signal, subprocess, time
try:
    output = subprocess.check_output(["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL)
except Exception:
    output = ""
pids = set(re.findall(r"pid=(\d+)", "\n".join(
    line for line in output.splitlines() if ":8109 " in line
)))
for value in pids:
    pid = int(value)
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore")
        if "login_portal.py" in cmd:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
for _ in range(20):
    time.sleep(0.1)
    check = subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout
    if ":8109 " not in check:
        break
PY

cd "$APP_DIR"
nohup "$APP_DIR/.venv/bin/python" login_portal.py >> logs/login_portal.log 2>&1 </dev/null &
sleep 1
ss -ltnp | grep ':8109'

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

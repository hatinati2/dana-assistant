"""Persistent, local-only order scheduling inspired by Daya Assistant.

This module never sends orders to a broker. It stores manual order plans,
moves due plans to a ready-for-review state, and supports history operations.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover - fallback for minimal systems
    TEHRAN = timezone(timedelta(hours=3, minutes=30), name="IRST")

JALALI_BREAKS = (
    -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181,
    1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394,
    2456, 3178,
)
JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
MAX_BATCH_ORDERS = 5000
MAX_LEAD_MS = 3_600_000
MAX_INTERVAL_MS = 60_000
SYMBOL_RE = re.compile(r"^[\w.\-]{1,24}$", re.UNICODE)
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def normalize_digits(value: str) -> str:
    return str(value or "").translate(DIGITS).strip()


def _div(a: int, b: int) -> int:
    # Match Go integer division: truncate toward zero.
    return int(a / b)


def _mod(a: int, b: int) -> int:
    # Match Go remainder: sign follows the dividend.
    return a - _div(a, b) * b


def _jal_cal(jy: int) -> tuple[int, int, int]:
    if jy < JALALI_BREAKS[0] or jy >= JALALI_BREAKS[-1]:
        raise ValueError("سال شمسی خارج از محدوده معتبر است")
    gy = jy + 621
    leap_j = -14
    jp = JALALI_BREAKS[0]
    jump = 0
    for jm in JALALI_BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n -= jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def is_jalali_leap_year(year: int) -> bool:
    leap, _, _ = _jal_cal(year)
    return leap == 0


def jalali_month_length(year: int, month: int) -> int:
    if year < -61 or year > 3177 or month < 1 or month > 12:
        raise ValueError("سال یا ماه شمسی نامعتبر است")
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    return 30 if is_jalali_leap_year(year) else 29


def is_valid_jalali_date(year: int, month: int, day: int) -> bool:
    try:
        return 1 <= day <= jalali_month_length(year, month)
    except ValueError:
        return False


def _g2d(gy: int, gm: int, gd: int) -> int:
    d = _div((gy + _div(gm - 8, 6) + 100100) * 1461, 4)
    d += _div(153 * _mod(gm + 9, 12) + 2, 5)
    d += gd - 34840408
    d -= _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4)
    d += 752
    return d


def _d2g(jdn: int) -> tuple[int, int, int]:
    j = 4 * jdn + 139361631
    j += _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908
    i = _div(_mod(j, 1461), 4) * 5 + 308
    gd = _div(_mod(i, 153), 5) + 1
    gm = _mod(_div(i, 153), 12) + 1
    gy = _div(j, 1461) - 100100 + _div(8 - gm, 6)
    return gy, gm, gd


def jalali_to_gregorian(year: int, month: int, day: int) -> tuple[int, int, int]:
    if not is_valid_jalali_date(year, month, day):
        raise ValueError("تاریخ شمسی معتبر نیست")
    _, gy, march = _jal_cal(year)
    jdn = _g2d(gy, 3, march) + (month - 1) * 31 - _div(month, 7) * (month - 7) + day - 1
    return _d2g(jdn)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    jdn = _g2d(gy, gm, gd)
    gy2, _, _ = _d2g(jdn)
    jy = gy2 - 621
    leap, _, march = _jal_cal(jy)
    jdn1f = _g2d(gy2, 3, march)
    k = jdn - jdn1f
    if k >= 0:
        if k <= 185:
            jm = 1 + _div(k, 31)
            jd = _mod(k, 31) + 1
            return jy, jm, jd
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:
            k += 1
    jm = 7 + _div(k, 30)
    jd = _mod(k, 30) + 1
    return jy, jm, jd


def parse_jalali_datetime(date_text: str, time_text: str) -> datetime:
    date_parts = re.split(r"[/.\-]", normalize_digits(date_text))
    if len(date_parts) != 3:
        raise ValueError("فرمت تاریخ باید مانند ۱۴۰۵/۰۸/۱۴ باشد")
    try:
        year, month, day = (int(part) for part in date_parts)
    except Exception as exc:
        raise ValueError("تاریخ شمسی نامعتبر است") from exc
    if not is_valid_jalali_date(year, month, day):
        raise ValueError("تاریخ شمسی معتبر نیست")

    raw_time = normalize_digits(time_text)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?", raw_time)
    if not match:
        raise ValueError("ساعت باید به شکل ساعت:دقیقه یا ساعت:دقیقه:ثانیه باشد")
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or "0")
    fraction = (match.group(4) or "").ljust(3, "0")
    millis = int(fraction or "0")
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("ساعت واردشده معتبر نیست")
    gy, gm, gd = jalali_to_gregorian(year, month, day)
    return datetime(gy, gm, gd, hour, minute, second, millis * 1000, tzinfo=TEHRAN)


def format_jalali_datetime(value: str | datetime) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    local = value.astimezone(TEHRAN)
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    millis = local.microsecond // 1000
    return (
        f"{jy:04d}/{jm:02d}/{jd:02d} "
        f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}.{millis:03d}"
    )


def calendar_data(year: int, month: int) -> dict[str, Any]:
    days = jalali_month_length(year, month)
    gy, gm, gd = jalali_to_gregorian(year, month, 1)
    # Saturday is column zero, matching the Daya calendar.
    first_weekday = (datetime(gy, gm, gd).weekday() + 2) % 7
    return {
        "year": year,
        "month": month,
        "month_name": JALALI_MONTHS[month - 1],
        "days": days,
        "first_weekday": first_weekday,
    }


def format_clock(milliseconds: int) -> str:
    hour = milliseconds // 3_600_000
    minute = (milliseconds // 60_000) % 60
    second = (milliseconds // 1_000) % 60
    millis = milliseconds % 1_000
    return f"{hour:02d}:{minute:02d}:{second:02d}.{millis:03d}"


def build_order_plan(target_ms: int, lead_ms: int, interval_ms: int) -> dict[str, Any]:
    day_ms = 24 * 60 * 60 * 1000
    if target_ms < 0 or target_ms >= day_ms:
        raise ValueError("ساعت هدف نامعتبر است")
    if lead_ms < 1 or lead_ms > MAX_LEAD_MS:
        raise ValueError("زمان شروع باید بین یک میلی‌ثانیه تا یک ساعت باشد")
    if interval_ms < 1 or interval_ms > MAX_INTERVAL_MS:
        raise ValueError("فاصله نوبت‌ها باید بین یک میلی‌ثانیه تا یک دقیقه باشد")
    if target_ms < lead_ms:
        raise ValueError("شروع برنامه از نیمه‌شب عبور می‌کند؛ تاریخ هدف را اصلاح کن")
    count = (lead_ms + interval_ms - 1) // interval_ms
    if count < 1 or count > MAX_BATCH_ORDERS:
        raise ValueError(f"برنامه از سقف {MAX_BATCH_ORDERS} نوبت بیشتر است")
    start_ms = target_ms - lead_ms
    slots = []
    for index in range(count):
        slot_ms = start_ms + index * interval_ms
        if slot_ms >= target_ms:
            break
        slots.append({"number": index + 1, "time": format_clock(slot_ms), "milliseconds": slot_ms})
    if not slots:
        raise ValueError("برنامه نوبتی خالی است")
    return {
        "target": format_clock(target_ms),
        "lead_ms": lead_ms,
        "interval_ms": interval_ms,
        "count": len(slots),
        "first": slots[0]["time"],
        "last": slots[-1]["time"],
        "slots": slots,
    }


def allocate_batch_quantities(quantity: int, count: int, mode: str) -> list[int]:
    if quantity <= 0 or count < 1 or count > MAX_BATCH_ORDERS:
        raise ValueError("تعداد سهم یا تعداد سفارش معتبر نیست")
    if mode == "each":
        return [quantity] * count
    if mode != "total":
        raise ValueError("روش تعیین تعداد سهم معتبر نیست")
    if quantity < count:
        raise ValueError("تعداد کل سهم باید حداقل به اندازه تعداد سفارش‌ها باشد")
    base, remainder = divmod(quantity, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _common_order(fields: dict[str, Any]) -> dict[str, Any]:
    symbol = str(fields.get("symbol", "")).strip()
    side = str(fields.get("side", "buy")).strip().lower()
    isin = str(fields.get("isin", "")).strip().upper()
    try:
        price = int(str(fields.get("price", "")).strip())
        quantity = int(str(fields.get("quantity", "")).strip())
    except Exception as exc:
        raise ValueError("قیمت و تعداد باید عدد صحیح باشند") from exc
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("نماد نامعتبر است")
    if side not in ("buy", "sell"):
        raise ValueError("نوع سفارش نامعتبر است")
    if price <= 0 or quantity <= 0:
        raise ValueError("قیمت و تعداد باید مثبت باشند")
    if isin and not ISIN_RE.fullmatch(isin):
        raise ValueError("شناسه ISIN معتبر نیست")
    return {
        "symbol": symbol,
        "isin": isin,
        "side": side,
        "price": price,
        "quantity": quantity,
        "execution_mode": "manual",
    }


class ScheduledOrderStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._orders: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            return
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("ساختار فایل سفارش‌های زمان‌بندی‌شده معتبر نیست")
        self._orders = [row for row in data if isinstance(row, dict)]

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=".orders-", suffix=".tmp", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._orders, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _new_record(self, common: dict[str, Any], execute_at: datetime, now: datetime) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            **common,
            "execute_at": execute_at.astimezone(timezone.utc).isoformat(),
            "status": "scheduled",
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "updated_at": now.astimezone(timezone.utc).isoformat(),
        }

    def create_order(self, fields: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
        common = _common_order(fields)
        execute_at = parse_jalali_datetime(str(fields.get("execute_at_date", "")), str(fields.get("execute_time", "")))
        if execute_at <= now:
            raise ValueError("زمان اجرا باید در آینده باشد")
        record = self._new_record(common, execute_at, now)
        with self._lock:
            self._orders.append(record)
            try:
                self._save_locked()
            except Exception:
                self._orders.pop()
                raise
        return self._public_record(record)

    def create_batch(self, fields: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
        common = _common_order(fields)
        target = parse_jalali_datetime(str(fields.get("execute_at_date", "")), str(fields.get("execute_time", "")))
        if target <= now:
            raise ValueError("زمان هدف باید در آینده باشد")
        try:
            lead_ms = int(round(float(str(fields.get("batch_lead_seconds", "10"))) * 1000))
            interval_ms = int(str(fields.get("batch_interval_ms", "200")))
        except Exception as exc:
            raise ValueError("تنظیمات فاصله زمانی معتبر نیست") from exc
        midnight = target.replace(hour=0, minute=0, second=0, microsecond=0)
        target_ms = int((target - midnight).total_seconds() * 1000)
        plan = build_order_plan(target_ms, lead_ms, interval_ms)
        quantities = allocate_batch_quantities(
            common["quantity"], plan["count"], str(fields.get("batch_quantity_mode", "each"))
        )
        slot_datetimes = [midnight + timedelta(milliseconds=slot["milliseconds"]) for slot in plan["slots"]]
        if slot_datetimes[0] <= now:
            raise ValueError("اولین نوبت برنامه گذشته است؛ زمان هدف یا بازه شروع را جلوتر ببر")
        records = [
            self._new_record({**common, "quantity": quantities[index]}, slot_datetimes[index], now)
            for index in range(plan["count"])
        ]
        with self._lock:
            previous_length = len(self._orders)
            self._orders.extend(records)
            try:
                self._save_locked()
            except Exception:
                del self._orders[previous_length:]
                raise
        return {
            "count": len(records),
            "first": format_jalali_datetime(records[0]["execute_at"]),
            "last": format_jalali_datetime(records[-1]["execute_at"]),
            "plan": plan,
        }

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        output = dict(record)
        output["execute_at_jalali"] = format_jalali_datetime(record["execute_at"])
        output["created_at_jalali"] = format_jalali_datetime(record["created_at"])
        return output

    def list_orders(self, view: str = "active") -> list[dict[str, Any]]:
        with self._lock:
            if view == "history":
                rows = [row for row in self._orders if row.get("status") in ("cancelled", "manual_done")]
                rows.sort(key=lambda row: row.get("updated_at", ""), reverse=True)
            else:
                rows = [row for row in self._orders if row.get("status") in ("scheduled", "ready")]
                rows.sort(key=lambda row: row.get("execute_at", ""))
            return [self._public_record(dict(row)) for row in rows]

    def mark_due(self, now: datetime | None = None) -> int:
        now = (now or datetime.now(TEHRAN)).astimezone(timezone.utc)
        changed = 0
        with self._lock:
            previous = [dict(row) for row in self._orders]
            for row in self._orders:
                if row.get("status") != "scheduled":
                    continue
                try:
                    due = datetime.fromisoformat(row["execute_at"]) <= now
                except Exception:
                    continue
                if due:
                    row["status"] = "ready"
                    row["updated_at"] = now.isoformat()
                    changed += 1
            if changed:
                try:
                    self._save_locked()
                except Exception:
                    self._orders = previous
                    raise
        return changed

    def cancel(self, order_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, row in enumerate(self._orders):
                if row.get("id") == order_id:
                    if row.get("status") not in ("scheduled", "ready"):
                        raise ValueError("فقط سفارش زمان‌بندی‌شده یا آماده بررسی قابل لغو است")
                    previous = dict(row)
                    row["status"] = "cancelled"
                    row["updated_at"] = now
                    try:
                        self._save_locked()
                    except Exception:
                        self._orders[index] = previous
                        raise
                    return
        raise ValueError("سفارش پیدا نشد")

    def cancel_all_scheduled(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self._lock:
            previous = [dict(row) for row in self._orders]
            for row in self._orders:
                if row.get("status") == "scheduled":
                    row["status"] = "cancelled"
                    row["updated_at"] = now
                    count += 1
            if count:
                try:
                    self._save_locked()
                except Exception:
                    self._orders = previous
                    raise
        return count

    def confirm_manual(self, order_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, row in enumerate(self._orders):
                if row.get("id") == order_id:
                    if row.get("status") != "ready":
                        raise ValueError("فقط سفارش آماده بررسی را می‌توان تأیید کرد")
                    previous = dict(row)
                    row["status"] = "manual_done"
                    row["updated_at"] = now
                    try:
                        self._save_locked()
                    except Exception:
                        self._orders[index] = previous
                        raise
                    return
        raise ValueError("سفارش پیدا نشد")

    def clear_history(self) -> int:
        with self._lock:
            old = self._orders
            self._orders = [row for row in old if row.get("status") not in ("cancelled", "manual_done")]
            removed = len(old) - len(self._orders)
            if removed:
                try:
                    self._save_locked()
                except Exception:
                    self._orders = old
                    raise
            return removed


def start_order_scheduler(store: ScheduledOrderStore, interval: float = 0.25) -> threading.Thread:
    def run() -> None:
        while True:
            try:
                store.mark_due()
            except Exception:
                # A later pass retries persistence; no broker operation is involved.
                pass
            time.sleep(max(0.1, interval))

    thread = threading.Thread(target=run, name="dana-order-scheduler", daemon=True)
    thread.start()
    return thread

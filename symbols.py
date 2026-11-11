"""Read-only Iranian market symbol catalogue adapted from Daya Assistant.

The catalogue and suggestion service use Tehran Stock Exchange public instrument
endpoints. It never submits orders or authenticates to a broker.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://webgw.tse.ir"
CASH_PATH = "/InstrumentProvider/api/v1/MarketWatch/MarketWatchCash/fa"
ETF_PATH = "/InstrumentProvider/api/v1/MarketWatch/MarketWatchEtf/fa"
SHORTCUT_PATH = "/InstrumentProvider/api/v1/Instrument/InstrumentShortcut/fa"
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
LIMIT = 12
CACHE_FILE = Path(__file__).resolve().parent / "data" / "market_symbols.json"
MAX_BODY = 16 * 1024 * 1024
_lock = threading.RLock()
_symbols: list[dict] = []
_updated_at = ""
_last_error = ""
_loaded = False
_refresh_started = False
_query_cache: dict[str, tuple[float, list[dict]]] = {}


def normalize_symbol(value: str) -> str:
    value = str(value or "").strip()
    return value.replace("ي", "ی").replace("ك", "ک").replace("\u200c", "").lower()


def valid_isin(value: str) -> bool:
    return bool(ISIN_RE.fullmatch(str(value or "").strip().upper()))


def _request_json(path: str, params: dict[str, str] | None = None, timeout: float = 4.0):
    url = BASE_URL + path
    if params:
        url += "?" + urlencode(params)
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.tse.ir/",
            "Origin": "https://www.tse.ir",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"TSE HTTP {response.status}")
        body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise RuntimeError("پاسخ فهرست نمادها بیش از حد مجاز است")
    return json.loads(body.decode("utf-8-sig"))


def _items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("Items", "items", "data", "Data", "result", "Result"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _items(value)
            if nested:
                return nested
    return []


def _pick(obj: dict, *names: str) -> str:
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _map_shortcut(item: dict) -> dict | None:
    isin = _pick(item, "instrumentid", "instrumentId", "isin").upper()
    symbol = _pick(item, "instrument_Name", "instrumentName", "symbol", "ticker")
    company = _pick(item, "company_Name_Persian", "companyNamePersian", "companyName", "company")
    market = _pick(item, "marketcategoryname", "markettypename", "marketname", "marketType")
    if not symbol or not valid_isin(isin):
        return None
    return {
        "symbol": symbol,
        "isin": isin,
        "company_name": company,
        "market_type": market,
    }


def _map_watch(item: dict, market_type: str) -> dict | None:
    isin = _pick(item, "instrumentId", "instrumentid", "isin").upper()
    symbol = _pick(item, "instrumentName", "instrumentname", "symbol", "ticker")
    company = _pick(item, "companyNamePersian", "companynamepersian", "companyName", "company")
    if not symbol or not valid_isin(isin):
        return None
    return {
        "symbol": symbol,
        "isin": isin,
        "company_name": company,
        "market_type": market_type,
    }


def _sort_key(item: dict):
    return (normalize_symbol(item.get("symbol", "")), item.get("isin", ""))


def _load_cache_locked():
    global _loaded, _symbols, _updated_at
    if _loaded:
        return
    _loaded = True
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached = payload.get("symbols", []) if isinstance(payload, dict) else []
        valid = []
        for item in cached:
            if not isinstance(item, dict):
                continue
            mapped = _map_shortcut(item)
            if mapped:
                valid.append(mapped)
            else:
                symbol = str(item.get("symbol", "")).strip()
                isin = str(item.get("isin", "")).strip().upper()
                if symbol and valid_isin(isin):
                    valid.append({
                        "symbol": symbol,
                        "isin": isin,
                        "company_name": str(item.get("company_name", "")).strip(),
                        "market_type": str(item.get("market_type", "")).strip(),
                    })
        if valid:
            _symbols = list({x["isin"]: x for x in valid}.values())
            _symbols.sort(key=_sort_key)
            _updated_at = str(payload.get("updated_at", ""))
    except (OSError, ValueError, TypeError):
        pass


def _save_cache(symbols: list[dict], updated_at: str):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"updated_at": updated_at, "symbols": symbols}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(CACHE_FILE)


def refresh_market_watch():
    global _symbols, _updated_at, _last_error
    try:
        merged: dict[str, dict] = {}
        for path, kind in ((CASH_PATH, "سهام"), (ETF_PATH, "صندوق ETF")):
            payload = _request_json(path, timeout=15)
            for raw in _items(payload):
                if not isinstance(raw, dict):
                    continue
                item = _map_watch(raw, kind)
                if item:
                    previous = merged.get(item["isin"], {})
                    for field in ("symbol", "company_name", "market_type"):
                        if not item.get(field):
                            item[field] = previous.get(field, "")
                    merged[item["isin"]] = item
        if not merged:
            raise RuntimeError("فهرست سهام و ETF خالی یا فاقد ISIN معتبر است")
        result = sorted(merged.values(), key=_sort_key)
        updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            _save_cache(result, updated)
        except OSError:
            # The in-memory list is still useful even if local cache is read-only.
            pass
        with _lock:
            _symbols = result
            _updated_at = updated
            _last_error = ""
        return len(result)
    except Exception as exc:
        with _lock:
            _last_error = type(exc).__name__ + ": " + str(exc)[:180]
        return 0


def start_symbol_refresh():
    global _refresh_started
    with _lock:
        if _refresh_started:
            return
        _load_cache_locked()
        _refresh_started = True

    def worker():
        refresh_market_watch()
        while True:
            time.sleep(12 * 60 * 60)
            refresh_market_watch()

    threading.Thread(target=worker, name="dana-symbol-refresh", daemon=True).start()


def _rank(item: dict, query: str) -> int:
    ticker = normalize_symbol(item.get("symbol", ""))
    company = normalize_symbol(item.get("company_name", ""))
    if ticker == query:
        return 0
    if ticker.startswith(query):
        return 1
    if query in ticker:
        return 2
    if company.startswith(query):
        return 3
    if query in company:
        return 4
    return 99


def suggest_symbols(query: str) -> list[dict]:
    query = normalize_symbol(query)
    if not query or len(query) > 64:
        return []
    now = time.monotonic()
    with _lock:
        _load_cache_locked()
        cached_query = _query_cache.get(query)
        if cached_query and now - cached_query[0] < 20:
            return list(cached_query[1])
        local = list(_symbols)

    remote: list[dict] = []
    try:
        payload = _request_json(
            SHORTCUT_PATH,
            {"SimpleSearchText": query, "PageNumber": "1", "PageSize": str(LIMIT)},
            timeout=3.5,
        )
        for raw in _items(payload):
            if isinstance(raw, dict):
                item = _map_shortcut(raw)
                if item:
                    remote.append(item)
    except Exception:
        pass

    merged = {item["isin"]: item for item in local}
    for item in remote:
        previous = merged.get(item["isin"], {})
        for field in ("company_name", "market_type"):
            if not item.get(field):
                item[field] = previous.get(field, "")
        merged[item["isin"]] = item

    ranked = []
    for item in merged.values():
        rank = _rank(item, query)
        if rank < 99:
            ranked.append((rank, item))
    ranked.sort(key=lambda pair: (pair[0], normalize_symbol(pair[1]["symbol"]), pair[1]["isin"]))
    result = [item for _, item in ranked[:LIMIT]]
    with _lock:
        _query_cache[query] = (now, result)
        if len(_query_cache) > 100:
            for key in list(_query_cache)[:50]:
                _query_cache.pop(key, None)
    return result


def symbol_catalog_status() -> dict:
    with _lock:
        _load_cache_locked()
        return {
            "count": len(_symbols),
            "updated_at": _updated_at,
            "last_error": _last_error,
            "source": "بورس تهران؛ فهرست سهام و ETF به‌علاوه جست‌وجوی ابزارهای غیرفعال",
        }

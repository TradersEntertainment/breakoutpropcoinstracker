"""
Breakout Prop x Binance Funding Alarm Botu
──────────────────────────────────────────
Breakout Prop'ta (Hyperliquid) listeli coinleri Binance USDT-M perpetual
kontratlarıyla eşler ve Binance funding oranı mutlak değer olarak
FUNDING_THRESHOLD'u (varsayılan %0.7 = "%1 civarı, 0.3 tolerans; üst
sınır yok, yön fark etmez) geçtiğinde Telegram'a bildirim atar.
Karşılaştırma için Hyperliquid'in saatlik funding'i de mesaja eklenir.

Railway üzerinde sürekli çalışan worker olarak tasarlandı (bkz. README.md).
RUN_ONCE=1 ile tek tarama yapıp çıkar (test için).
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Ayarlar (hepsi env ile değiştirilebilir) ─────────────────────────


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
CHAT_ID = _env("CHAT_ID", "TELEGRAM_CHAT_ID")

FUNDING_THRESHOLD = _env_num("FUNDING_THRESHOLD", 0.7)        # % — mutlak alt sınır
CHECK_INTERVAL = int(_env_num("CHECK_INTERVAL_SECONDS", 60))
COOLDOWN_MINUTES = _env_num("ALERT_COOLDOWN_MINUTES", 45)
REALERT_DELTA = _env_num("REALERT_DELTA", 0.3)                # % puan; bu kadar artarsa cooldown beklemez
HEARTBEAT_HOURS = _env_num("HEARTBEAT_HOURS", 24)             # 0 = kapalı
MAPPING_REFRESH_HOURS = _env_num("MAPPING_REFRESH_HOURS", 6)
RUN_ONCE = os.environ.get("RUN_ONCE", "") == "1"

FAPI = "https://fapi.binance.com/fapi/v1"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
ASSETS_FILE = Path(__file__).with_name("assets.json")
TELEGRAM_CHUNK = 3800  # Telegram tek mesaj limiti 4096

session = requests.Session()
session.headers.update({"User-Agent": "breakout-prop-funding-bot/1.0"})


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", flush=True)


# ── HTTP yardımcıları ────────────────────────────────────────────────


def _request_json(method: str, url: str, *, payload=None, retries: int = 3):
    last_error: Exception = RuntimeError("unreachable")
    for attempt in range(1, retries + 1):
        try:
            if method == "GET":
                resp = session.get(url, timeout=25)
            else:
                resp = session.post(url, json=payload, timeout=25)
            if resp.status_code in (418, 429):
                wait = int(resp.headers.get("Retry-After", "0") or 0) or 10 * attempt
                log(f"Rate limit ({resp.status_code}) {url} → {wait}s bekleniyor")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as error:  # ağ hatası, JSON hatası vs.
            last_error = error
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_error


def get_json(url: str):
    return _request_json("GET", url)


def post_json(url: str, payload):
    return _request_json("POST", url, payload=payload)


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Coin listesi ve Binance eşleştirme ───────────────────────────────


def load_assets_config() -> dict:
    with open(ASSETS_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("assets", [])
    cfg.setdefault("overrides", {})
    cfg.setdefault("exclude", [])
    return cfg


def binance_candidates(hl_name: str) -> list[str]:
    """Hyperliquid ismi için olası Binance perpetual sembolleri (öncelik sırasıyla)."""
    name = hl_name.strip()
    # Hyperliquid'in k-öneki 1000x demek: kPEPE → 1000PEPE
    if len(name) > 1 and name[0] == "k" and name[1:].isupper():
        base = name[1:]
        return [f"1000{base}USDT", f"{base}USDT"]
    upper = name.upper()
    return [f"{upper}USDT", f"1000{upper}USDT"]


def fetch_binance_perp_symbols() -> set[str]:
    """İşleme açık USDT-M perpetual sembolleri."""
    data = get_json(f"{FAPI}/exchangeInfo")
    return {
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }


def build_mapping(cfg: dict, perp_symbols: set[str]) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    unmatched: list[str] = []
    excluded = set(cfg["exclude"])
    for asset in cfg["assets"]:
        if asset in excluded:
            continue
        override = cfg["overrides"].get(asset)
        candidates = [override] if override else binance_candidates(asset)
        symbol = next((c for c in candidates if c in perp_symbols), None)
        if symbol:
            mapping[asset] = symbol
        else:
            unmatched.append(asset)
    return mapping, unmatched


def fetch_funding_intervals() -> dict[str, float]:
    """Varsayılan 8 saatten farklı funding periyodu olan semboller."""
    try:
        data = get_json(f"{FAPI}/fundingInfo")
        return {
            d["symbol"]: safe_float(d.get("fundingIntervalHours"), 8.0)
            for d in data
            if isinstance(d, dict) and d.get("symbol")
        }
    except Exception as error:
        log(f"fundingInfo alınamadı (varsayılan 8s kullanılacak): {error}")
        return {}


def fetch_premium_index() -> dict[str, dict]:
    data = get_json(f"{FAPI}/premiumIndex")
    return {d["symbol"]: d for d in data if isinstance(d, dict) and d.get("symbol")}


def fetch_hyperliquid_funding() -> dict[str, float]:
    """HL coin adı → saatlik funding (% cinsinden)."""
    try:
        data = post_json(HYPERLIQUID_INFO, {"type": "metaAndAssetCtxs"})
        universe = data[0].get("universe", [])
        contexts = data[1]
        result: dict[str, float] = {}
        for meta, ctx in zip(universe, contexts):
            funding = ctx.get("funding")
            if funding is not None:
                result[meta["name"]] = safe_float(funding) * 100
        return result
    except Exception as error:
        log(f"Hyperliquid funding alınamadı (mesaj HL verisi olmadan gidecek): {error}")
        return {}


# ── Telegram ─────────────────────────────────────────────────────────


def chunk_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_CHUNK:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > TELEGRAM_CHUNK and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log(f"[DRY-RUN] Telegram ayarlı değil, gidecek mesaj:\n{text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in chunk_message(text):
        payload = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, 4):
            try:
                resp = session.post(url, json=payload, timeout=25)
                if resp.status_code == 429:
                    wait = resp.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(wait + 1)
                    continue
                if resp.status_code != 200:
                    log(f"Telegram hatası {resp.status_code}: {resp.text[:300]}")
                break
            except Exception as error:
                log(f"Telegram gönderilemedi (deneme {attempt}): {error}")
                time.sleep(2 * attempt)


# ── Mesaj biçimleme ──────────────────────────────────────────────────


def format_coin_block(
    hl_name: str,
    symbol: str,
    rate_pct: float,
    next_funding_ms: float,
    interval_hours: float,
    hl_rate_pct_hour: float | None,
) -> str:
    if rate_pct > 0:
        side = "Funding'i SHORT taraf topluyor → arb: Binance SHORT + HL LONG"
    else:
        side = "Funding'i LONG taraf topluyor → arb: Binance LONG + HL SHORT"
    lines = [
        f"🔔 <b>{hl_name}</b> ({symbol})",
        f"   💰 Binance funding: <b>{rate_pct:+.4f}%</b> / {interval_hours:g} saat",
    ]
    if next_funding_ms:
        minutes = max(0, int(next_funding_ms / 1000 - time.time()) // 60)
        lines.append(f"   ⏳ Sonraki Binance funding: {minutes} dk")
    if hl_rate_pct_hour is not None:
        equivalent = hl_rate_pct_hour * interval_hours
        lines.append(
            f"   🌊 Hyperliquid funding: {hl_rate_pct_hour:+.4f}%/saat"
            f" (~{equivalent:+.4f}%/{interval_hours:g}s)"
        )
    lines.append(f"   📍 {side}")
    return "\n".join(lines)


def startup_message(cfg: dict, mapping: dict[str, str], unmatched: list[str]) -> str:
    renamed = [
        f"{hl}→{sym}" for hl, sym in mapping.items() if sym != f"{hl.upper()}USDT"
    ]
    total = len(cfg["assets"]) - len(set(cfg["exclude"]))
    lines = [
        "🤖 <b>Breakout Prop Funding Botu başladı</b>",
        f"Eşik: |funding| ≥ {FUNDING_THRESHOLD:g}% (üst sınır yok, +/- fark etmez)",
        f"Kontrol: {CHECK_INTERVAL} sn • Cooldown: {COOLDOWN_MINUTES:g} dk",
        f"✅ Binance'te eşleşen: {len(mapping)}/{total}",
    ]
    if renamed:
        lines.append(f"🔁 Farklı isimle eşleşen: {', '.join(renamed)}")
    if unmatched:
        lines.append(f"❌ Binance'te bulunamayan: {', '.join(unmatched)}")
    if cfg["exclude"]:
        lines.append(f"🚫 Hariç tutulan: {', '.join(cfg['exclude'])}")
    lines.append(
        "⚠️ Eşleşmeler isim bazlı — listede yanlışlık görürsen assets.json'da "
        "\"overrides\" / \"exclude\" alanlarını kullan."
    )
    return "\n".join(lines)


def heartbeat_message(
    mapping: dict[str, str], premium: dict[str, dict], unmatched: list[str]
) -> str:
    rates = []
    for hl_name, symbol in mapping.items():
        data = premium.get(symbol)
        if data:
            rates.append((hl_name, safe_float(data.get("lastFundingRate")) * 100))
    rates.sort(key=lambda item: abs(item[1]), reverse=True)
    top = " • ".join(f"{name} {rate:+.3f}%" for name, rate in rates[:3])
    lines = [
        f"✅ Bot çalışıyor — {len(mapping)} coin izleniyor"
        + (f" ({len(unmatched)} eşleşmedi)" if unmatched else ""),
        f"En yüksek |funding|: {top}" if top else "Veri yok",
        f"Eşik: |funding| ≥ {FUNDING_THRESHOLD:g}%",
    ]
    return "\n".join(lines)


# ── Ana döngü ────────────────────────────────────────────────────────


def scan(
    mapping: dict[str, str],
    intervals: dict[str, float],
    last_alerts: dict[str, tuple[float, float]],
) -> tuple[list[str], dict[str, dict]]:
    """Bir tarama yapar; bildirim bloklarını ve premium verisini döndürür."""
    premium = fetch_premium_index()
    now = time.time()

    triggered: list[tuple[str, str, float]] = []
    for hl_name, symbol in mapping.items():
        data = premium.get(symbol)
        if not data:
            continue
        rate_pct = safe_float(data.get("lastFundingRate")) * 100
        if abs(rate_pct) < FUNDING_THRESHOLD:
            # Eşiğin altına düştü → bir sonraki aşımda tekrar anında bildir
            last_alerts.pop(symbol, None)
            continue
        previous = last_alerts.get(symbol)
        due = (
            previous is None
            or now - previous[0] >= COOLDOWN_MINUTES * 60
            or abs(rate_pct) >= previous[1] + REALERT_DELTA
        )
        if due:
            triggered.append((hl_name, symbol, rate_pct))

    blocks: list[str] = []
    if triggered:
        hl_funding = fetch_hyperliquid_funding()
        for hl_name, symbol, rate_pct in triggered:
            blocks.append(
                format_coin_block(
                    hl_name,
                    symbol,
                    rate_pct,
                    safe_float(premium[symbol].get("nextFundingTime")),
                    intervals.get(symbol, 8.0),
                    hl_funding.get(hl_name),
                )
            )
            last_alerts[symbol] = (now, abs(rate_pct))
    return blocks, premium


def main() -> None:
    log("Bot başlıyor…")
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("UYARI: TELEGRAM_TOKEN / CHAT_ID tanımlı değil → DRY-RUN modu (mesajlar sadece log'a).")

    cfg = load_assets_config()

    # Açılışta Binance listesi gelene kadar bekle (geçici ağ hatasında ölme)
    while True:
        try:
            perp_symbols = fetch_binance_perp_symbols()
            break
        except Exception as error:
            log(f"exchangeInfo alınamadı, 30 sn sonra tekrar: {error}")
            time.sleep(30)

    mapping, unmatched = build_mapping(cfg, perp_symbols)
    intervals = fetch_funding_intervals()
    log(f"Eşleşen {len(mapping)} coin, eşleşmeyen {len(unmatched)}: {', '.join(unmatched) or '-'}")
    send_telegram(startup_message(cfg, mapping, unmatched))

    last_alerts: dict[str, tuple[float, float]] = {}
    consecutive_failures = 0
    last_failure_notice = 0.0
    started = time.time()
    next_heartbeat = started + HEARTBEAT_HOURS * 3600 if HEARTBEAT_HOURS > 0 else None
    next_mapping_refresh = started + MAPPING_REFRESH_HOURS * 3600

    while True:
        cycle_start = time.time()
        try:
            if cycle_start >= next_mapping_refresh:
                perp_symbols = fetch_binance_perp_symbols()
                new_mapping, new_unmatched = build_mapping(cfg, perp_symbols)
                added = sorted(set(new_mapping) - set(mapping))
                removed = sorted(set(mapping) - set(new_mapping))
                if added or removed:
                    log(f"Eşleşme güncellendi — yeni: {added or '-'} çıkan: {removed or '-'}")
                mapping, unmatched = new_mapping, new_unmatched
                intervals = fetch_funding_intervals()
                next_mapping_refresh = cycle_start + MAPPING_REFRESH_HOURS * 3600

            blocks, premium = scan(mapping, intervals, last_alerts)
            if blocks:
                header = f"🚨 <b>FUNDING FIRSATI</b> (eşik ±{FUNDING_THRESHOLD:g}%)\n\n"
                footer = (
                    "\n\nℹ️ HL funding saat başı ödenir; Binance periyodu "
                    "coine göre 4s/8s olabilir."
                )
                send_telegram(header + "\n\n".join(blocks) + footer)
                log(f"{len(blocks)} coin için bildirim gönderildi.")

            if next_heartbeat and cycle_start >= next_heartbeat:
                send_telegram(heartbeat_message(mapping, premium, unmatched))
                next_heartbeat = cycle_start + HEARTBEAT_HOURS * 3600

            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            log(f"Tarama hatası ({consecutive_failures}. kez üst üste):\n{traceback.format_exc()}")
            if consecutive_failures >= 10 and time.time() - last_failure_notice > 6 * 3600:
                send_telegram(
                    "⚠️ Bot 10+ taramadır Binance verisi alamıyor — Railway loglarını kontrol et."
                )
                last_failure_notice = time.time()

        if RUN_ONCE:
            log("RUN_ONCE=1 → tek tarama tamamlandı, çıkılıyor.")
            break
        elapsed = time.time() - cycle_start
        time.sleep(max(5.0, CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    main()

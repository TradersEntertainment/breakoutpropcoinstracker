"""
Range Finder
────────────
Breakout Prop listesindeki coinlerin Binance mumlarını tarar ve bir kanal
içinde gidip gelen ("range yapan") coinleri bulur. Kanal eğimli olabilir:
yavaşça alçalarak/yükselerek de olsa banttan banta gitgel yapan yapı
aranır. Bulunca RANGE_CHAT_ID'ye (funding'den ayrı Telegram kanalı)
bildirim atar; sonuçlar dashboard'da da görünür.

Yöntem: son RANGE_LOOKBACK_HOURS saatin kapanışlarına doğrusal trend
uydurulur, trendden arındırılmış seride bant (p5–p95) çıkarılır ve
alt/üst bant bölgesine dönüşümlü dokunuşlar sayılır. Genişlik, dokunuş
sayısı, trendin bant yüksekliğine oranı ve Kaufman verimlilik oranından
0-100 arası bir skor üretilir.
"""

import math
import os
import time
import traceback

import bot
import state
from bot import (
    FAPI,
    SEPARATOR,
    _env,
    _env_num,
    build_mapping,
    fetch_binance_perp_symbols,
    fmt_price,
    get_json,
    load_assets_config,
    log,
    safe_float,
    send_telegram,
)

# ── Ayarlar ──────────────────────────────────────────────────────────

RANGE_CHAT_ID = _env("RANGE_CHAT_ID")
RANGE_INTERVAL = _env("RANGE_INTERVAL", default="15m")
LOOKBACK_HOURS = _env_num("RANGE_LOOKBACK_HOURS", 24)
SCAN_MINUTES = _env_num("RANGE_SCAN_MINUTES", 15)

MIN_WIDTH = _env_num("RANGE_MIN_WIDTH", 2.0)      # % — bundan dar bant ilgisiz
MAX_WIDTH = _env_num("RANGE_MAX_WIDTH", 20.0)     # % — bundan geniş "range" değil kaos
MIN_TOUCHES = int(_env_num("RANGE_MIN_TOUCHES", 4))
MAX_DRIFT = _env_num("RANGE_MAX_DRIFT", 1.5)      # trendin bant yüksekliğine oranı
SCORE_ENTER = _env_num("RANGE_SCORE_ENTER", 60)
SCORE_EXIT = _env_num("RANGE_SCORE_EXIT", 45)
BREAK_OVERSHOOT = _env_num("RANGE_BREAK_OVERSHOOT", 0.25)  # bant dışına taşma (yükseklik oranı)

EDGE_ALERTS = os.environ.get("EDGE_ALERTS", "1") != "0"
EDGE_ZONE = _env_num("EDGE_ZONE", 0.15)           # konum <= 0.15 alt bant, >= 0.85 üst bant
EDGE_COOLDOWN_MIN = _env_num("EDGE_COOLDOWN_MINUTES", 90)

RUN_ONCE = os.environ.get("RUN_ONCE", "") == "1"
# Varsayılan KAPALI: range sinyalleri dashboard'da gösterilir; Telegram
# bildirimi istersen RANGE_ALERTS=1 + RANGE_CHAT_ID ayarla.
RANGE_ALERTS = os.environ.get("RANGE_ALERTS", "0") != "0"

_INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240,
}
SPARK_POINTS = 48  # dashboard sparkline için örneklenen nokta sayısı


def interval_minutes() -> int:
    return _INTERVAL_MINUTES.get(RANGE_INTERVAL, 15)


def candles_needed() -> int:
    return max(30, min(1000, int(LOOKBACK_HOURS * 60 / interval_minutes())))


# ── Matematik yardımcıları ───────────────────────────────────────────


def linreg(values: list[float]) -> tuple[float, float]:
    """En küçük kareler doğrusu: (eğim, kesişim)."""
    n = len(values)
    sx = n * (n - 1) / 2
    sxx = (n - 1) * n * (2 * n - 1) / 6
    sy = sum(values)
    sxy = sum(i * v for i, v in enumerate(values))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n if n else 0.0
    slope = (n * sxy - sx * sy) / denom
    return slope, (sy - slope * sx) / n


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ── Range analizi ────────────────────────────────────────────────────


def analyze_series(closes: list[float]) -> dict | None:
    """Kapanış serisinden range metrikleri üretir; veri yetersizse None."""
    n = len(closes)
    if n < 30 or any(c <= 0 for c in closes):
        return None
    last = closes[-1]

    slope, intercept = linreg(closes)
    resid = [c - (slope * i + intercept) for i, c in enumerate(closes)]
    resid_sorted = sorted(resid)
    p5 = percentile(resid_sorted, 5)
    p95 = percentile(resid_sorted, 95)
    height = p95 - p5
    if height <= 0:
        return None

    width_pct = height / last * 100

    # Dönüşümlü bant dokunuşları: üst %25 ↔ alt %25 bölgeleri
    touches = 0
    last_side = None
    for r in resid:
        pos = (r - p5) / height
        if pos >= 0.75:
            side = "H"
        elif pos <= 0.25:
            side = "L"
        else:
            continue
        if side != last_side:
            touches += 1
            last_side = side

    drift_total = slope * (n - 1)
    drift_ratio = abs(drift_total) / height
    bars_per_day = 24 * 60 / interval_minutes()
    drift_day_pct = slope * bars_per_day / last * 100

    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, n))
    efficiency = abs(closes[-1] - closes[0]) / path if path > 0 else 1.0

    # Bantın "şu anki" mutlak seviyeleri (trend son barda değerlendirilir)
    trend_last = slope * (n - 1) + intercept
    band_low = trend_last + p5
    band_high = trend_last + p95
    position = clamp((last - band_low) / height, -0.5, 1.5)

    # Elemeler: biri düşerse skor 0 ve sebep yazılır
    reasons = []
    if width_pct < MIN_WIDTH:
        reasons.append("dar bant")
    if width_pct > MAX_WIDTH:
        reasons.append("aşırı geniş")
    if touches < MIN_TOUCHES:
        reasons.append("az dokunuş")
    if drift_ratio > MAX_DRIFT:
        reasons.append("trend baskın")

    if reasons:
        score = 0.0
    else:
        touches_score = clamp(touches / 8, 0, 1)
        if 3 <= width_pct <= 10:
            width_score = 1.0
        elif width_pct < 3:
            width_score = 0.6 + 0.4 * (width_pct - MIN_WIDTH) / max(3 - MIN_WIDTH, 1e-9)
        else:
            width_score = 1.0 - 0.6 * (width_pct - 10) / max(MAX_WIDTH - 10, 1e-9)
        drift_score = 1.0 - 0.6 * clamp(drift_ratio / MAX_DRIFT, 0, 1)
        er_score = 1.0 - clamp(efficiency / 0.5, 0, 1)
        score = 100 * (
            0.45 * touches_score
            + 0.20 * clamp(width_score, 0, 1)
            + 0.15 * drift_score
            + 0.20 * er_score
        )

    step = max(1, n // SPARK_POINTS)
    spark = [round(c, 8) for c in closes[::step]]

    return {
        "score": round(score, 1),
        "reasons": reasons,
        "width_pct": round(width_pct, 2),
        "touches": touches,
        "drift_ratio": round(drift_ratio, 2),
        "drift_day_pct": round(drift_day_pct, 2),
        "efficiency": round(efficiency, 3),
        "band_low": band_low,
        "band_high": band_high,
        # Kanalın pencere başındaki seviyeleri — dashboard eğimli bandı bunlarla çizer
        "band_low_start": intercept + p5,
        "band_high_start": intercept + p95,
        "last": last,
        "position": round(position, 3),
        "spark": spark,
        "spark_step_min": step * interval_minutes(),
    }


def fetch_closes(symbol: str) -> list[float]:
    data = get_json(
        f"{FAPI}/klines?symbol={symbol}&interval={RANGE_INTERVAL}&limit={candles_needed()}"
    )
    return [safe_float(k[4]) for k in data if isinstance(k, (list, tuple)) and len(k) > 4]


def sweep(mapping: dict[str, str]) -> dict[str, dict]:
    """Tüm coinleri tarar; coin → metrik sözlüğü döndürür."""
    results: dict[str, dict] = {}
    failed = 0
    for hl_name, symbol in mapping.items():
        try:
            metrics = analyze_series(fetch_closes(symbol))
            if metrics:
                metrics["coin"] = hl_name
                metrics["symbol"] = symbol
                results[hl_name] = metrics
        except Exception as error:
            failed += 1
            log(f"{symbol} kline hatası: {error}")
        time.sleep(0.12)  # ağırlık limitine nazik davran
    if failed:
        log(f"{failed} sembolün mum verisi alınamadı.")
    return results


# ── Bildirim kararları ───────────────────────────────────────────────


def drift_label(drift_day_pct: float) -> str:
    if drift_day_pct <= -0.3:
        return f"↘️ alçalan kanal: {drift_day_pct:+.1f}%/gün"
    if drift_day_pct >= 0.3:
        return f"↗️ yükselen kanal: {drift_day_pct:+.1f}%/gün"
    return f"➡️ yatay kanal: {drift_day_pct:+.1f}%/gün"


def new_range_block(m: dict) -> str:
    return "\n".join([
        f"📦 <b>{m['coin']}</b> range'e girdi · skor <b>{m['score']:.0f}</b>/100"
        f" · genişlik %{m['width_pct']:.1f} · {m['touches']} dokunuş",
        f"{m['symbol']} · {fmt_price(m['last'])}"
        f" · bant {fmt_price(m['band_low'])} – {fmt_price(m['band_high'])}"
        f" · konum %{m['position'] * 100:.0f}",
        drift_label(m["drift_day_pct"])
        + f" · pencere {LOOKBACK_HOURS:g}sa/{RANGE_INTERVAL}",
    ])


def edge_block(m: dict, side: str) -> str:
    side_txt = "ALT bant" if side == "low" else "ÜST bant"
    icon = "🟢" if side == "low" else "🔴"
    return "\n".join([
        f"🎯 <b>{m['coin']}</b> {side_txt} bölgesinde {icon}"
        f" · {fmt_price(m['last'])} · konum %{m['position'] * 100:.0f}",
        f"Bant {fmt_price(m['band_low'])} – {fmt_price(m['band_high'])}"
        f" · skor {m['score']:.0f} · genişlik %{m['width_pct']:.1f}",
    ])


def break_block(m: dict, reason: str) -> str:
    return "\n".join([
        f"💥 <b>{m['coin']}</b> range kırıldı · {reason}",
        f"{m['symbol']} · {fmt_price(m['last'])} · skor {m['score']:.0f}",
    ])


def evaluate(
    results: dict[str, dict],
    tracker: dict,
    now: float,
) -> tuple[list[str], list[str], list[str]]:
    """Tarama sonuçlarını duruma uygular; (yeni, kırılan, bant) blokları döndürür.

    tracker: {"ranging": set[str], "edge_last": {(coin, side): ts}}
    """
    ranging: set = tracker.setdefault("ranging", set())
    edge_last: dict = tracker.setdefault("edge_last", {})

    new_blocks: list[str] = []
    break_blocks: list[str] = []
    edge_blocks: list[str] = []

    for coin, m in sorted(results.items(), key=lambda kv: kv[1]["score"], reverse=True):
        if coin not in ranging:
            if m["score"] >= SCORE_ENTER:
                ranging.add(coin)
                new_blocks.append(new_range_block(m))
                # Aynı taramada ayrıca bant uyarısı atma; konum mesajda zaten var
                for side in ("low", "high"):
                    edge_last[(coin, side)] = now
            continue

        # Halihazırda range'de olan coin
        if m["position"] < -BREAK_OVERSHOOT:
            ranging.discard(coin)
            break_blocks.append(break_block(m, "fiyat bandın ALTINA taştı"))
            continue
        if m["position"] > 1 + BREAK_OVERSHOOT:
            ranging.discard(coin)
            break_blocks.append(break_block(m, "fiyat bandın ÜSTÜNE taştı"))
            continue
        if m["score"] < SCORE_EXIT:
            ranging.discard(coin)
            break_blocks.append(break_block(m, "yapı bozuldu (skor düştü)"))
            continue

        if EDGE_ALERTS:
            side = None
            if m["position"] <= EDGE_ZONE:
                side = "low"
            elif m["position"] >= 1 - EDGE_ZONE:
                side = "high"
            if side:
                last_ts = edge_last.get((coin, side), 0)
                if now - last_ts >= EDGE_COOLDOWN_MIN * 60:
                    edge_last[(coin, side)] = now
                    edge_blocks.append(edge_block(m, side))

    return new_blocks, break_blocks, edge_blocks


def publish_range_state(results: dict[str, dict], tracker: dict, sweep_seconds: float) -> None:
    ranging = tracker.get("ranging", set())
    coins = []
    for coin, m in sorted(results.items(), key=lambda kv: kv[1]["score"], reverse=True):
        entry = dict(m)
        entry["ranging"] = coin in ranging
        coins.append(entry)
    state.update("ranges", {
        "updated": time.time(),
        "sweep_seconds": round(sweep_seconds, 1),
        "spark_step_min": max(1, candles_needed() // SPARK_POINTS) * interval_minutes(),
        "interval": RANGE_INTERVAL,
        "lookback_hours": LOOKBACK_HOURS,
        "score_enter": SCORE_ENTER,
        "score_exit": SCORE_EXIT,
        "edge_zone": EDGE_ZONE,
        "ranging_count": len(ranging),
        "coins": coins,
    })


def startup_message(mapping_size: int) -> str:
    lines = [
        "📦 <b>Range Finder başladı</b>",
        f"{mapping_size} coin taranacak · pencere {LOOKBACK_HOURS:g}sa/{RANGE_INTERVAL}"
        f" · tarama sıklığı {SCAN_MINUTES:g} dk",
        f"Kriter: ≥{MIN_TOUCHES} bant dokunuşu · genişlik %{MIN_WIDTH:g}–%{MAX_WIDTH:g}"
        f" · skor ≥{SCORE_ENTER:g} girer, <{SCORE_EXIT:g} çıkar",
        "Eğimli (alçalan/yükselen) kanallar da dahildir.",
    ]
    if EDGE_ALERTS:
        lines.append(
            f"🎯 Range'deki coin alt/üst banda gelince ayrıca haber verilir"
            f" (bölge %{EDGE_ZONE * 100:.0f}, {EDGE_COOLDOWN_MIN:g} dk arayla)."
        )
    lines.append("Konum: %0 = alt bant, %100 = üst bant.")
    return "\n".join(lines)


# ── Ana döngü ────────────────────────────────────────────────────────


def main() -> None:
    log("Range finder başlıyor…")
    if not RANGE_ALERTS:
        log("Range Telegram bildirimleri KAPALI (RANGE_ALERTS=1 ile açılır); sonuçlar dashboard'da.")
    elif not RANGE_CHAT_ID:
        log("UYARI: RANGE_CHAT_ID tanımlı değil → range mesajları sadece log'a yazılacak.")

    cfg = load_assets_config()
    while True:
        try:
            perp_symbols = fetch_binance_perp_symbols()
            break
        except Exception as error:
            log(f"exchangeInfo alınamadı (range), 30 sn sonra tekrar: {error}")
            time.sleep(30)

    mapping, unmatched = build_mapping(cfg, perp_symbols)
    log(f"Range finder {len(mapping)} coin izleyecek ({len(unmatched)} eşleşmedi).")
    send_telegram(startup_message(len(mapping)), RANGE_CHAT_ID, enabled=RANGE_ALERTS)

    tracker: dict = {"ranging": set(), "edge_last": {}}
    next_mapping_refresh = time.time() + bot.MAPPING_REFRESH_HOURS * 3600

    while True:
        cycle_start = time.time()
        try:
            if cycle_start >= next_mapping_refresh:
                mapping, unmatched = build_mapping(cfg, fetch_binance_perp_symbols())
                next_mapping_refresh = cycle_start + bot.MAPPING_REFRESH_HOURS * 3600

            results = sweep(mapping)
            new_blocks, break_blocks, edge_blocks = evaluate(results, tracker, time.time())
            publish_range_state(results, tracker, time.time() - cycle_start)

            blocks = new_blocks + edge_blocks + break_blocks
            if blocks:
                send_telegram(SEPARATOR.join(blocks), RANGE_CHAT_ID, enabled=RANGE_ALERTS)
                log(
                    f"Range bildirimi: {len(new_blocks)} yeni, "
                    f"{len(edge_blocks)} bant, {len(break_blocks)} kırılma."
                )
            log(
                f"Range taraması bitti: {len(results)} coin, "
                f"{len(tracker['ranging'])} range'de, {time.time() - cycle_start:.0f} sn."
            )
        except Exception:
            log(f"Range tarama hatası:\n{traceback.format_exc()}")

        if RUN_ONCE:
            log("RUN_ONCE=1 → tek range taraması tamamlandı, çıkılıyor.")
            break
        elapsed = time.time() - cycle_start
        time.sleep(max(30.0, SCAN_MINUTES * 60 - elapsed))


if __name__ == "__main__":
    main()

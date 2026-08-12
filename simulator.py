"""
Range Simülatörü (kağıt üzerinde, A/B)
──────────────────────────────────────
Dashboard'ın önerdiği range işlemlerini iki ayrı stratejiyle otomatik
uygular — her strateji kendi 10.000$'lık hesabıyla, birebir aynı
girişlerle:

  1. "Stopsuz"       — girince stop yok; karşı banda (hedefe) varana,
                       zaman aşımına ya da likidasyona kadar tutar.
  2. "%1 kırılma"    — fiyat bandı pozisyon aleyhine %1 aşarsa keser
                       (SHORT: üst bant × 1.01, LONG: alt bant × 0.99).

Ortak kurallar: giriş bandın kenarında (alt → LONG, üst → SHORT, beklenen
kâr RANGE_MIN_PROFIT üstünde), hesap 5 eşit slot, slot marjini × 10x
notional, taker komisyonu + kayma iki yönde, beklenen tur süresinin
3 katında zaman aşımı, zarar marjini yerse likidasyon. İki strateji
arasındaki TEK fark stop kuralıdır — veri buna göre okunur.

Kapanan her işlem giriş bağlamıyla (skor, genişlik, beklenen süre/kâr…)
kaydedilir; ham veri /api/sim'de. Durum dosyaları yazılabilir /data
volume'u varsa oraya yazılır (deploy'lar arası kalıcı).
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import state
from bot import HYPERLIQUID_INFO, post_json, safe_float

# ── Ayarlar ──────────────────────────────────────────────────────────


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


SIM_ENABLED = os.environ.get("SIM_ENABLED", "1") != "0"
START_BALANCE = _env_num("SIM_START_BALANCE", 10000)
LEVERAGE = _env_num("SIM_LEVERAGE", 10)
MAX_POSITIONS = int(_env_num("SIM_MAX_POSITIONS", 5))
FEE_PCT = _env_num("SIM_FEE_PCT", 0.045)            # taraf başına taker %
SLIPPAGE_PCT = _env_num("SIM_SLIPPAGE_PCT", 0.02)   # taraf başına %
COOLDOWN_MIN = _env_num("SIM_COOLDOWN_MINUTES", 30)
TIME_STOP_MULT = _env_num("SIM_TIME_STOP_MULT", 3)  # beklenen tur × N; 0 = kapalı
STOP_BREAK_PCT = _env_num("SIM_STOP_BREAK_PCT", 1.0)  # 2. strateji: bant + %1
TICK_SECONDS = int(_env_num("SIM_TICK_SECONDS", 60))
SAVE_EVERY_SECONDS = 300
EQUITY_SAMPLE_SECONDS = 3600
MAX_EQUITY_SAMPLES = 1000


def _state_dir() -> Path:
    """SIM_STATE_FILE'ın klasörü > yazılabilir /data > uygulama klasörü."""
    override = os.environ.get("SIM_STATE_FILE", "").strip()
    if override:
        return Path(override).parent
    data_dir = Path("/data")
    try:
        if data_dir.is_dir() and os.access(data_dir, os.W_OK):
            return data_dir
    except OSError:
        pass
    return Path(__file__).parent


STATE_DIR = _state_dir()

VARIANTS = [
    {"key": "nostop", "name": "Stopsuz", "stop_mode": "none"},
    {"key": "stop1", "name": f"%{STOP_BREAK_PCT:g} kırılma stopu",
     "stop_mode": "band_pct", "stop_pct": STOP_BREAK_PCT},
    {"key": "longonly", "name": f"Sadece LONG · %{STOP_BREAK_PCT:g} stop",
     "stop_mode": "band_pct", "stop_pct": STOP_BREAK_PCT, "sides": ["LONG"]},
    {"key": "shortonly", "name": f"Sadece SHORT · %{STOP_BREAK_PCT:g} stop",
     "stop_mode": "band_pct", "stop_pct": STOP_BREAK_PCT, "sides": ["SHORT"]},
]


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] [sim] {message}", flush=True)


def fetch_hl_prices(metrics: dict) -> dict[str, float]:
    """Hisse (EQ) coinleri için Hyperliquid mid fiyatları — dex başına tek istek.

    Binance mark'ı olmayan coinlerde tarama arası (15 dk) fiyat çok bayat
    kalır; dakikalık tick bu mid'lerle beslenir. Hata olursa {} döner ve
    taramanın son fiyatı kullanılır.
    """
    # Binance'ten izlenen EQ'ların mark fiyatı zaten funding akışında var;
    # burada yalnız HL kaynaklı olanlar (dex:isim biçimli) sorgulanır.
    coins = {c: m["symbol"] for c, m in metrics.items()
             if m.get("market") == "hisse" and m.get("symbol")
             and (":" in m["symbol"] or not m["symbol"].endswith("USDT"))}
    if not coins:
        return {}
    prices: dict[str, float] = {}
    dexes = {full.split(":", 1)[0] if ":" in full else "" for full in coins.values()}
    for dex in dexes:
        payload: dict = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        try:
            mids = post_json(HYPERLIQUID_INFO, payload) or {}
        except Exception as error:
            log(f"HL allMids alınamadı (dex={dex or 'ana'}): {error}")
            continue
        for coin, full in coins.items():
            name = full.split(":", 1)[1] if ":" in full else full
            value = mids.get(full) or mids.get(name)
            price = safe_float(value)
            if price > 0:
                prices[coin] = price
    return prices


# ── Simülatör ────────────────────────────────────────────────────────


class Simulator:
    def __init__(self, key: str, name: str, stop_mode: str = "none",
                 stop_pct: float = 0.0, start_balance: float = START_BALANCE,
                 sides: list[str] | None = None):
        self.key = key
        self.name = name
        self.stop_mode = stop_mode
        self.stop_pct = stop_pct
        self.sides = set(sides or ["LONG", "SHORT"])
        self.state_file = STATE_DIR / f"sim_state_{key}.json"
        self.data: dict = {
            "created": time.time(),
            "start_balance": start_balance,
            "balance": start_balance,
            "fees_paid": 0.0,
            "positions": {},      # coin -> pozisyon
            "trades": [],         # kapananlar (analiz verisi)
            "cooldowns": {},      # coin -> tekrar giriş zamanı
            "equity_samples": [],
        }
        self._last_save = 0.0
        self._dirty = False

    # ── kalıcılık ──
    def load(self) -> None:
        try:
            if self.state_file.exists():
                saved = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and "balance" in saved:
                    self.data.update(saved)
                    log(f"[{self.key}] Kayıtlı durum yüklendi: "
                        f"bakiye {self.data['balance']:.2f}$, "
                        f"{len(self.data['trades'])} işlem, "
                        f"{len(self.data['positions'])} açık pozisyon.")
        except Exception as error:
            log(f"[{self.key}] Durum dosyası okunamadı, sıfırdan başlıyor: {error}")

    def save(self, force: bool = False) -> None:
        if not force and not self._dirty and time.time() - self._last_save < SAVE_EVERY_SECONDS:
            return
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.state_file)
            self._last_save = time.time()
            self._dirty = False
        except Exception as error:
            log(f"[{self.key}] Durum kaydedilemedi: {error}")

    # ── yardımcılar ──
    @staticmethod
    def _fill_price(price: float, side: str, entering: bool) -> float:
        """Kayma: girişte de çıkışta da aleyhte."""
        slip = SLIPPAGE_PCT / 100
        worse = (side == "LONG") == entering   # LONG giriş & SHORT çıkış → yukarı
        return price * (1 + slip) if worse else price * (1 - slip)

    def _unrealized(self, p: dict, price: float) -> float:
        direction = 1 if p["side"] == "LONG" else -1
        gross = p["qty"] * (price - p["entry"]) * direction
        return gross - p["entry_fee"]  # çıkış ücreti kapanışta

    def _stop_hit(self, p: dict, price: float) -> bool:
        if self.stop_mode != "band_pct":
            return False
        if p["side"] == "SHORT":
            return price >= p["band_high"] * (1 + self.stop_pct / 100)
        return price <= p["band_low"] * (1 - self.stop_pct / 100)

    # ── çekirdek ──
    def tick(self, ranges: dict, funding: dict, now: float | None = None) -> dict:
        now = now or time.time()
        metrics = {m["coin"]: m for m in ranges.get("coins", []) if m.get("coin")}
        prices = {
            c["coin"]: c["mark_price"]
            for c in funding.get("coins", [])
            if c.get("mark_price")
        }
        zone = ranges.get("edge_zone", 0.15)
        overshoot = ranges.get("break_overshoot", 0.25)
        min_profit = ranges.get("min_profit", 0.0)

        self._check_exits(metrics, prices, now)
        self._check_entries(metrics, prices, zone, overshoot, min_profit, now)
        self._sample_equity(prices, metrics, now)
        self.save()
        return self._view(prices, metrics, now)

    def _check_exits(self, metrics, prices, now) -> None:
        for coin, p in list(self.data["positions"].items()):
            m = metrics.get(coin)
            if m and m.get("ranging"):
                # Bant sürükleniyorsa hedef/bant güncellenir (eğimli kanal)
                p["band_low"], p["band_high"] = m["band_low"], m["band_high"]
                p["target"] = p["band_high"] if p["side"] == "LONG" else p["band_low"]
            price = prices.get(coin) or (m or {}).get("last") or p.get("last_price")
            if not price:
                continue
            p["last_price"] = price
            upnl = self._unrealized(p, price)

            reason = None
            if upnl <= -p["margin"]:
                reason = "likidasyon"
            elif p["side"] == "LONG" and price >= p["target"]:
                reason = "hedef"
            elif p["side"] == "SHORT" and price <= p["target"]:
                reason = "hedef"
            elif self._stop_hit(p, price):
                reason = f"stop (bant %{self.stop_pct:g} kırıldı)"
            elif TIME_STOP_MULT > 0:
                limit_h = (p.get("swing_hours") or 8) * TIME_STOP_MULT
                if now - p["opened"] > limit_h * 3600:
                    reason = "zaman aşımı"

            if reason:
                self._close(coin, price, reason, now)

    def _close(self, coin: str, price: float, reason: str, now: float) -> None:
        p = self.data["positions"].pop(coin)
        exit_price = self._fill_price(price, p["side"], entering=False)
        exit_fee = p["qty"] * exit_price * FEE_PCT / 100
        direction = 1 if p["side"] == "LONG" else -1
        pnl = p["qty"] * (exit_price - p["entry"]) * direction - p["entry_fee"] - exit_fee
        if reason == "likidasyon":
            pnl = -p["margin"]  # marjinden fazlası kaybedilmez
        self.data["balance"] += pnl
        self.data["fees_paid"] += exit_fee
        held_hours = (now - p["opened"]) / 3600
        self.data["trades"].append({
            "coin": coin,
            "side": p["side"],
            "entry": p["entry"],
            "exit": exit_price,
            "qty": p["qty"],
            "margin": p["margin"],
            "notional": p["notional"],
            "pnl": round(pnl, 2),
            "pnl_pct_margin": round(pnl / p["margin"] * 100, 2),
            "fees": round(p["entry_fee"] + exit_fee, 2),
            "reason": reason,
            "opened": p["opened"],
            "closed": now,
            "held_hours": round(held_hours, 2),
            # giriş anındaki bağlam — analiz için
            "market": p.get("market", "kripto"),
            "score": p.get("score"),
            "width_pct": p.get("width_pct"),
            "touches": p.get("touches"),
            "drift_day_pct": p.get("drift_day_pct"),
            "swing_hours": p.get("swing_hours"),
            "expected_pct": p.get("expected_pct"),
        })
        self.data["cooldowns"][coin] = now + COOLDOWN_MIN * 60
        self._dirty = True
        log(f"[{self.key}] KAPANDI {coin} {p['side']} @{exit_price:.6g} · {reason} · "
            f"pnl {pnl:+.2f}$ · bakiye {self.data['balance']:.2f}$")

    def _check_entries(self, metrics, prices, zone, overshoot, min_profit, now) -> None:
        positions = self.data["positions"]
        if self.data["balance"] <= 0:
            return
        ordered = sorted(metrics.values(), key=lambda m: m.get("score", 0), reverse=True)
        for m in ordered:
            if len(positions) >= MAX_POSITIONS:
                break
            coin = m["coin"]
            if not m.get("ranging") or coin in positions:
                continue
            if now < self.data["cooldowns"].get(coin, 0):
                continue
            price = prices.get(coin) or m.get("last")
            height = m["band_high"] - m["band_low"]
            if not price or height <= 0:
                continue
            pos_now = (price - m["band_low"]) / height

            side = None
            if -overshoot <= pos_now <= zone:
                side, target = "LONG", m["band_high"]
            elif 1 - zone <= pos_now <= 1 + overshoot:
                side, target = "SHORT", m["band_low"]
            if not side or side not in self.sides:
                continue
            direction = 1 if side == "LONG" else -1
            expected_pct = (target - price) / price * 100 * direction
            # Hisse tarafının eşiği daha düşük — coin kendi eşiğini taşır
            if expected_pct < m.get("min_profit", min_profit):
                continue

            margin = self.data["balance"] / MAX_POSITIONS
            notional = margin * LEVERAGE
            entry = self._fill_price(price, side, entering=True)
            qty = notional / entry
            entry_fee = notional * FEE_PCT / 100
            self.data["fees_paid"] += entry_fee
            positions[coin] = {
                "side": side,
                "entry": entry,
                "qty": qty,
                "margin": margin,
                "notional": notional,
                "target": target,
                "band_low": m["band_low"],
                "band_high": m["band_high"],
                "opened": now,
                "last_price": price,
                "entry_fee": entry_fee,
                "score": m.get("score"),
                "width_pct": m.get("width_pct"),
                "touches": m.get("touches"),
                "drift_day_pct": m.get("drift_day_pct"),
                "swing_hours": m.get("swing_hours"),
                "market": m.get("market", "kripto"),
                "expected_pct": round(expected_pct, 2),
            }
            self._dirty = True
            log(f"[{self.key}] AÇILDI {coin} {side} @{entry:.6g} · "
                f"marjin {margin:.0f}$ × {LEVERAGE:g}x · hedef {target:.6g} "
                f"({expected_pct:+.1f}%)")

    def _equity(self, prices, metrics) -> float:
        equity = self.data["balance"]
        for coin, p in self.data["positions"].items():
            price = prices.get(coin) or (metrics.get(coin) or {}).get("last") or p.get("last_price")
            if price:
                equity += self._unrealized(p, price)
        return equity

    def _sample_equity(self, prices, metrics, now) -> None:
        samples = self.data["equity_samples"]
        if samples and now - samples[-1]["t"] < EQUITY_SAMPLE_SECONDS:
            return
        samples.append({"t": now, "equity": round(self._equity(prices, metrics), 2)})
        del samples[:-MAX_EQUITY_SAMPLES]
        self._dirty = True

    def _view(self, prices, metrics, now) -> dict:
        trades = self.data["trades"]
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        open_view = []
        for coin, p in self.data["positions"].items():
            price = prices.get(coin) or (metrics.get(coin) or {}).get("last") or p.get("last_price")
            upnl = self._unrealized(p, price) if price else 0.0
            open_view.append({
                "coin": coin, "side": p["side"], "entry": p["entry"],
                "price": price, "target": p["target"],
                "band_low": p["band_low"], "band_high": p["band_high"],
                "margin": p["margin"], "upnl": round(upnl, 2),
                "upnl_pct_margin": round(upnl / p["margin"] * 100, 2) if p["margin"] else 0,
                "held_hours": round((now - p["opened"]) / 3600, 2),
                "swing_hours": p.get("swing_hours"),
                "expected_pct": p.get("expected_pct"),
            })
        reason_counts: dict[str, int] = {}
        for t in trades:
            reason_counts[t["reason"]] = reason_counts.get(t["reason"], 0) + 1
        return {
            "key": self.key,
            "name": self.name,
            "start_balance": self.data["start_balance"],
            "balance": round(self.data["balance"], 2),
            "equity": round(self._equity(prices, metrics), 2),
            "fees_paid": round(self.data["fees_paid"], 2),
            "positions": open_view,
            "trades_total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else None,
            "avg_held_hours": round(sum(t["held_hours"] for t in trades) / len(trades), 1) if trades else None,
            "reason_counts": reason_counts,
            "recent_trades": trades[-20:][::-1],
            "equity_samples": self.data["equity_samples"][-200:],
            "since": self.data["created"],
        }


def make_simulators() -> list[Simulator]:
    return [
        Simulator(v["key"], v["name"], v.get("stop_mode", "none"),
                  v.get("stop_pct", 0.0), sides=v.get("sides"))
        for v in VARIANTS
    ]


def publish(views: list[dict], now: float) -> None:
    state.update("sim", {
        "updated": now,
        "enabled": True,
        "leverage": LEVERAGE,
        "fee_pct": FEE_PCT,
        "slippage_pct": SLIPPAGE_PCT,
        "max_positions": MAX_POSITIONS,
        "variants": views,
    })


def main() -> None:
    if not SIM_ENABLED:
        log("Simülasyon kapalı (SIM_ENABLED=0).")
        return
    sims = make_simulators()
    for sim in sims:
        sim.load()
    log(f"Simülasyon başladı ({len(sims)} strateji: "
        f"{', '.join(s.name for s in sims)}) · her biri {START_BALANCE:g}$ · "
        f"{LEVERAGE:g}x · {MAX_POSITIONS} slot · komisyon %{FEE_PCT:g}/taraf · "
        f"kayma %{SLIPPAGE_PCT:g} · durum: {STATE_DIR}")
    while True:
        try:
            snapshot = state.snapshot()
            ranges = snapshot.get("ranges") or {}
            funding = snapshot.get("funding") or {}
            # Hisse coinlerinin dakikalık fiyatı HL mid'lerinden gelir
            metrics = {m["coin"]: m for m in ranges.get("coins", []) if m.get("coin")}
            hl_prices = fetch_hl_prices(metrics)
            if hl_prices:
                funding = dict(funding)
                funding["coins"] = list(funding.get("coins", [])) + [
                    {"coin": c, "mark_price": p} for c, p in hl_prices.items()
                ]
            now = time.time()
            views = [sim.tick(ranges, funding, now) for sim in sims]
            publish(views, now)
        except Exception:
            log(f"Tick hatası:\n{traceback.format_exc()}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()

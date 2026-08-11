"""
Range Simülatörü (kağıt üzerinde)
─────────────────────────────────
Dashboard'ın önerdiği range işlemlerini otomatik uygular: range'deki coin
bandın kenarına gelince girer (alt bant → LONG, üst bant → SHORT), karşı
banda varınca kâr alır; bant kırılırsa/yapı bozulursa/süre aşılırsa çıkar.

Varsayılanlar: 10.000$ hesap, 10x kaldıraç, hesap 5 eşit slota bölünür
(slot başına marjin = bakiye/5, notional = marjin × kaldıraç). Taker
komisyonu ve kayma her iki yönde düşülür. Zarar marjini yerse likidasyon.

Her kapanan işlem, giriş anındaki bağlamla (skor, genişlik, beklenen tur
süresi…) birlikte kaydedilir — birkaç gün sonra "hangi range'ler para
kazandırıyor" analizi bu veriden yapılır. Durum SIM_STATE_FILE'a yazılır;
Railway'de kalıcı olması için Volume bağla (bkz. README).

Fiyat kaynağı: funding botunun her dakika çektiği mark price'lar +
range taramasının bant seviyeleri (state modülü üzerinden, ek API yok).
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import state

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
FEE_PCT = _env_num("SIM_FEE_PCT", 0.045)          # taraf başına taker %
SLIPPAGE_PCT = _env_num("SIM_SLIPPAGE_PCT", 0.02)  # taraf başına %
COOLDOWN_MIN = _env_num("SIM_COOLDOWN_MINUTES", 30)
TIME_STOP_MULT = _env_num("SIM_TIME_STOP_MULT", 3)  # beklenen tur × N; 0 = kapalı
TICK_SECONDS = int(_env_num("SIM_TICK_SECONDS", 60))
SAVE_EVERY_SECONDS = 300
EQUITY_SAMPLE_SECONDS = 3600
MAX_EQUITY_SAMPLES = 1000

STATE_FILE = Path(os.environ.get("SIM_STATE_FILE", "")
                  or Path(__file__).with_name("sim_state.json"))


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] [sim] {message}", flush=True)


# ── Simülatör ────────────────────────────────────────────────────────


class Simulator:
    def __init__(self, start_balance: float = START_BALANCE):
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
            if STATE_FILE.exists():
                saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and "balance" in saved:
                    self.data.update(saved)
                    log(f"Kayıtlı durum yüklendi: bakiye {self.data['balance']:.2f}$, "
                        f"{len(self.data['trades'])} kapanmış işlem, "
                        f"{len(self.data['positions'])} açık pozisyon.")
        except Exception as error:
            log(f"Durum dosyası okunamadı, sıfırdan başlıyor: {error}")

    def save(self, force: bool = False) -> None:
        if not force and not self._dirty and time.time() - self._last_save < SAVE_EVERY_SECONDS:
            return
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(STATE_FILE)
            self._last_save = time.time()
            self._dirty = False
        except Exception as error:
            log(f"Durum kaydedilemedi: {error}")

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

    # ── çekirdek ──
    def tick(self, ranges: dict, funding: dict, now: float | None = None) -> None:
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

        self._check_exits(metrics, prices, zone, overshoot, now)
        self._check_entries(metrics, prices, zone, overshoot, min_profit, now)
        self._sample_equity(prices, metrics, now)
        self._publish(prices, metrics, now)
        self.save()

    def _check_exits(self, metrics, prices, zone, overshoot, now) -> None:
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
            height = p["band_high"] - p["band_low"]
            if height <= 0:
                continue
            pos_now = (price - p["band_low"]) / height
            upnl = self._unrealized(p, price)

            reason = None
            if upnl <= -p["margin"]:
                reason = "likidasyon"
            elif p["side"] == "LONG" and price >= p["target"]:
                reason = "hedef"
            elif p["side"] == "SHORT" and price <= p["target"]:
                reason = "hedef"
            elif p["side"] == "LONG" and pos_now <= -overshoot:
                reason = "stop (bant kırıldı)"
            elif p["side"] == "SHORT" and pos_now >= 1 + overshoot:
                reason = "stop (bant kırıldı)"
            elif m is not None and not m.get("ranging"):
                reason = "yapı bozuldu"
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
            "score": p.get("score"),
            "width_pct": p.get("width_pct"),
            "touches": p.get("touches"),
            "drift_day_pct": p.get("drift_day_pct"),
            "swing_hours": p.get("swing_hours"),
            "expected_pct": p.get("expected_pct"),
        })
        self.data["cooldowns"][coin] = now + COOLDOWN_MIN * 60
        self._dirty = True
        log(f"KAPANDI {coin} {p['side']} @{exit_price:.6g} · {reason} · "
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
            if not side:
                continue
            direction = 1 if side == "LONG" else -1
            expected_pct = (target - price) / price * 100 * direction
            if expected_pct < min_profit:
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
                "expected_pct": round(expected_pct, 2),
            }
            self._dirty = True
            log(f"AÇILDI {coin} {side} @{entry:.6g} · marjin {margin:.0f}$ × {LEVERAGE:g}x "
                f"· hedef {target:.6g} ({expected_pct:+.1f}%)")

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

    def _publish(self, prices, metrics, now) -> None:
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
        equity = self._equity(prices, metrics)
        reason_counts: dict[str, int] = {}
        for t in trades:
            reason_counts[t["reason"]] = reason_counts.get(t["reason"], 0) + 1
        state.update("sim", {
            "updated": now,
            "enabled": True,
            "start_balance": self.data["start_balance"],
            "balance": round(self.data["balance"], 2),
            "equity": round(equity, 2),
            "fees_paid": round(self.data["fees_paid"], 2),
            "leverage": LEVERAGE,
            "fee_pct": FEE_PCT,
            "slippage_pct": SLIPPAGE_PCT,
            "max_positions": MAX_POSITIONS,
            "positions": open_view,
            "trades_total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else None,
            "avg_held_hours": round(sum(t["held_hours"] for t in trades) / len(trades), 1) if trades else None,
            "reason_counts": reason_counts,
            "recent_trades": trades[-30:][::-1],
            "equity_samples": self.data["equity_samples"][-200:],
            "since": self.data["created"],
        })


def main() -> None:
    if not SIM_ENABLED:
        log("Simülasyon kapalı (SIM_ENABLED=0).")
        return
    sim = Simulator()
    sim.load()
    log(f"Simülasyon başladı: {sim.data['balance']:.2f}$ · {LEVERAGE:g}x · "
        f"{MAX_POSITIONS} slot · komisyon %{FEE_PCT:g}/taraf · kayma %{SLIPPAGE_PCT:g} · "
        f"durum: {STATE_FILE}")
    while True:
        try:
            snapshot = state.snapshot()
            sim.tick(snapshot.get("ranges") or {}, snapshot.get("funding") or {})
        except Exception:
            log(f"Tick hatası:\n{traceback.format_exc()}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()

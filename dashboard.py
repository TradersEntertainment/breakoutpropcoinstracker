"""
Dashboard
─────────
Range finder + funding botunun canlı karar ekranı. Sayfa açılınca ilk
bölüm "ne yapmalı"yı söyler: funding arb'ı ve range kenarlarından türeyen
LONG/SHORT aksiyonları, henüz tetiklenmemiş yaklaşan fırsatlarla birlikte.
Railway'de domain verildiğinde (Settings → Networking → Generate Domain)
dışarıdan erişilir; veri süreç içindeki `state` modülünden gelir ve sayfa
60 saniyede bir /api/state'i yeniden çeker.

Renkler dataviz referans paletinin dark değerleridir (tek seri rengi +
statü renkleri). LONG yeşil / SHORT kırmızı kodlaması her yerde etiketle
birlikte kullanılır, renk tek başına anlam taşımaz.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot
import state
from bot import log

HTML = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Breakout Prop Tracker</title>
<style>
  :root {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --border: rgba(255,255,255,0.10);
    --s1: #3987e5;          /* seri 1 (mavi, dark adımı) */
    --good: #0ca30c;
    --warning: #fab219;
    --critical: #d03b3b;
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--page);
    color: var(--ink);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 20px;
    max-width: 1180px;
    margin: 0 auto;
  }
  h1 { font-size: 19px; font-weight: 650; letter-spacing: .2px; }
  h2 { font-size: 13px; font-weight: 600; color: var(--ink-2);
       text-transform: uppercase; letter-spacing: .8px; margin: 26px 0 10px; }
  header { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: baseline; }
  .stamp { color: var(--muted); font-size: 12px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr));
           gap: 10px; margin-top: 14px; }
  .tile { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 12px 14px; }
  .tile .lbl { color: var(--muted); font-size: 12px; }
  .tile .val { font-size: 26px; font-weight: 600; margin-top: 2px; }
  .tile .sub { color: var(--ink-2); font-size: 12px; margin-top: 2px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px,1fr));
           gap: 12px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 12px 14px; }
  .card .top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .card .coin { font-size: 16px; font-weight: 650; }
  .card .sym { color: var(--muted); font-size: 11px; }
  .chip { margin-left: auto; font-size: 11px; font-weight: 600;
          padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border);
          color: var(--ink-2); white-space: nowrap; }
  .chip.on { color: var(--good); border-color: rgba(12,163,12,.4); }
  .badge { font-size: 13px; font-weight: 700; padding: 3px 12px; border-radius: 7px;
           letter-spacing: .4px; white-space: nowrap; }
  .badge.long  { color: var(--good); background: rgba(12,163,12,.14);
                 border: 1px solid rgba(12,163,12,.45); }
  .badge.short { color: var(--critical); background: rgba(208,59,59,.14);
                 border: 1px solid rgba(208,59,59,.45); }
  .badge.wait  { color: var(--ink-2); background: rgba(255,255,255,.05);
                 border: 1px solid var(--border); }
  .src { margin-left: auto; color: var(--muted); font-size: 11px; }
  .act .rows { margin-top: 8px; display: grid; gap: 4px; font-size: 13px;
               color: var(--ink-2); }
  .act .rows b { color: var(--ink); font-weight: 600; }
  .act .why { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .act.hurry { border-color: rgba(250,178,25,.5); }
  .hurrytag { color: var(--warning); font-size: 12px; font-weight: 600; }
  .meta { display: flex; flex-wrap: wrap; gap: 4px 12px; color: var(--ink-2);
          font-size: 12px; margin-top: 6px; }
  .meta b { color: var(--ink); font-weight: 600; }
  .plan { margin-top: 8px; font-size: 12px; color: var(--ink-2);
          display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  svg { display: block; width: 100%; height: auto; margin-top: 8px; }
  .posbar { position: relative; height: 6px; border-radius: 3px; background: var(--grid);
            margin-top: 10px; }
  .posbar .zone { position: absolute; top: 0; bottom: 0; background: rgba(57,135,229,.12);
                  border-radius: 3px; }
  .posbar .dot { position: absolute; top: 50%; width: 10px; height: 10px;
                 border-radius: 50%; background: var(--s1);
                 border: 2px solid var(--surface); transform: translate(-50%,-50%); }
  .poslbl { display: flex; justify-content: space-between; color: var(--muted);
            font-size: 11px; margin-top: 4px; }
  .watch { background: var(--surface); border: 1px solid var(--border);
           border-radius: 10px; margin-top: 12px; }
  .watch .row { display: flex; gap: 10px; align-items: baseline; padding: 8px 14px;
                border-bottom: 1px solid var(--grid); font-size: 13px; color: var(--ink-2);
                flex-wrap: wrap; }
  .watch .row:last-child { border-bottom: none; }
  .watch b { color: var(--ink); }
  .watch .gap { margin-left: auto; color: var(--muted); font-size: 12px; }
  .tablewrap { overflow-x: auto; background: var(--surface); border: 1px solid var(--border);
               border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; min-width: 640px; }
  th { text-align: left; color: var(--muted); font-size: 11px; font-weight: 600;
       text-transform: uppercase; letter-spacing: .5px; padding: 9px 12px;
       border-bottom: 1px solid var(--grid); white-space: nowrap; }
  td { padding: 7px 12px; border-bottom: 1px solid var(--grid); white-space: nowrap;
       font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  td.coin { font-weight: 600; }
  td .why { color: var(--muted); font-size: 12px; }
  .bar { position: relative; height: 6px; width: 120px; border-radius: 3px;
         background: var(--grid); display: inline-block; vertical-align: middle; }
  .bar i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px;
           background: var(--s1); }
  .bar .tick { position: absolute; top: -2px; bottom: -2px; width: 1px;
               background: var(--muted); }
  .hot td { background: rgba(250,178,25,.06); }
  .hot td:first-child { box-shadow: inset 3px 0 0 var(--warning); }
  .up { color: var(--good); } .down { color: var(--critical); }
  .empty { color: var(--muted); padding: 22px; text-align: center; }
  #tip { position: fixed; pointer-events: none; background: var(--page);
         border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px;
         font-size: 12px; color: var(--ink-2); display: none; z-index: 9; }
  .loading { opacity: .6; transition: opacity .2s; }
  footer { color: var(--muted); font-size: 12px; margin: 26px 0 8px; line-height: 1.7; }
</style>
</head>
<body>
<header>
  <h1>Breakout Prop Tracker</h1>
  <span class="stamp" id="stamp">veri bekleniyor…</span>
</header>

<div class="tiles" id="tiles"></div>

<h2>🎯 Ne yapmalı — şimdi</h2>
<div class="cards" id="actions"><div class="empty">İlk veri bekleniyor…</div></div>
<div class="watch" id="watch" style="display:none"></div>

<h2>📦 Range'deki coinler</h2>
<div class="cards" id="cards"><div class="empty">İlk tarama bekleniyor…</div></div>

<h2>Tüm coinler — range skoru</h2>
<div class="tablewrap"><table id="rangetable"></table></div>

<h2>💰 Funding (Binance)</h2>
<div class="tablewrap"><table id="fundingtable"></table></div>

<footer id="foot"></footer>
<div id="tip"></div>

<script>
const $ = id => document.getElementById(id);
let TZ = 3;

function ts(unix) {
  if (!unix) return "–";
  const d = new Date((unix + TZ * 3600) * 1000);
  return String(d.getUTCHours()).padStart(2,"0") + ":" + String(d.getUTCMinutes()).padStart(2,"0");
}
function ago(unix, now) {
  if (!unix) return "–";
  const m = Math.max(0, Math.round((now - unix) / 60));
  return m < 1 ? "az önce" : m < 60 ? m + " dk önce" : Math.floor(m/60) + "sa " + (m%60) + "dk önce";
}
function dur(mins) {
  return mins < 60 ? mins + " dk" : Math.floor(mins/60) + "sa " + (mins%60) + "dk";
}
function px(v) { return Math.round(v * 10) / 10; }
function fmtPrice(p) {
  if (!p && p !== 0) return "–";
  const dec = p >= 100 ? 2 : p >= 1 ? 4 : 6;
  return "$" + p.toLocaleString("tr-TR", {minimumFractionDigits: dec, maximumFractionDigits: dec});
}
function fmtUsd(v) {
  return (v >= 0 ? "+" : "−") + Math.abs(v).toLocaleString("tr-TR",
    {minimumFractionDigits: 2, maximumFractionDigits: 2}) + "$";
}
function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function badge(side) {
  if (side === "LONG") return '<span class="badge long">▲ LONG</span>';
  if (side === "SHORT") return '<span class="badge short">▼ SHORT</span>';
  return '<span class="badge wait">● BEKLE</span>';
}

/* ── Aksiyonlar: funding arb + range kenarları ── */
function fundingActions(fd, now) {
  const th = fd.threshold ?? 0.7, out = [];
  for (const c of fd.coins || []) {
    if (Math.abs(c.rate_pct) < th) continue;
    const side = c.rate_pct < 0 ? "SHORT" : "LONG";           // HL bacağı
    const hedge = c.rate_pct < 0 ? "Binance LONG" : "Binance SHORT";
    const hl = c.hl_rate_pct;
    const spread = hl === null || hl === undefined
      ? null : Math.abs(c.rate_pct) - Math.abs(hl);
    const mins = c.next_funding ? Math.max(0, Math.round((c.next_funding - now)/60)) : null;
    out.push({ kind: "funding", coin: c.coin, side, hedge, rate: c.rate_pct,
               interval: c.interval_h, hl, spread, mins, next: c.next_funding,
               size: fd.position_size || 10000,
               sort: Math.abs(c.rate_pct) });
  }
  return out.sort((a,b) => b.sort - a.sort);
}
function rangeActions(rg) {
  const zone = rg.edge_zone ?? 0.15, out = [];
  for (const m of rg.coins || []) {
    if (!m.ranging) continue;
    if (m.position <= zone) {
      out.push({ kind: "range", coin: m.coin, side: "LONG", m,
                 target: m.band_high, pot: (m.band_high - m.last) / m.last * 100,
                 sort: zone - m.position });
    } else if (m.position >= 1 - zone) {
      out.push({ kind: "range", coin: m.coin, side: "SHORT", m,
                 target: m.band_low, pot: (m.last - m.band_low) / m.last * 100,
                 sort: m.position - (1 - zone) });
    }
  }
  return out.sort((a,b) => b.sort - a.sort);
}
function actionCard(a) {
  if (a.kind === "funding") {
    const hurry = a.mins !== null && a.mins <= 60;
    const est = a.spread === null ? null : a.spread / 100 * a.size;
    return `<div class="card act${hurry ? " hurry" : ""}">
      <div class="top">${badge(a.side)}<span class="coin">${esc(a.coin)}</span>
        <span class="src">funding arb · HL bacağı</span></div>
      <div class="rows">
        <span>Binance funding <b>${(a.rate>0?"+":"") + a.rate.toFixed(4)}%</b> / ${a.interval}sa
          · HL ${a.hl === null || a.hl === undefined ? "veri yok"
               : (a.hl>0?"+":"") + a.hl.toFixed(4) + "%/1sa"}</span>
        ${a.spread === null ? "" :
          `<span>Fark <b>${(a.spread>0?"+":"") + a.spread.toFixed(4)}%</b>
           · ${a.size.toLocaleString("tr-TR")}$ bacak ≈ <b>${fmtUsd(est)}</b></span>`}
        <span>${a.mins === null ? "" :
          `⏳ Binance ödemesine <b>${dur(a.mins)}</b> (${ts(a.next)})`}
          ${hurry ? '<span class="hurrytag">— ödemeden önce gir</span>' : ""}</span>
      </div>
      <div class="why">HL'de ${a.side} · hedge: ${a.hedge} · ödeme sonrası çıkılabilir</div>
    </div>`;
  }
  const m = a.m;
  return `<div class="card act">
    <div class="top">${badge(a.side)}<span class="coin">${esc(a.coin)}</span>
      <span class="src">range ${a.side === "LONG" ? "alt" : "üst"} bandı</span></div>
    <div class="rows">
      <span>Fiyat <b>${fmtPrice(m.last)}</b> · konum <b>%${(m.position*100).toFixed(0)}</b>
        · skor ${m.score.toFixed(0)}</span>
      <span>Hedef ${a.side === "LONG" ? "üst" : "alt"} bant <b>${fmtPrice(a.target)}</b>
        (${a.pot >= 0 ? "+" : ""}%${a.pot.toFixed(1)})</span>
      <span>Bant ${fmtPrice(m.band_low)} – ${fmtPrice(m.band_high)}
        · genişlik %${m.width_pct.toFixed(1)}</span>
    </div>
    <div class="why">Gitgel: ${a.side === "LONG" ? "alt banttan al, üst banda sat"
      : "üst banttan sat, alt banttan geri al"} · bant dışına taşarsa iptal</div>
  </div>`;
}

/* ── Yaklaşanlar: henüz tetiklenmemiş adaylar ── */
function watchRows(fd, rg, now) {
  const th = fd.threshold ?? 0.7, zone = rg.edge_zone ?? 0.15, rows = [];
  for (const c of fd.coins || []) {
    const a = Math.abs(c.rate_pct);
    if (a >= th * 0.6 && a < th) {
      rows.push({ sort: th - a, html:
        `<span>💰 <b>${esc(c.coin)}</b> funding ${(c.rate_pct>0?"+":"") + c.rate_pct.toFixed(4)}%`
        + ` → olursa <b>HL ${c.rate_pct<0 ? "SHORT" : "LONG"}</b></span>`
        + `<span class="gap">eşiğe ${(th - a).toFixed(2)} puan</span>` });
    }
  }
  for (const m of rg.coins || []) {
    if (!m.ranging) continue;
    const p = m.position;
    if (p > zone && p <= 0.35) {
      rows.push({ sort: p - zone, html:
        `<span>📦 <b>${esc(m.coin)}</b> konum %${(p*100).toFixed(0)}`
        + ` → alt banda inerse <b>LONG</b></span>`
        + `<span class="gap">bölgeye %${((p - zone)*100).toFixed(0)}</span>` });
    } else if (p >= 0.65 && p < 1 - zone) {
      rows.push({ sort: (1 - zone) - p, html:
        `<span>📦 <b>${esc(m.coin)}</b> konum %${(p*100).toFixed(0)}`
        + ` → üst banda çıkarsa <b>SHORT</b></span>`
        + `<span class="gap">bölgeye %${(((1 - zone) - p)*100).toFixed(0)}</span>` });
    }
  }
  return rows.sort((a,b) => a.sort - b.sort).slice(0, 8);
}

/* Sparkline: eğimli kanal dolgusu (%10 seri rengi) + 2px kapanış çizgisi + uç noktası */
function sparkSVG(m, meta) {
  const w = 300, h = 84, p = 8, vals = m.spark || [];
  if (vals.length < 2) return "<svg viewBox='0 0 300 84'></svg>";
  const lo = Math.min(Math.min(...vals), m.band_low, m.band_low_start ?? m.band_low);
  const hi = Math.max(Math.max(...vals), m.band_high, m.band_high_start ?? m.band_high);
  const X = i => p + (w - 2*p) * i / (vals.length - 1);
  const Y = v => h - p - (h - 2*p) * (v - lo) / ((hi - lo) || 1);
  const n = vals.length - 1;
  const chan = `${px(X(0))},${px(Y(m.band_high_start ?? m.band_high))} ${px(X(n))},${px(Y(m.band_high))} ` +
               `${px(X(n))},${px(Y(m.band_low))} ${px(X(0))},${px(Y(m.band_low_start ?? m.band_low))}`;
  const line = vals.map((v,i) => `${px(X(i))},${px(Y(v))}`).join(" ");
  const last = vals[vals.length-1];
  const lblY = Y(last) < 20 ? px(Y(last)+14) : px(Y(last)-8);
  return `<svg viewBox="0 0 ${w} ${h}" data-spark="1">
    <polygon points="${chan}" fill="rgba(57,135,229,.10)"/>
    <polyline points="${px(X(0))},${px(Y(m.band_high_start ?? m.band_high))} ${px(X(n))},${px(Y(m.band_high))}"
      fill="none" stroke="var(--grid)" stroke-width="1"/>
    <polyline points="${px(X(0))},${px(Y(m.band_low_start ?? m.band_low))} ${px(X(n))},${px(Y(m.band_low))}"
      fill="none" stroke="var(--grid)" stroke-width="1"/>
    <polyline points="${line}" fill="none" stroke="var(--s1)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${px(X(n))}" cy="${px(Y(last))}" r="4.5" fill="var(--s1)"
      stroke="var(--surface)" stroke-width="2"/>
    <text x="${px(X(n)-6)}" y="${lblY}" text-anchor="end" font-size="10"
      fill="var(--ink-2)">${fmtPrice(last)}</text>
  </svg>`;
}

/* Kart üstünde imleç: en yakın muma tooltip */
function bindSpark(svg, m, meta) {
  const vals = m.spark || [];
  const stepMin = m.spark_step_min || meta.spark_step_min || 15;
  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(vals.length-1,
      Math.round((ev.clientX - r.left) / r.width * (vals.length-1))));
    const t = (meta.range_updated || 0) - (vals.length-1-i) * stepMin * 60;
    const tip = $("tip");
    tip.textContent = ts(t) + " · " + fmtPrice(vals[i]);
    tip.style.display = "block";
    tip.style.left = (ev.clientX + 12) + "px";
    tip.style.top = (ev.clientY + 12) + "px";
  });
  svg.addEventListener("mouseleave", () => { $("tip").style.display = "none"; });
}

function driftTxt(d) {
  const s = (d > 0 ? "+" : "") + d.toFixed(1) + "%/gün";
  return d <= -0.3 ? "↘️ " + s : d >= 0.3 ? "↗️ " + s : "➡️ " + s;
}

function render(data) {
  const now = data.meta.now || (Date.now()/1000);
  window.__now = now;
  TZ = data.meta.tz_offset_hours ?? 3;
  const rg = data.ranges || {}, fd = data.funding || {};
  const coins = rg.coins || [], fcoins = fd.coins || [];
  const ranging = coins.filter(c => c.ranging);
  const zone = rg.edge_zone ?? 0.15;
  const meta = { range_updated: rg.updated, spark_step_min: rg.spark_step_min || 15 };

  $("stamp").textContent =
    "range: " + ago(rg.updated, now) + " · funding: " + ago(fd.updated, now);

  /* Aksiyonlar */
  const fActs = fundingActions(fd, now);
  const rActs = rangeActions(rg);
  const acts = [...fActs, ...rActs];
  const longs = acts.filter(a => a.side === "LONG").length;
  const shorts = acts.length - longs;

  /* Özet kutuları */
  const topRange = coins[0];
  const topFund = fcoins[0];
  $("tiles").innerHTML = [
    { lbl: "Şimdi işlem", val: acts.length,
      sub: acts.length ? (longs + " long · " + shorts + " short") : "sinyal bekleniyor" },
    { lbl: "Range'de coin", val: ranging.length,
      sub: rg.updated ? (rg.interval + " · " + rg.lookback_hours + "sa pencere") : "bekleniyor" },
    { lbl: "En uç funding", val: topFund ? topFund.rate_pct.toFixed(3) + "%" : "–",
      sub: topFund ? (topFund.coin + " · eşik ±" + (fd.threshold ?? 0.7) + "%") : "" },
    { lbl: "İzlenen coin", val: fd.matched ?? coins.length,
      sub: (fd.unmatched || []).length ? (fd.unmatched.length + " eşleşmedi") : "" },
  ].map(t => `<div class="tile"><div class="lbl">${t.lbl}</div>` +
             `<div class="val">${t.val}</div><div class="sub">${esc(t.sub)}</div></div>`).join("");

  if (!fd.updated && !rg.updated) {
    $("actions").innerHTML = '<div class="empty">İlk veri bekleniyor…</div>';
  } else if (!acts.length) {
    $("actions").innerHTML =
      '<div class="empty">Şu an net sinyal yok — funding eşiği (±' + (fd.threshold ?? 0.7) +
      '%) veya range kenarı (%' + (zone*100).toFixed(0) + ') bekleniyor.' +
      ' Yaklaşanlar aşağıda.</div>';
  } else {
    $("actions").innerHTML = acts.map(actionCard).join("");
  }

  const watch = watchRows(fd, rg, now);
  if (watch.length) {
    $("watch").style.display = "";
    $("watch").innerHTML =
      '<div class="row" style="color:var(--muted);font-size:12px">📡 Yaklaşanlar</div>' +
      watch.map(w => `<div class="row">${w.html}</div>`).join("");
  } else {
    $("watch").style.display = "none";
  }

  /* Range kartları */
  if (!ranging.length) {
    $("cards").innerHTML = `<div class="empty">${rg.updated ?
      "Şu an kriterlere uyan range yok — bulununca burada görünür." :
      "İlk tarama bekleniyor…"}</div>`;
  } else {
    $("cards").innerHTML = ranging.map(m => {
      const posPct = Math.max(-20, Math.min(120, m.position * 100));
      const inLow = m.position <= zone, inHigh = m.position >= 1 - zone;
      const planNow = inLow ? badge("LONG") + " <b>şu an alt bantta</b>"
                    : inHigh ? badge("SHORT") + " <b>şu an üst bantta</b>"
                    : badge("BEKLE") + " kenar bekleniyor (konum %" + (m.position*100).toFixed(0) + ")";
      return `<div class="card">
        <div class="top"><span class="coin">${esc(m.coin)}</span>
          <span class="sym">${esc(m.symbol)}</span>
          <span class="chip on">✓ RANGE ${m.score.toFixed(0)}</span></div>
        <div class="meta">
          <span>genişlik <b>%${m.width_pct.toFixed(1)}</b></span>
          <span><b>${m.touches}</b> dokunuş</span>
          <span>${driftTxt(m.drift_day_pct)}</span>
        </div>
        ${sparkSVG(m, meta)}
        <div class="posbar"><span class="zone" style="left:0;width:${zone*100}%"></span>
          <span class="zone" style="right:0;width:${zone*100}%"></span>
          <span class="dot" style="left:${Math.max(0, Math.min(100, posPct))}%"></span></div>
        <div class="poslbl"><span>alt ${fmtPrice(m.band_low)} → LONG</span>
          <span>konum %${(m.position*100).toFixed(0)}</span>
          <span>SHORT ← üst ${fmtPrice(m.band_high)}</span></div>
        <div class="plan">${planNow}</div>
      </div>`;
    }).join("");
    document.querySelectorAll("#cards svg[data-spark]").forEach((svg, i) => {
      bindSpark(svg, ranging[i], meta);
    });
  }

  /* Skor tablosu */
  $("rangetable").innerHTML =
    "<tr><th>Coin</th><th>Skor</th><th>Genişlik</th><th>Dokunuş</th>" +
    "<th>Eğim</th><th>Konum</th><th>Durum</th></tr>" +
    (coins.length ? coins.map(m => `<tr>
      <td class="coin">${esc(m.coin)}</td>
      <td><span class="bar"><i style="width:${Math.min(100, m.score)}%"></i>` +
        `<span class="tick" style="left:${rg.score_enter ?? 60}%"></span></span> ` +
        `${m.score.toFixed(0)}</td>
      <td>%${m.width_pct.toFixed(1)}</td>
      <td>${m.touches}</td>
      <td>${(m.drift_day_pct > 0 ? "+" : "") + m.drift_day_pct.toFixed(1)}%/g</td>
      <td>%${(m.position*100).toFixed(0)}</td>
      <td>${m.ranging ? '<span class="chip on">✓ RANGE</span>'
                      : '<span class="why">' + esc((m.reasons||[]).join(", ") || "skor düşük") + "</span>"}</td>
    </tr>`).join("") : '<tr><td colspan="7" class="empty">İlk tarama bekleniyor…</td></tr>');

  /* Funding tablosu */
  const th = fd.threshold ?? 0.7;
  const maxAbs = Math.max(th * 1.4, ...fcoins.map(c => Math.abs(c.rate_pct)));
  $("fundingtable").innerHTML =
    "<tr><th>Coin</th><th>Funding</th><th>|Funding|</th><th>HL (1sa)</th>" +
    "<th>Periyot</th><th>Ödeme</th><th>Aksiyon</th></tr>" +
    (fcoins.length ? fcoins.slice(0, 40).map(c => {
      const hot = Math.abs(c.rate_pct) >= th;
      const mins = c.next_funding ? Math.max(0, Math.round((c.next_funding - now)/60)) : null;
      return `<tr class="${hot ? "hot" : ""}">
        <td class="coin">${hot ? "⚡ " : ""}${esc(c.coin)}</td>
        <td class="${c.rate_pct > 0 ? "up" : c.rate_pct < 0 ? "down" : ""}">` +
          `${c.rate_pct > 0 ? "▲" : c.rate_pct < 0 ? "▼" : ""} ` +
          `<span style="color:var(--ink)">${(c.rate_pct>0?"+":"") + c.rate_pct.toFixed(4)}%</span></td>
        <td><span class="bar"><i style="width:${Math.min(100, Math.abs(c.rate_pct)/maxAbs*100)}%"></i>` +
          `<span class="tick" style="left:${Math.min(100, th/maxAbs*100)}%"></span></span></td>
        <td>${c.hl_rate_pct === null || c.hl_rate_pct === undefined ? "–"
             : (c.hl_rate_pct>0?"+":"") + c.hl_rate_pct.toFixed(4) + "%"}</td>
        <td>${c.interval_h}sa</td>
        <td>${mins === null ? "–" : dur(mins) + " (" + ts(c.next_funding) + ")"}</td>
        <td>${hot ? badge(c.rate_pct < 0 ? "SHORT" : "LONG") : '<span class="why">–</span>'}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="7" class="empty">Veri bekleniyor…</td></tr>');

  $("foot").innerHTML =
    ((fd.unmatched || []).length ? "Binance'te eşleşmeyen: " + esc(fd.unmatched.join(", ")) + "<br>" : "") +
    "Funding aksiyonu HL bacağıdır (funding negatif → HL SHORT + Binance LONG; pozitif → tersi). " +
    "Range: %0 alt bant (LONG bölgesi), %100 üst bant (SHORT bölgesi), kenar bölgesi %" +
    ((zone)*100).toFixed(0) + ". Skor çubuğundaki çizgi giriş eşiği (" + (rg.score_enter ?? 60) + "), " +
    "funding çubuğundaki çizgi alarm eşiği (±" + th + "%). Saatler UTC+" + TZ +
    ". Sayfa 60 sn'de bir yenilenir. Bilgi amaçlıdır, emir vermez.";
}

async function refresh() {
  document.body.classList.add("loading");
  try {
    const res = await fetch("/api/state", {cache: "no-store"});
    render(await res.json());
  } catch (e) {
    $("stamp").textContent = "bağlantı hatası — tekrar denenecek";
  }
  document.body.classList.remove("loading");
}
refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            if self.path.startswith("/api/state"):
                payload = state.snapshot()
                payload.setdefault("meta", {})
                payload["meta"]["now"] = time.time()
                payload["meta"]["tz_offset_hours"] = bot.TZ_OFFSET_HOURS
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(payload).encode("utf-8"))
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
            elif self.path == "/health":
                self._send(200, "text/plain", b"ok")
            else:
                self._send(404, "text/plain", b"not found")
        except BrokenPipeError:
            pass

    def log_message(self, *args) -> None:  # gürültüyü kes
        pass


def serve() -> None:
    port = int(os.environ.get("PORT", "8080") or "8080")
    while True:
        try:
            log(f"Dashboard dinliyor: 0.0.0.0:{port}")
            ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
        except Exception as error:
            log(f"Dashboard sunucu hatası: {error} — 10 sn sonra tekrar")
            time.sleep(10)


if __name__ == "__main__":
    serve()

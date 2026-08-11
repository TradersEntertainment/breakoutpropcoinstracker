# Breakout Prop Tracker

Breakout Prop'ta (Hyperliquid) listeli coinler için üç parça, tek Railway servisi:

1. **Funding alarmı** → `CHAT_ID` kanalına: Binance funding'i mutlak değerce
   %0.7'yi geçen coinler (aşağıda).
2. **Range finder** → `RANGE_CHAT_ID` kanalına: bir kanal içinde gitgel yapan
   (alçalarak/yükselerek de olsa) coinler.
3. **Dashboard** → web sayfası: iki tarafın canlı durumu, sparkline'larla.

## 1) Funding alarmı

Binance Futures (USDT-M perpetual) funding oranı **mutlak değerce %0.7'yi
geçtiğinde** Telegram'a bildirim atar. Mantık: %1 hedef, 0.3 tolerans → alt
sınır ±%0.7, üst sınır yok (yani +0.9, -0.75, -2, -4 hepsi bildirim üretir;
+0.5 üretmez).

Mesajda karşılaştırma için Hyperliquid'in **saatlik** funding'i de gösterilir —
HL'de funding saat başı ödendiği için genelde çok daha küçüktür; fark arb fırsatıdır:

- Binance funding **negatif** (ör. -1%): funding'i LONG taraf toplar → Binance LONG + HL SHORT
- Binance funding **pozitif** (ör. +1%): funding'i SHORT taraf toplar → Binance SHORT + HL LONG

## Bildirim örneği

Başlık/footer yok. **İlk satır** telefon bildiriminde görünen satırdır; Binance
ödemesine kalan süre, funding'ler ve net fark oraya konur, ayrıntı altta:

```
⏳ 46 dk kaldı · 🔴 KAITO · Binance -0.7069%/8sa · HL -0.0125%/1sa · Fark +0.6944%
KAITOUSDT · $0.6612 · 24s -4.18%

💰 Binance: -0.7069% / 8sa  →  saatlik -0.0884%
🌊 Hyperliquid: -0.0125% / 1sa  →  8 saatte -0.1000%
⚖️ Fark: +0.6944% — Binance'in 8sa'lik ödemesi eksi HL'de 1 saatlik funding
   Her periyotta yakalarsan yıllık ~%760  ·  tam 8sa tutarsan +0.6069%

⏳ Binance ödemesi: 46 dk sonra (20:27)
🕐 HL ödemesi: 19 dk sonra (20:00)

📍 Binance LONG + HL SHORT
   Binance long 0.7069% alır (tek ödeme) · HL short saatte 0.0125% öder
   10.000$ bacak başına ≈ +69,44$ (funding'i al, ~1 saat tut)
```

### "Fark" nasıl hesaplanır

Binance funding'i **periyot sonunda tek seferde** ödenir (8 veya 4 saatte bir);
Hyperliquid'de ise **her saat başı** işler. Kurgu bu asimetriyi kullanır:
funding saatinden hemen önce gir, ödemeyi al, kısa süre sonra çık. O zaman
Binance'in tam periyot ödemesini alırsın ama HL'de sadece ~1 saatlik funding
ödersin.

```
Fark = Binance'in periyot ödemesi − HL'de 1 saatlik funding
     = 0.7069% − 0.0125% = 0.6944%
```

Pozisyonu tam periyot boyunca tutarsan HL 8 kez keser, o senaryo da ikinci
satırda ayrıca yazar (`tam 8sa tutarsan +0.6069%`). Yön otomatik seçilir: iki
kombinasyondan hangisi pozitif net veriyorsa o yazılır — HL'nin 1 saatlik
funding'i Binance'in tek ödemesini aşarsa yön kendiliğinden ters döner.
Saatler TR saatidir (`TZ_OFFSET_HOURS`).

## Nasıl çalışır

1. Açılışta `assets.json` içindeki prop listesini Binance'in güncel perpetual
   listesiyle eşler (`kPEPE → 1000PEPEUSDT` gibi dönüşümler otomatik) ve
   Telegram'a bir başlangıç raporu atar: kaç coin eşleşti, hangileri Binance'te yok.
2. Varsayılan olarak **60 saniyede bir** tüm funding oranlarını tek istekle çeker.
3. Eşiği geçen coinler için tek bir toplu bildirim atar. Aynı coin için spam
   olmaması adına **45 dk cooldown** vardır; funding bu sürede 0.3 puan daha
   yükselirse cooldown beklenmeden tekrar bildirir.
4. **Hatırlatma:** Binance funding saatine **30 dk ve 15 dk kala**, coin hâlâ
   eşiğin üstündeyse cooldown'a bakmadan tekrar bildirir — pozisyonu tam
   zamanında açabilmen için. Hatırlatma mesajları ⏰ ile başlar (ilk bildirim ⏳).
   Her işaret, her funding döngüsünde bir kez atar; funding eşiğin altına
   düşerse hatırlatma gelmez. Dakikalar `REMINDER_MINUTES` ile değişir
   (ör. `45,20,5`; boş bırakırsan hatırlatma kapanır).
5. Eşleşme listesi 6 saatte bir yenilenir (Binance'e yeni listelenen coinler
   otomatik dahil olur), günde bir "bot çalışıyor" özeti atar.

## 2) Range finder

Aynı coin listesini kullanır; **ayrı bir Telegram kanalına** (`RANGE_CHAT_ID`)
bildirir. Aranan form: sürekli alçalarak/yükselerek de olsa **bir bant içinde
gitgel yapan** fiyat (ör. KAITO'nun 0.66–0.72 arasında defalarca gidip gelmesi).

Nasıl bulur: son 24 saatin 15 dakikalık kapanışlarına doğrusal bir trend
uydurur (eğimli kanal), trendden arındırılmış seride bandı (p5–p95) çıkarır ve
şunları ölçer:

| Metrik | Kriter (varsayılan) |
|---|---|
| Bant dokunuşu (alt ↔ üst dönüşümlü) | ≥ 4 |
| Bant genişliği | %2 – %20 |
| Trendin banda oranı | ≤ 1.5× (eğimli kanal serbest, düz trend elenir) |
| Skor (dokunuş + genişlik + eğim + verimlilik) | ≥ 60 girer, < 45 çıkar |

Bildirimler:
- **📦 Range'e girdi** — skor, genişlik, dokunuş, bant seviyeleri, konum, eğim.
- **🎯 Alt/üst bant** — range'deki coin bandın %15'lik kenarına gelince
  (90 dk arayla; gitgel al-satı için giriş zamanlaması).
- **💥 Range kırıldı** — fiyat bandın dışına taştı ya da yapı bozuldu.

Konum: **%0 = alt bant, %100 = üst bant.** Skor histerezislidir (60 girer,
45'te çıkar) — sınırda titreyip spam yapmaz. Tarama 15 dk'da bir.

## 3) Dashboard

Servis bir web sayfası da sunar: range kartları (eğimli kanal + sparkline),
tüm coinlerin skor tablosu (elenme sebepleriyle) ve funding tablosu; 60 sn'de
bir kendini yeniler. Açmak için Railway'de: **Service → Settings → Networking
→ Generate Domain.** Çıkan adres dashboard'dur (`/api/state` ham JSON verir).

## Kurulum

### 1. Telegram botu

1. Telegram'da **@BotFather**'a `/newbot` yaz, token'ı al (`123456:ABC-...`).
2. Botuna herhangi bir mesaj at (ör. "merhaba").
3. Tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` aç,
   `"chat":{"id":123456789...}` içindeki sayı senin `CHAT_ID`'n.

**Range kanalı için:** ikinci bir grup/kanal aç, aynı botu ekle (kanalsa
yönetici yap), oraya bir mesaj at ve `getUpdates`'te görünen yeni id'yi
`RANGE_CHAT_ID` olarak kaydet (gruplarda `-` ile, kanallarda `-100` ile
başlar). `RANGE_CHAT_ID` boş kalırsa range mesajları sadece log'a yazılır.

### 2. Railway'e deploy

1. [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo** → bu repoyu seç.
2. Service → **Variables** sekmesinde ekle:
   - `TELEGRAM_TOKEN` = BotFather token'ı
   - `CHAT_ID` = funding kanalının id'si
   - `RANGE_CHAT_ID` = range kanalının id'si
3. Deploy et. Loglarda iki başlangıç raporu görmelisin; aynı raporlar ilgili
   Telegram kanallarına da düşer.
4. Dashboard için: Service → Settings → **Networking → Generate Domain**.

## Ayarlar (opsiyonel env değişkenleri)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `FUNDING_THRESHOLD` | `0.7` | Mutlak eşik (%). |
| `CHECK_INTERVAL_SECONDS` | `60` | Kontrol sıklığı. |
| `ALERT_COOLDOWN_MINUTES` | `45` | Aynı coin için tekrar bildirim süresi. |
| `REMINDER_MINUTES` | `30,15` | Funding'e kaç dk kala hatırlatsın (boş = kapalı). |
| `REALERT_DELTA` | `0.3` | Funding bu kadar puan artarsa cooldown'u bekleme. |
| `HEARTBEAT_HOURS` | `24` | "Çalışıyorum" özeti sıklığı (0 = kapat). |
| `MAPPING_REFRESH_HOURS` | `6` | Binance listesi yenileme sıklığı. |
| `POSITION_SIZE_USD` | `10000` | Kazanç örneğinin hesaplandığı bacak büyüklüğü. |
| `TZ_OFFSET_HOURS` | `3` | Mesajdaki saat gösterimi (TR = +3). |
| `RUN_ONCE` | — | `1` yapılırsa tek tarama yapıp çıkar (sadece test için). |

Range finder'a özel:

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `RANGE_CHAT_ID` | — | Range bildirimlerinin gideceği kanal (zorunlu). |
| `RANGE_INTERVAL` | `15m` | Mum periyodu (`5m`, `15m`, `30m`, `1h`…). |
| `RANGE_LOOKBACK_HOURS` | `24` | İncelenen pencere. |
| `RANGE_SCAN_MINUTES` | `15` | Tarama sıklığı. |
| `RANGE_MIN_WIDTH` / `RANGE_MAX_WIDTH` | `2` / `20` | Bant genişliği sınırları (%). |
| `RANGE_MIN_TOUCHES` | `4` | En az dönüşümlü bant dokunuşu. |
| `RANGE_MAX_DRIFT` | `1.5` | Trendin bant yüksekliğine oranı üst sınırı. |
| `RANGE_SCORE_ENTER` / `RANGE_SCORE_EXIT` | `60` / `45` | Giriş/çıkış skoru (histerezis). |
| `EDGE_ALERTS` | `1` | Alt/üst bant uyarıları (`0` = kapat). |
| `EDGE_ZONE` | `0.15` | Kenar bölgesi (bandın %15'i). |
| `EDGE_COOLDOWN_MINUTES` | `90` | Aynı kenar için tekrar uyarı aralığı. |

## Coin listesini güncelleme

Prop'a coin eklenince/çıkınca `assets.json` içindeki `assets` listesini düzenleyip
push'la — Railway otomatik yeniden deploy eder.

- **`overrides`**: otomatik eşleşme yanlış coine denk gelirse elle düzelt:
  `"overrides": { "LIT": "LITUSDT" }` ya da doğru sembol her neyse.
- **`exclude`**: hiç takip edilmesin istediklerin: `"exclude": ["CC"]`.

⚠️ Eşleştirme isim bazlıdır. Nadiren aynı ticker iki borsada **farklı projeye**
ait olabilir (ör. kısa/generik isimler: LIT, CC, MET, CHIP, STABLE gibi).
Başlangıç raporundaki listeyi bir kez gözden geçir; şüpheli olanı `exclude`'a at
veya `overrides` ile doğrula.

## Notlar

- Binance funding periyodu coine göre 8s veya 4s olabilir; mesajda periyot yazar.
- Bildirimler bilgilendirmedir, yatırım tavsiyesi değildir; pozisyon yönü ve
  büyüklüğü senin kararın.

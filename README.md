# Breakout Prop × Binance Funding Alarm Botu

Breakout Prop'ta (Hyperliquid) listeli coinleri Binance Futures (USDT-M perpetual)
ile eşler ve **Binance funding oranı mutlak değerce %0.7'yi geçtiğinde**
Telegram'a bildirim atar. Mantık: %1 hedef, 0.3 tolerans → alt sınır ±%0.7,
üst sınır yok (yani +0.9, -0.75, -2, -4 hepsi bildirim üretir; +0.5 üretmez).

Mesajda karşılaştırma için Hyperliquid'in **saatlik** funding'i de gösterilir —
HL'de funding saat başı ödendiği için genelde çok daha küçüktür; fark arb fırsatıdır:

- Binance funding **negatif** (ör. -1%): funding'i LONG taraf toplar → Binance LONG + HL SHORT
- Binance funding **pozitif** (ör. +1%): funding'i SHORT taraf toplar → Binance SHORT + HL LONG

## Bildirim örneği

Başlık/footer yok. **İlk satır** telefon bildiriminde görünen tek satırdır;
funding'ler ve net fark oraya konur, ayrıntı altta:

```
🔴 KAITO · Binance -0.7069% · HL -0.0125% · Fark +0.6069%
KAITOUSDT · $0.6612 · 24s -4.18%

💰 Binance: -0.7069% / 8sa  →  saatlik -0.0884%
🌊 Hyperliquid: -0.0125% / 1sa  →  8 saatte -0.1000%
⚖️ Fark: +0.6069% / 8sa  ·  yıllık ~%665

⏳ Binance ödemesi: 46 dk sonra (20:18)
🕐 HL ödemesi: 28 dk sonra (20:00)

📍 Binance LONG + HL SHORT
   Binance long 0.7069% alır · HL short 0.1000% öder
   10.000$ bacak başına ≈ +60,69$ / 8sa
```

**Fark** satırı iki bacaklı kurgunun net getirisidir: Binance bacağının aldığı
funding eksi HL bacağının ödediği funding. Yön otomatik seçilir — iki
kombinasyondan hangisi pozitif net veriyorsa o yazılır (HL funding'i Binance'i
aşarsa yön kendiliğinden ters döner). Saatler TR saatidir (`TZ_OFFSET_HOURS`).

## Nasıl çalışır

1. Açılışta `assets.json` içindeki prop listesini Binance'in güncel perpetual
   listesiyle eşler (`kPEPE → 1000PEPEUSDT` gibi dönüşümler otomatik) ve
   Telegram'a bir başlangıç raporu atar: kaç coin eşleşti, hangileri Binance'te yok.
2. Varsayılan olarak **60 saniyede bir** tüm funding oranlarını tek istekle çeker.
3. Eşiği geçen coinler için tek bir toplu bildirim atar. Aynı coin için spam
   olmaması adına **45 dk cooldown** vardır; funding bu sürede 0.3 puan daha
   yükselirse cooldown beklenmeden tekrar bildirir.
4. Eşleşme listesi 6 saatte bir yenilenir (Binance'e yeni listelenen coinler
   otomatik dahil olur), günde bir "bot çalışıyor" özeti atar.

## Kurulum

### 1. Telegram botu

1. Telegram'da **@BotFather**'a `/newbot` yaz, token'ı al (`123456:ABC-...`).
2. Botuna herhangi bir mesaj at (ör. "merhaba").
3. Tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` aç,
   `"chat":{"id":123456789...}` içindeki sayı senin `CHAT_ID`'n.

### 2. Railway'e deploy

1. [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo** → bu repoyu seç.
2. Service → **Variables** sekmesinde ekle:
   - `TELEGRAM_TOKEN` = BotFather token'ı
   - `CHAT_ID` = yukarıdaki chat id
3. Deploy et. Bot web servisi değil worker'dır, port/domain gerekmez.
   Loglarda başlangıç raporunu görmelisin; aynı rapor Telegram'a da düşer.

## Ayarlar (opsiyonel env değişkenleri)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `FUNDING_THRESHOLD` | `0.7` | Mutlak eşik (%). |
| `CHECK_INTERVAL_SECONDS` | `60` | Kontrol sıklığı. |
| `ALERT_COOLDOWN_MINUTES` | `45` | Aynı coin için tekrar bildirim süresi. |
| `REALERT_DELTA` | `0.3` | Funding bu kadar puan artarsa cooldown'u bekleme. |
| `HEARTBEAT_HOURS` | `24` | "Çalışıyorum" özeti sıklığı (0 = kapat). |
| `MAPPING_REFRESH_HOURS` | `6` | Binance listesi yenileme sıklığı. |
| `POSITION_SIZE_USD` | `10000` | Kazanç örneğinin hesaplandığı bacak büyüklüğü. |
| `TZ_OFFSET_HOURS` | `3` | Mesajdaki saat gösterimi (TR = +3). |
| `RUN_ONCE` | — | `1` yapılırsa tek tarama yapıp çıkar (sadece test için). |

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

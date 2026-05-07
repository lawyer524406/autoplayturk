# AutoPlayTurk Resolver

AutoPlay Türk uygulaması için **otomatik güncellenen YouTube M3U liste deposu**.

## Ne Yapar?

`sources/` klasöründeki master M3U dosyalarını **12 saatte bir** kontrol eder:
- Her YouTube videosu hâlâ erişilebilir mi? (yt-dlp ile probe)
- Erişilemez (kaldırılmış / login gerekli / telif) olanlar **silinir**
- Sağlıklı olanlar `output/` klasörüne yazılır
- Değişiklik varsa otomatik commit edilir

## Klasör Yapısı

```
sources/                  ← Master M3U'lar (manuel düzenle)
  yerlikarisik.m3u
  yabancikarisik.m3u
output/                   ← Otomatik üretilen temiz M3U'lar
  yerlikarisik.m3u
  yabancikarisik.m3u
  last_run.json           ← Son çalıştırma istatistikleri
scripts/
  resolve_youtube.py      ← Ana script
  requirements.txt        ← Python deps (yt-dlp)
.github/workflows/
  resolver.yml            ← GitHub Actions cron (12 saatte bir)
```

## Kullanım — AutoPlayTurk uygulamasından

Telefon / tablet / araç box'taki AutoPlay Türk:

1. **Klasör/Liste Yükle** modal'ını aç
2. **Sinema** sekmesine git
3. **URL** alanına yapıştır:
   - Yerli filmler: `https://raw.githubusercontent.com/lawyer524406/autoplayturk/main/output/yerlikarisik.m3u`
   - Yabancı filmler: `https://raw.githubusercontent.com/lawyer524406/autoplayturk/main/output/yabancikarisik.m3u`
4. Otomatik yüklenir → Sinema sekmesinde filmler hazır

## Master M3U'yu Güncelleme

Yeni filmler eklemek için:
1. Local'de `sources/yerlikarisik.m3u`'yu düzenle
2. Web UI'dan upload et (üzerine yaz)
3. Push sonrası workflow otomatik tetiklenir
4. ~10 dakika içinde `output/` güncellenir

## Manuel Tetikleme

GitHub'da:
- **Actions** sekmesi
- "AutoPlay Resolver" workflow'unu seç
- "Run workflow" → "Run workflow"
- ~10-15 dakikada biter, yeni `output/` commit'lenir

## Lisans

Public M3U liste paylaşımı. Filmler YouTube'un kendi platformunda kullanıcı yüklemesidir; bu repo sadece **erişilebilirlik bilgisi** sağlar.

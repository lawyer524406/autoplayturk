#!/usr/bin/env python3
"""
AutoPlayTurk YouTube M3U Resolver

Calistigi yer: GitHub Actions runner (ubuntu-latest)
Sikligi: 12 saatte bir (resolver.yml cron)

Mantik:
1. sources/ altindaki tum *.m3u dosyalarini bul
2. Her birinde her kanal icin:
   - URL watch?v=ID veya youtu.be/ID formatinda mi?
   - Logo URL'si img.youtube.com/vi/ID/... formatinda mi (validate)?
   - yt-dlp ile saglik check (process=False, hizli metadata):
     * status OK -> KEEP (orijinal entry'yi koru)
     * LOGIN_REQUIRED / UNAVAILABLE / COPYRIGHT -> DROP
3. output/ altina ayni isimle filtreli M3U yaz

Kullanici (AutoPlayTurk telefon/tablet/box):
  https://raw.githubusercontent.com/lawyer524406/autoplayturk/main/output/yerlikarisik.m3u
URL'sini Klasor/Liste Yukle -> URL alanina yapistirir, otomatik fetch edilir.

Performans:
  ThreadPoolExecutor (5 worker) ile paralel.
  Tahmin: 4000 video × ~1sn / 5 worker = ~13 dakika.
"""

import os
import re
import sys
import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import yt_dlp


# ============================================================
# Yapilandirma
# ============================================================
ROOT = Path(__file__).parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

# YouTube video ID extraction
YT_ID_RE = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})',
    re.IGNORECASE,
)
LOGO_ID_RE = re.compile(
    r'img\.youtube\.com/vi/([A-Za-z0-9_-]{11})/',
    re.IGNORECASE,
)

MAX_WORKERS = 8
PROBE_TIMEOUT = 12  # yt-dlp probe timeout (sn)
# CACHE: Onceki output'ta KEPT olan video ID'leri yeniden probe etme
# (zaten saglikliydi). Sadece source'a yeni eklenen ID'leri probe et.
# Force tam re-check icin workflow_dispatch'a 'force_full=true' input'u eklendi.
FORCE_FULL_RECHECK = os.environ.get('FORCE_FULL_RECHECK', '').lower() in ('1', 'true', 'yes')

# yt-dlp Sessiz, hizli metadata extraction
YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'skip_download': True,
    'extract_flat': False,
    # process=False -> format extraction yapma, sadece basic info.
    # Bu cok onemli — hiz icin kritik. Sadece id/title/uploader/duration alinir.
    'noplaylist': True,
    # User-Agent: yt-dlp default zaten OK
}


# ============================================================
# M3U parser/writer (hafif — sadece ihtiyacimiz olan)
# ============================================================
GV_RE = re.compile(r'googlevideo\.com/videoplayback', re.IGNORECASE)


def parse_m3u(text: str):
    """
    Donus: list of dict { 'extinf', 'extras', 'url', 'video_id' }
    Otomatik donusum: URL googlevideo ham CDN URL'siyse VE logo'dan video ID
    cikabiliyorsa, URL'yi watch?v=ID ile DEGISTIRIR. Boylece sources/ altina
    orijinal videoplayback URL'li (expire olmus) M3U konulsa bile output temiz
    watch?v= URL'leriyle uretilir; AutoPlayTurk yerel resolveVod ile fresh url alir.
    EXTVLCOPT (referer/UA) satirlari da dropplanir — gereksiz.
    """
    lines = text.splitlines()
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\r')
        if line.startswith('#EXTINF:'):
            extinf = line
            # Sonraki non-EXTVLCOPT non-comment satir = URL
            j = i + 1
            extras = []
            while j < len(lines):
                nxt = lines[j].rstrip('\r')
                if nxt.startswith('#EXTVLCOPT:'):
                    # YouTube watch?v= URL'leri icin EXTVLCOPT (referer/UA) gereksiz —
                    # AutoPlayTurk YoutubeResolver kendi UA + Referer'i ekler. Atla.
                    j += 1
                    continue
                if nxt.startswith('#KODI'):
                    extras.append(nxt)
                    j += 1
                    continue
                if nxt.startswith('#'):
                    break
                if nxt.strip() == '':
                    j += 1
                    continue
                # URL satiri
                url = nxt.strip()
                vid = None
                m = YT_ID_RE.search(url)
                if m:
                    vid = m.group(1)
                else:
                    # URL'de yoksa logo'dan dene
                    lm = LOGO_ID_RE.search(extinf)
                    if lm:
                        vid = lm.group(1)
                # OTOMATIK DONUSUM: googlevideo URL + logo'dan vid bulunduysa
                # URL'yi watch?v= ile degistir. resolver bunu output'a yazar.
                if vid and GV_RE.search(url):
                    url = f'https://www.youtube.com/watch?v={vid}'
                entries.append({
                    'extinf': extinf,
                    'extras': extras,
                    'url': url,
                    'video_id': vid,
                })
                i = j + 1
                break
            else:
                i = j
        else:
            i += 1
    return entries


def write_m3u(entries, path: Path):
    lines = ['#EXTM3U']
    for e in entries:
        lines.append(e['extinf'])
        for x in e.get('extras', []):
            lines.append(x)
        lines.append(e['url'])
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ============================================================
# yt-dlp probe
# ============================================================
def probe_video(video_id: str):
    """
    Donus: ('ok', None) | ('drop', reason)
    """
    url = f'https://www.youtube.com/watch?v={video_id}'
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            if info is None:
                return ('drop', 'no-info')
            # process=False ile bazi alan eksik olabilir, ama title/id genelde var.
            # availability: 'public', 'unlisted', 'private', 'subscriber_only', 'needs_auth'
            availability = info.get('availability')
            live_status = info.get('live_status')
            if availability in ('private', 'needs_auth', 'subscriber_only'):
                return ('drop', f'avail={availability}')
            # Eger title yoksa ve extractor 'youtube' degilse — yine drop
            if not info.get('title') and not info.get('id'):
                return ('drop', 'empty-info')
            return ('ok', None)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if 'unavailable' in msg or 'removed' in msg or 'private' in msg:
            return ('drop', 'unavailable')
        if 'sign in' in msg or 'login' in msg or 'age' in msg:
            # Age-restricted veya login gerekenler — AutoPlayTurk cozemiyor
            return ('drop', 'login-required')
        if 'copyright' in msg:
            return ('drop', 'copyright')
        # Diger hatalar — geçici olabilir, KEEP
        return ('ok', f'transient-error: {msg[:80]}')
    except Exception as e:
        # Network / timeout vs — geçici, KEEP (yanlislikla atmayalim)
        return ('ok', f'exception: {type(e).__name__}')


# ============================================================
# Ana akis
# ============================================================
def process_file(src_path: Path) -> dict:
    """Tek bir M3U dosyasini isle, output/ altina yaz, istatistik don."""
    print(f'\n=== {src_path.name} ===')
    text = src_path.read_text(encoding='utf-8', errors='replace')
    entries = parse_m3u(text)
    print(f'  Toplam giris: {len(entries)}')

    youtube_entries = [e for e in entries if e['video_id']]
    other_entries = [e for e in entries if not e['video_id']]
    print(f'  YouTube ID bulunan: {len(youtube_entries)}')
    print(f'  Diger / ID yok: {len(other_entries)} (olduklari gibi korunur)')

    # CACHE: onceki output'taki video ID'leri zaten verified — yeniden probe etme.
    # Sadece source'a yeni eklenen veya FORCE_FULL_RECHECK ise tum probe.
    out_path = OUTPUT_DIR / src_path.name
    cached_ids = set()
    if not FORCE_FULL_RECHECK and out_path.exists():
        try:
            prev_text = out_path.read_text(encoding='utf-8', errors='replace')
            prev_entries = parse_m3u(prev_text)
            cached_ids = {e['video_id'] for e in prev_entries if e['video_id']}
            print(f'  Cache: onceki output\'ta {len(cached_ids)} verified ID')
        except Exception as ex:
            print(f'  Cache okuma hatasi: {ex}')

    # Cache hit'leri otomatik kept'e ekle, sadece miss'leri probe et
    cache_kept = []
    to_probe = []
    for e in youtube_entries:
        if e['video_id'] in cached_ids:
            cache_kept.append(e)
        else:
            to_probe.append(e)
    print(f'  Cache hit: {len(cache_kept)}  Probe edilecek: {len(to_probe)}')

    # Paralel probe (sadece miss'ler)
    kept = list(cache_kept)
    dropped = []
    t0 = time.time()
    if to_probe:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(probe_video, e['video_id']): e for e in to_probe}
            for n, fut in enumerate(as_completed(futs), 1):
                e = futs[fut]
                try:
                    status, reason = fut.result(timeout=PROBE_TIMEOUT)
                except Exception as exc:
                    status, reason = 'ok', f'future-timeout: {exc}'
                if status == 'ok':
                    kept.append(e)
                else:
                    dropped.append((e, reason))
                if n % 100 == 0 or n == len(to_probe):
                    elapsed = time.time() - t0
                    rate = n / elapsed if elapsed > 0 else 0
                    print(f'  [{n}/{len(to_probe)}] kept={len(kept)} dropped={len(dropped)} ({rate:.1f} /sn)')

    elapsed = time.time() - t0
    print(f'  Toplam probe suresi: {elapsed:.1f}sn')
    print(f'  KEPT: {len(kept)} (cache:{len(cache_kept)} + new:{len(kept)-len(cache_kept)})')
    print(f'  DROPPED: {len(dropped)}')

    if dropped:
        print(f'  Drop ornekleri:')
        for e, r in dropped[:20]:
            print(f'    {e["video_id"]}: {r}')
        if len(dropped) > 20:
            print(f'    ... ve {len(dropped) - 20} tane daha')

    # Output yaz: kept + other (URL'i YouTube olmayanlar olduklari gibi)
    final_entries = kept + other_entries
    write_m3u(final_entries, out_path)
    print(f'  YAZILDI: {out_path} ({len(final_entries)} entry)')

    return {
        'file': src_path.name,
        'total': len(entries),
        'youtube': len(youtube_entries),
        'cache_hit': len(cache_kept),
        'newly_probed': len(to_probe),
        'kept': len(kept),
        'dropped': len(dropped),
        'elapsed_sec': round(elapsed, 1),
    }


def main():
    if not SOURCES_DIR.exists():
        print(f'HATA: {SOURCES_DIR} yok.', file=sys.stderr)
        sys.exit(1)

    m3u_files = sorted(SOURCES_DIR.glob('*.m3u')) + sorted(SOURCES_DIR.glob('*.m3u8'))
    if not m3u_files:
        print(f'sources/ altinda M3U dosyasi yok — atlandi.')
        return

    print(f'Bulunan M3U dosyalari: {len(m3u_files)}')
    for f in m3u_files:
        print(f'  - {f.name}')

    summary = []
    for src in m3u_files:
        try:
            stats = process_file(src)
            summary.append(stats)
        except Exception as e:
            print(f'HATA {src.name}: {e}', file=sys.stderr)
            summary.append({'file': src.name, 'error': str(e)})

    # Ozet log + JSON dosyasi (debug icin)
    print('\n========== OZET ==========')
    for s in summary:
        print(json.dumps(s, ensure_ascii=False))
    (OUTPUT_DIR / 'last_run.json').write_text(
        json.dumps({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'files': summary,
        }, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )


if __name__ == '__main__':
    main()

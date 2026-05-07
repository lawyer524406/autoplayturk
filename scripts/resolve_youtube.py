#!/usr/bin/env python3
"""
AutoPlayTurk YouTube M3U Resolver — oEmbed sürümü
yt-dlp yerine YouTube oEmbed API kullanir (ratelimit gevsek, ~50ms/istek).
"""

import os
import re
import sys
import time
import json
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


ROOT = Path(__file__).parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

YT_ID_RE = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})',
    re.IGNORECASE,
)
LOGO_ID_RE = re.compile(r'img\.youtube\.com/vi/([A-Za-z0-9_-]{11})/', re.IGNORECASE)
GV_RE = re.compile(r'googlevideo\.com/videoplayback', re.IGNORECASE)

MAX_WORKERS = 12
PROBE_TIMEOUT = 8
USER_AGENT = 'Mozilla/5.0 (Linux; Android 13; AutoPlayResolver/1.0) AppleWebKit/537.36'
FORCE_FULL_RECHECK = os.environ.get('FORCE_FULL_RECHECK', '').lower() in ('1', 'true', 'yes')


def parse_m3u(text: str):
    lines = text.splitlines()
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\r')
        if line.startswith('#EXTINF:'):
            extinf = line
            j = i + 1
            extras = []
            while j < len(lines):
                nxt = lines[j].rstrip('\r')
                if nxt.startswith('#EXTVLCOPT:'):
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
                url = nxt.strip()
                vid = None
                m = YT_ID_RE.search(url)
                if m:
                    vid = m.group(1)
                else:
                    lm = LOGO_ID_RE.search(extinf)
                    if lm:
                        vid = lm.group(1)
                if vid and GV_RE.search(url):
                    url = f'https://www.youtube.com/watch?v={vid}'
                entries.append({'extinf': extinf, 'extras': extras, 'url': url, 'video_id': vid})
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


def probe_video(video_id: str):
    url = ('https://www.youtube.com/oembed?'
           + urllib.parse.urlencode({
               'url': f'https://www.youtube.com/watch?v={video_id}',
               'format': 'json',
           }))
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            if resp.status == 200:
                try:
                    body = resp.read(2048)
                    if b'"title"' in body or b'"author_name"' in body:
                        return ('ok', None)
                    return ('ok', 'no-title-but-200')
                except Exception:
                    return ('ok', '200-noread')
            return ('ok', f'status={resp.status}')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return ('drop', f'http={e.code}')
        return ('ok', f'http={e.code}')
    except urllib.error.URLError as e:
        return ('ok', f'urlerror: {str(e.reason)[:60]}')
    except Exception as e:
        return ('ok', f'exception: {type(e).__name__}')


def process_file(src_path: Path) -> dict:
    print(f'\n=== {src_path.name} ===')
    text = src_path.read_text(encoding='utf-8', errors='replace')
    entries = parse_m3u(text)
    print(f'  Toplam giris: {len(entries)}')

    youtube_entries = [e for e in entries if e['video_id']]
    other_entries = [e for e in entries if not e['video_id']]
    print(f'  YouTube ID bulunan: {len(youtube_entries)}')
    print(f'  Diger: {len(other_entries)}')

    out_path = OUTPUT_DIR / src_path.name
    cached_ids = set()
    if not FORCE_FULL_RECHECK and out_path.exists():
        try:
            prev_text = out_path.read_text(encoding='utf-8', errors='replace')
            prev_entries = parse_m3u(prev_text)
            cached_ids = {e['video_id'] for e in prev_entries if e['video_id']}
            print(f'  Cache: {len(cached_ids)} verified ID')
        except Exception as ex:
            print(f'  Cache okuma hatasi: {ex}')

    cache_kept = []
    to_probe = []
    for e in youtube_entries:
        if e['video_id'] in cached_ids:
            cache_kept.append(e)
        else:
            to_probe.append(e)
    print(f'  Cache hit: {len(cache_kept)}  Probe: {len(to_probe)}')

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
                if n % 200 == 0 or n == len(to_probe):
                    elapsed = time.time() - t0
                    rate = n / elapsed if elapsed > 0 else 0
                    print(f'  [{n}/{len(to_probe)}] kept={len(kept)} dropped={len(dropped)} ({rate:.1f} /sn)')

    elapsed = time.time() - t0
    print(f'  Toplam probe: {elapsed:.1f}sn')
    print(f'  KEPT: {len(kept)} (cache:{len(cache_kept)} + new:{len(kept)-len(cache_kept)})')
    print(f'  DROPPED: {len(dropped)}')

    if dropped:
        print(f'  Drop ornekleri:')
        for e, r in dropped[:20]:
            print(f'    {e["video_id"]}: {r}')
        if len(dropped) > 20:
            print(f'    ... ve {len(dropped) - 20} tane daha')

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
        print(f'sources/ altinda M3U yok.')
        return

    print(f'Bulunan: {len(m3u_files)} M3U dosyasi')
    summary = []
    for src in m3u_files:
        try:
            stats = process_file(src)
            summary.append(stats)
        except Exception as e:
            print(f'HATA {src.name}: {e}', file=sys.stderr)
            summary.append({'file': src.name, 'error': str(e)})

    print('\n========== OZET ==========')
    for s in summary:
        print(json.dumps(s, ensure_ascii=False))
    (OUTPUT_DIR / 'last_run.json').write_text(
        json.dumps({'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
                    'files': summary}, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )


if __name__ == '__main__':
    main()

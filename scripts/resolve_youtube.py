#!/usr/bin/env python3
"""
AutoPlayTurk YouTube M3U Resolver — DAGITIM SURUMU (no health check)
Backend: sources/ -> output/ kopya + URL donusumu (googlevideo -> watch?v=).
AutoPlayTurk yerel resolveVod ile cihaz tarafinda saglik kontrolu yapar.
"""

import os
import re
import sys
import time
import json
from pathlib import Path


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


def process_file(src_path: Path) -> dict:
    print(f'\n=== {src_path.name} ===')
    text = src_path.read_text(encoding='utf-8', errors='replace')
    entries = parse_m3u(text)
    youtube_count = sum(1 for e in entries if e['video_id'])
    other_count = len(entries) - youtube_count
    out_path = OUTPUT_DIR / src_path.name
    write_m3u(entries, out_path)
    print(f'  Toplam: {len(entries)} (YouTube ID: {youtube_count}, Diger: {other_count})')
    print(f'  YAZILDI: {out_path}')
    return {
        'file': src_path.name,
        'total': len(entries),
        'youtube': youtube_count,
        'other': other_count,
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
    t_start = time.time()
    for src in m3u_files:
        try:
            stats = process_file(src)
            summary.append(stats)
        except Exception as e:
            print(f'HATA {src.name}: {e}', file=sys.stderr)
            summary.append({'file': src.name, 'error': str(e)})
    total_elapsed = time.time() - t_start

    print(f'\n========== OZET ({total_elapsed:.1f}sn) ==========')
    for s in summary:
        print(json.dumps(s, ensure_ascii=False))
    (OUTPUT_DIR / 'last_run.json').write_text(
        json.dumps({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'mode': 'distribution-only (no health check)',
            'elapsed_sec': round(total_elapsed, 2),
            'files': summary,
        }, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )


if __name__ == '__main__':
    main()

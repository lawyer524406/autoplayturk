#!/usr/bin/env python3
"""AutoPlayTurk Resolver — distribution + manifest + conditional EXTVLCOPT"""

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

RAW_BASE = 'https://raw.githubusercontent.com/lawyer524406/autoplayturk/main/output'

TAB_MAP = {
    # Sinema/film listeleri (YouTube watch?v= URL'leri)
    'kovboy.m3u':         'sinema',
    'yerlikarisik.m3u':   'sinema',
    'yabancikarisik.m3u': 'sinema',
    'yerlikomedi.m3u':    'sinema',
    'sinema.m3u':         'sinema',
    'RecTvSinema.m3u':    'sinema',
    'yt_kanal.m3u':       'sinema',
    'yt_kanal1.m3u':      'sinema',
    'yt_kanal2.m3u':      'sinema',
    'yt_kanal3.m3u':      'sinema',
    # TV kanal listeleri (HLS/canli yayin URL'leri)
    'ENSONSTREAMLENMİSFULLLİSTE.m3u': 'tv',
    'dunyamusictvkanallari.m3u':      'tv',
    'yabanci_sinematvkanallari.m3u':  'tv',
    'yabanci_yemektvkanallari.m3u':   'tv',
    'yabancitvsporkanallari.m3u':     'tv',
    'dunyabelgeseltv.m3u':            'tv',
    'dünyanewstv.m3u':                'tv',
    # Radyo akislari
    'turkradyolari.m3u':              'radyo',
}

def guess_tab_from_filename(fname):
    if fname in TAB_MAP:
        return TAB_MAP[fname]
    fl = fname.lower()
    if fl.startswith('tv') or fl.startswith('iptv'):  return 'tv'
    if fl.startswith('radyo') or fl.startswith('radio'): return 'radyo'
    if fl.startswith('muzik') or fl.startswith('music'): return 'muzik'
    if fl.startswith('video'): return 'video'
    return 'sinema'

YT_ID_RE = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})',
    re.IGNORECASE,
)
LOGO_ID_RE = re.compile(r'img\.youtube\.com/vi/([A-Za-z0-9_-]{11})/', re.IGNORECASE)
GV_RE = re.compile(r'googlevideo\.com/videoplayback', re.IGNORECASE)


def parse_m3u(text):
    """EXTVLCOPT KOSULLU: YouTube'da AT, diger CDN'lerde (RecTv) KORU."""
    lines = text.splitlines()
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\r')
        if line.startswith('#EXTINF:'):
            extinf = line
            j = i + 1
            extras = []
            pending_vlcopt = []
            while j < len(lines):
                nxt = lines[j].rstrip('\r')
                if nxt.startswith('#EXTVLCOPT:'):
                    pending_vlcopt.append(nxt); j += 1; continue
                if nxt.startswith('#KODI'):
                    extras.append(nxt); j += 1; continue
                if nxt.startswith('#'): break
                if nxt.strip() == '':
                    j += 1; continue
                url = nxt.strip()
                vid = None
                m = YT_ID_RE.search(url)
                if m:
                    vid = m.group(1)
                else:
                    lm = LOGO_ID_RE.search(extinf)
                    if lm: vid = lm.group(1)
                if vid and GV_RE.search(url):
                    url = f'https://www.youtube.com/watch?v={vid}'
                is_youtube = (
                    'youtube.com/watch' in url
                    or 'youtu.be/' in url
                    or 'youtube-nocookie.com/watch' in url
                    or 'googlevideo.com/videoplayback' in url
                )
                if not is_youtube and pending_vlcopt:
                    extras = pending_vlcopt + extras
                entries.append({'extinf': extinf, 'extras': extras, 'url': url, 'video_id': vid})
                i = j + 1
                break
            else:
                i = j
        else:
            i += 1
    return entries


def write_m3u(entries, path):
    lines = ['#EXTM3U']
    for e in entries:
        lines.append(e['extinf'])
        for x in e.get('extras', []):
            lines.append(x)
        lines.append(e['url'])
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def process_file(src_path):
    print(f'\n=== {src_path.name} ===')
    text = src_path.read_text(encoding='utf-8', errors='replace')
    entries = parse_m3u(text)
    youtube_count = sum(1 for e in entries if e['video_id'])
    other_count = len(entries) - youtube_count
    out_path = OUTPUT_DIR / src_path.name
    write_m3u(entries, out_path)
    print(f'  Toplam: {len(entries)} (YouTube ID: {youtube_count}, Diger: {other_count})')
    return {'file': src_path.name, 'total': len(entries), 'youtube': youtube_count, 'other': other_count}


def main():
    if not SOURCES_DIR.exists():
        print(f'HATA: {SOURCES_DIR} yok.', file=sys.stderr); sys.exit(1)

    m3u_files = sorted(SOURCES_DIR.glob('*.m3u')) + sorted(SOURCES_DIR.glob('*.m3u8'))
    if not m3u_files:
        print(f'sources/ altinda M3U yok.'); return

    print(f'Bulunan: {len(m3u_files)} M3U dosyasi')
    summary = []
    t_start = time.time()
    for src in m3u_files:
        try:
            summary.append(process_file(src))
        except Exception as e:
            print(f'HATA {src.name}: {e}', file=sys.stderr)
            summary.append({'file': src.name, 'error': str(e)})
    total_elapsed = time.time() - t_start

    manifest_lists = []
    for s in summary:
        if 'error' in s: continue
        fname = s['file']
        manifest_lists.append({
            'name': fname.replace('.m3u8', '').replace('.m3u', ''),
            'file': fname,
            'tab': guess_tab_from_filename(fname),
            'url': f'{RAW_BASE}/{fname}',
            'totalChannels': s.get('total', 0),
        })
    (OUTPUT_DIR / 'manifest.json').write_text(
        json.dumps({
            'name': 'AutoPlayTurk Resolver',
            'version': 1,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'lists': manifest_lists,
        }, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    print(f'\nMANIFEST yazildi: {len(manifest_lists)} liste')

    print(f'\n========== OZET ({total_elapsed:.1f}sn) ==========')
    for s in summary:
        print(json.dumps(s, ensure_ascii=False))
    (OUTPUT_DIR / 'last_run.json').write_text(
        json.dumps({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'mode': 'distribution + manifest + conditional EXTVLCOPT',
            'elapsed_sec': round(total_elapsed, 2),
            'files': summary,
        }, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )


if __name__ == '__main__':
    main()

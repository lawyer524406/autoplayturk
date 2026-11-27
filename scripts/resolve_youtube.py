#!/usr/bin/env python3
"""AutoPlayTurk Resolver — distribution + manifest + conditional EXTVLCOPT
+ YouTube erisilebilirlik kontrolu (oEmbed).

Her YouTube video ID'si YouTube oEmbed servisiyle kontrol edilir:
  200 -> saglam, listede kalir
  401 -> gomulu oynatma kapali (REMOVE_EMBED_DISABLED=true ise silinir)
  403 -> gizli / erisim yok -> silinir
  404 -> silinmis -> silinir
  diger / ag hatasi -> bilinmiyor, listede KALIR (guvenli taraf)

Sonuclar cache/yt_status.json icinde tutulur; her calismada sadece suresi
dolanlar yeniden kontrol edilir. Kontrol oncesi bilinen saglam bir video
(canary) denenir; o bile 200 donmezse (IP engeli / rate limit) bu calismada
hicbir video silinmez.
"""

import os
import re
import sys
import time
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
CACHE_DIR = ROOT / "cache"
CACHE_PATH = CACHE_DIR / "yt_status.json"

FORCE_FULL = os.environ.get('FORCE_FULL_RECHECK', 'false').lower() == 'true'
REMOVE_EMBED_DISABLED = os.environ.get('REMOVE_EMBED_DISABLED', 'true').lower() == 'true'
MAX_CHECKS = int(os.environ.get('MAX_CHECKS_PER_RUN', '8000'))
WORKERS = int(os.environ.get('CHECK_WORKERS', '6'))
TTL_OK = 7 * 86400        # saglam videolar haftada bir
TTL_BAD = 3 * 86400       # silinenler 3 gunde bir (geri gelirse listeye doner)
CANARY_ID = 'jNQXAC9IVRw'  # "Me at the zoo" - YouTube'un ilk videosu
OEMBED = 'https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v='
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

RAW_BASE = 'https://raw.githubusercontent.com/lawyer524406/autoplayturk/main/output'

TAB_MAP = {
    # Sinema/film listeleri (YouTube watch?v= URL'leri)
    'kovboy.m3u':         'sinema',
    'yerlikarisik.m3u':   'sinema',
    'yabancikarisik.m3u': 'sinema',
    'yerlikomedi.m3u':    'sinema',
    'sinema.m3u':         'sinema',
    'NetSinema.m3u':    'sinema',
    'yt_kanal.m3u':       'sinema',
    'yt_kanal1.m3u':      'sinema',
    'yt_kanal2.m3u':      'sinema',
    'yt_kanal3.m3u':      'sinema',
    # TV kanal listeleri (HLS/canli yayin URL'leri)
    'turktvkanallarifull.m3u':        'tv',
    'dunyamusictvkanallari.m3u':      'tv',
    'yabanci_sinematvkanallari.m3u':  'tv',
    'yabanci_yemektvkanallari.m3u':   'tv',
    'yabancitvsporkanallari.m3u':     'tv',
    'dunyabelgeseltv.m3u':            'tv',
    'dünyatvkanallari.m3u':           'tv',
    'vavoo.m3u': 'tv',
    # Radyo akislari
    'turkvedunyaradyolari.m3u':              'radyo',
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
GV_RE = re.compile(r'googlevideo\.com/', re.IGNORECASE)
TVG_URL_ID_RE = re.compile(r'tvg-url="[^"]*(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})', re.IGNORECASE)
# Canli yayin manifest linklerinde video ID yolda durur: .../id/<11 karakter>.1/...
GV_PATH_ID_RE = re.compile(r'googlevideo\.com/.*?/id/([A-Za-z0-9_-]{11})(?:\.\d+)?/', re.IGNORECASE)


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
                    lm = (LOGO_ID_RE.search(extinf) or TVG_URL_ID_RE.search(extinf)
                          or GV_PATH_ID_RE.search(url))
                    if lm: vid = lm.group(1)
                if vid and GV_RE.search(url):
                    # googlevideo linklerinin suresi birkac saatte dolar -> kalici watch linkine cevir
                    url = f'https://www.youtube.com/watch?v={vid}'
                is_youtube = (
                    'youtube.com/watch' in url
                    or 'youtu.be/' in url
                    or 'youtube-nocookie.com/watch' in url
                    or 'googlevideo.com/' in url
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


# ---------------------------------------------------------------- oEmbed ---

def load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_cache(cache):
    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':'), sort_keys=True), encoding='utf-8')


def oembed_status(vid, retries=3):
    """HTTP kodu (200/401/403/404/...) ya da ag hatasinda None."""
    req = urllib.request.Request(OEMBED + vid, headers={'User-Agent': UA, 'Accept-Language': 'tr,en;q=0.8'})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            return e.code
        except Exception:
            time.sleep(1 + attempt)
    return None


def is_removable(code):
    if code in (403, 404):
        return True
    if code == 401:
        return REMOVE_EMBED_DISABLED
    return False


def needs_check(cache, vid, now):
    if FORCE_FULL:
        return True
    c = cache.get(vid)
    if not c:
        return True
    ttl = TTL_OK if c.get('s') == 200 else TTL_BAD
    return now - c.get('t', 0) > ttl


def check_ids(all_ids, cache):
    """Cache'i gunceller. Kontrol yapilabildiyse True doner."""
    canary = oembed_status(CANARY_ID)
    print(f'Canary ({CANARY_ID}) oEmbed: {canary}')
    if canary != 200:
        print('UYARI: canary saglam donmedi (IP engeli / rate limit?). Bu calismada video SILINMEYECEK.')
        return False, 0
    now = int(time.time())
    todo = [v for v in sorted(all_ids) if needs_check(cache, v, now)]
    if len(todo) > MAX_CHECKS:
        print(f'{len(todo)} ID kontrol bekliyor, bu calismada ilk {MAX_CHECKS} kontrol edilecek.')
        todo = todo[:MAX_CHECKS]
    print(f'oEmbed kontrolu: {len(todo)} ID (toplam benzersiz {len(all_ids)})')
    done = 0
    unknown = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for vid, code in zip(todo, ex.map(oembed_status, todo)):
            done += 1
            if code is None or not (code == 200 or code in (401, 403, 404)):
                unknown += 1
                continue  # bilinmiyor -> cache'e yazma, sonraki calismada tekrar dene
            cache[vid] = {'s': code, 't': now}
            if done % 1000 == 0:
                print(f'  {done}/{len(todo)}')
    print(f'oEmbed bitti: {done} kontrol, {unknown} bilinmiyor')
    return True, done


def process_file(src_path, cache, filter_enabled):
    print(f'\n=== {src_path.name} ===')
    text = src_path.read_text(encoding='utf-8', errors='replace')
    entries = parse_m3u(text)
    youtube_count = sum(1 for e in entries if e['video_id'])
    other_count = len(entries) - youtube_count
    removed = {}
    kept = entries
    # Silme sadece sinema listelerinde yapilir; TV/radyo listelerine dokunulmaz
    # (canli yayin ID'leri degisir, uygulama kanal adindan yeniden bulabilir).
    if filter_enabled and guess_tab_from_filename(src_path.name) == 'sinema':
        kept = []
        for e in entries:
            vid = e['video_id']
            code = cache.get(vid, {}).get('s') if vid else None
            if vid and is_removable(code):
                removed[str(code)] = removed.get(str(code), 0) + 1
                continue
            kept.append(e)
    out_path = OUTPUT_DIR / src_path.name
    write_m3u(kept, out_path)
    n_removed = sum(removed.values())
    print(f'  Toplam: {len(entries)} (YouTube ID: {youtube_count}, Diger: {other_count}) '
          f'-> yazilan: {len(kept)}, silinen: {n_removed} {removed if removed else ""}')
    return {'file': src_path.name, 'total': len(kept), 'source_total': len(entries),
            'youtube': youtube_count, 'other': other_count, 'removed': removed}


def main():
    if not SOURCES_DIR.exists():
        print(f'HATA: {SOURCES_DIR} yok.', file=sys.stderr); sys.exit(1)

    m3u_files = sorted(SOURCES_DIR.glob('*.m3u')) + sorted(SOURCES_DIR.glob('*.m3u8'))
    if not m3u_files:
        print(f'sources/ altinda M3U yok.'); return

    print(f'Bulunan: {len(m3u_files)} M3U dosyasi')
    summary = []
    t_start = time.time()

    # 1) Tum YouTube ID'lerini topla ve oEmbed ile kontrol et
    cache = load_cache()
    all_ids = set()
    for src in m3u_files:
        try:
            for e in parse_m3u(src.read_text(encoding='utf-8', errors='replace')):
                if e['video_id']:
                    all_ids.add(e['video_id'])
        except Exception as e:
            print(f'HATA (ID toplama) {src.name}: {e}', file=sys.stderr)
    filter_enabled, n_checked = check_ids(all_ids, cache)
    # Listelerden cikan ID'leri cache'ten temizle (cache sismesin)
    for vid in list(cache):
        if vid not in all_ids:
            del cache[vid]
    save_cache(cache)

    # 2) sources/ -> output/
    for src in m3u_files:
        try:
            summary.append(process_file(src, cache, filter_enabled))
        except Exception as e:
            print(f'HATA {src.name}: {e}', file=sys.stderr)
            summary.append({'file': src.name, 'error': str(e)})
    total_elapsed = time.time() - t_start

    # sources/ karsiligi olmayan output dosyalari: dokunulmaz, sadece raporlanir
    src_names = {p.name for p in m3u_files}
    orphans = sorted(p.name for p in OUTPUT_DIR.iterdir()
                     if p.suffix in ('.m3u', '.m3u8') and p.name not in src_names)
    if orphans:
        print(f'\nUYARI: sources/ icinde olmayan output dosyalari (guncellenmiyor, dokunulmadi): {orphans}')

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
            'mode': 'distribution + manifest + conditional EXTVLCOPT + oEmbed check',
            'elapsed_sec': round(total_elapsed, 2),
            'oembed_check': {
                'enabled': filter_enabled,
                'checked_this_run': n_checked,
                'unique_ids': len(all_ids),
                'remove_embed_disabled': REMOVE_EMBED_DISABLED,
            },
            'orphan_outputs': orphans,
            'files': summary,
        }, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )


if __name__ == '__main__':
    main()

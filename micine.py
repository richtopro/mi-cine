#!/usr/bin/env python3
"""
Mi Cine — servidor
==================
Servidor ligero (solo librería estándar de Python 3.8+) para Ultra.cc o cualquier Linux.

- Lee tus pelis y series de una o varias carpetas (incluidas las montadas con rclone).
- Busca carátulas y tendencias en TMDB (una sola vez para todos los usuarios).
- Usuarios con contraseña y panel de administración para añadirlos y quitarlos.
- Sirve la web de Mi Cine y hace streaming de los vídeos (con saltos/Range).

Uso:
  python3 micine.py serve                 arranca el servidor
  python3 micine.py adduser NOMBRE [--admin]
  python3 micine.py passwd NOMBRE
  python3 micine.py deluser NOMBRE
  python3 micine.py users
  python3 micine.py scan                  escanea una vez y sale
"""
import concurrent.futures
import getpass
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get('MICINE_CONFIG', os.path.join(HERE, 'config.json'))
DAY = 86400

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "base_url": "/micine",
    "media": [{"name": "Ultra.cc", "path": "~/media"}],
    "tmdb_key": "",
    "data_dir": "~/.micine",
    "scan_interval_min": 30,
    "min_size_mb": 15,
    "session_days": 90,
    "cookie_secure": True,
    "exclude": [],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8') as fh:
            cfg.update(json.load(fh))
    cfg['base_url'] = '/' + cfg['base_url'].strip('/') if cfg['base_url'].strip('/') else ''
    cfg['data_dir'] = os.path.expanduser(cfg['data_dir'])
    for m in cfg['media']:
        m['path'] = os.path.expanduser(m['path'])
    os.makedirs(cfg['data_dir'], exist_ok=True)
    return cfg


CFG = load_config()


def log(*a):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *a, flush=True)


# ------------------------------------------------------------------ base de datos
_local = threading.local()
DB_LOCK = threading.Lock()


def db():
    c = getattr(_local, 'db', None)
    if c is None:
        c = sqlite3.connect(os.path.join(CFG['data_dir'], 'micine.db'), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        _local.db = c
    return c


def db_init():
    with DB_LOCK:
        c = db()
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, pw TEXT,
            admin INTEGER DEFAULT 0, disabled INTEGER DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created INTEGER, expires INTEGER, ua TEXT);
        CREATE TABLE IF NOT EXISTS stats(user_id INTEGER PRIMARY KEY, json TEXT, updated INTEGER);
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, root TEXT, rel TEXT, size INTEGER, mtime INTEGER, added INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k0 TEXT PRIMARY KEY, json TEXT, at INTEGER);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, at INTEGER);
        ''')
        c.commit()


def kv_get(k, max_age=None):
    r = db().execute('SELECT v, at FROM kv WHERE k=?', (k,)).fetchone()
    if not r or (max_age and time.time() - r['at'] > max_age):
        return None
    return json.loads(r['v'])


def kv_set(k, v):
    with DB_LOCK:
        db().execute('INSERT OR REPLACE INTO kv VALUES(?,?,?)', (k, json.dumps(v), int(time.time())))
        db().commit()


# ------------------------------------------------------------------ contraseñas y sesiones
def hash_pw(pw):
    salt = secrets.token_hex(16)
    it = 200000
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), it).hex()
    return 'pbkdf2$%d$%s$%s' % (it, salt, h)


def check_pw(pw, stored):
    try:
        _, it, salt, h = stored.split('$')
        h2 = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), int(it)).hex()
        return hmac.compare_digest(h, h2)
    except Exception:
        return False


def new_session(user_id, ua=''):
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    with DB_LOCK:
        db().execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (tok, user_id, now, now + CFG['session_days'] * DAY, ua[:200]))
        db().execute('DELETE FROM sessions WHERE expires < ?', (now,))
        db().commit()
    return tok


def user_for_token(tok):
    if not tok:
        return None
    r = db().execute('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                        WHERE s.token=? AND s.expires>? AND u.disabled=0''', (tok, int(time.time()))).fetchone()
    return dict(r) if r else None


FAILS = {}  # ip -> [timestamps]


def too_many_fails(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 900]
    return len(FAILS[ip]) >= 10


# ------------------------------------------------------------------ interpretar nombres
VIDEO_EXT = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.webm', '.ts', '.m2ts', '.mpg', '.mpeg', '.flv', '.3gp', '.ogv'}
VEXT = re.compile(r'\.(mkv|mp4|m4v|avi|mov|wmv|webm|ts|m2ts|mpe?g|flv|3gp|ogv)$', re.I)
JUNK = re.compile(r"\b(2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr10\+?|hdr|dolby ?vision|x26[45]|h ?26[45]|hevc|avc|xvid|divx|blu ?ray|bdrip|brrip|bdremux|remux|webrip|web ?dl|hdtv|hdrip|dvdrip|microhd|ac3|aac|dts(?: ?hd)?|ddp?5 ?1|truehd|atmos|10 ?bit|proper|repack|extended|unrated|directors cut|castellano|espanol|español|spanish|latino|dual|multi|subtitulad[oa]|subs|vose|esp|eng|screener|imax)\b", re.I)
SEASON_DIR = re.compile(r'^(?:temporada|season|temp|serie|s|t)\s*0*(\d{1,2})$', re.I)
GENERIC_DIR = re.compile(r'^(series|serie|tv|tv shows|peliculas|películas|pelis|movies|films|cine|videos|vídeos|media|multimedia|mi unidad|my drive|drive|descargas|downloads|anime|documentales)$', re.I)
PATS = [
    re.compile(r'^(.*?)\bS(\d{1,2}) ?E(\d{1,3})', re.I),
    re.compile(r'^(.*?)\b(\d{1,2})x(\d{1,3})\b', re.I),
    re.compile(r'^(.*?)\b(?:temporada|season|temp|t) ?(\d{1,2})\D{0,8}?(?:cap(?:itulo|ítulo)?|ep(?:isodio|isode)?|e) ?(\d{1,3})', re.I),
    re.compile(r'^(.*?)\bcap(?:itulo|ítulo)?\.? ?(\d)(\d{2})\b', re.I),
]


def norm(s):
    s = unicodedata.normalize('NFD', str(s or ''))
    s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn').lower().replace('&', ' and ')
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def clean(s):
    s = VEXT.sub('', s)
    s = re.sub(r'[\[{]((?:19|20)\d{2})[\]}]', r' \1 ', s)
    s = re.sub(r'\[[^\]]*\]|\{[^}]*\}', ' ', s)
    s = re.sub(r'[._]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def tidy(s):
    m = JUNK.search(s)
    if m and m.start() > 0:
        s = s[:m.start()]
    s = re.sub(r'[(\[][^)\]]*$', '', s)
    s = re.sub(r'\(\s*\)', '', s)
    s = re.sub(r'[\s\-–—:,.(+]+$', '', s)
    s = re.sub(r'^[\s\-–—]+', '', s)
    return s.strip()


def year_of(s):
    cur = time.gmtime().tm_year
    ms = [m for m in re.finditer(r'(?:^|[\s(\[])((?:19|20)\d{2})(?=$|[\s)\]])', s)
          if m.start() > 0 and int(m.group(1)) <= cur + 1]
    return ms[-1] if ms else None


def parse_name(name, path):
    base = clean(name)
    dirs = [clean(d) for d in path]
    series_dir = next((d for d in dirs if not SEASON_DIR.match(d) and not GENERIC_DIR.match(d)), '')
    for rx in PATS:
        m = rx.search(base)
        if m:
            t = tidy(m.group(1))
            y = None
            ym = year_of(t)
            if ym:
                y = int(ym.group(1))
                t = tidy(t[:ym.start()])
            if not t:
                t = tidy(series_dir)
            return {'type': 'tv', 'title': t or base, 'year': y, 'season': int(m.group(2)), 'episode': int(m.group(3))}
    sd = next((d for d in dirs if SEASON_DIR.match(d)), None)
    if sd:
        em = re.search(r'(?:^|\D)(?:e|ep|episodio|episode|cap(?:itulo|ítulo)?)?[ .]?0*(\d{1,3})(?=\D|$)', base, re.I)
        if em:
            return {'type': 'tv', 'title': tidy(series_dir) or 'Serie', 'year': None,
                    'season': int(SEASON_DIR.match(sd).group(1)), 'episode': int(em.group(1))}
    ym = year_of(base)
    title = base[:ym.start()] if ym else base
    title = tidy(title) or tidy(base) or base
    return {'type': 'movie', 'title': title, 'year': int(ym.group(1)) if ym else None}


# ------------------------------------------------------------------ TMDB
def tmdb(path, **params):
    key = CFG.get('tmdb_key', '')
    if not key:
        raise RuntimeError('Falta tmdb_key en config.json')
    params['language'] = 'es-ES'
    headers = {'Accept': 'application/json', 'User-Agent': 'MiCine/1.0'}
    if key.startswith('eyJ'):
        headers['Authorization'] = 'Bearer ' + key
    else:
        params['api_key'] = key
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})
    req = urllib.request.Request('https://api.themoviedb.org/3' + path + '?' + q, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError('TMDB 429')


def compact(x, ty):
    return {'id': x['id'], 't': x.get('title') or x.get('name'), 'p': x.get('poster_path') or '', 'b': x.get('backdrop_path') or '',
            'o': x.get('overview') or '', 'g': x.get('genre_ids') or [], 'r': round(x.get('vote_average') or 0, 1),
            'v': x.get('vote_count') or 0, 'd': x.get('release_date') or x.get('first_air_date') or '',
            'pop': round(x.get('popularity') or 0), 'ty': ty}


def lookup(g):
    tries = []
    if g['type'] == 'movie':
        if g['year']:
            tries.append(('/search/movie', {'query': g['title'], 'year': g['year']}))
        tries += [('/search/movie', {'query': g['title']}), ('/search/tv', {'query': g['title']})]
    else:
        if g['year']:
            tries.append(('/search/tv', {'query': g['title'], 'first_air_date_year': g['year']}))
        tries += [('/search/tv', {'query': g['title']}), ('/search/multi', {'query': g['title']})]
    best = None
    for path, params in tries:
        try:
            r = tmdb(path, **params)
        except Exception as e:
            log('TMDB error', e)
            continue
        for x in r.get('results') or []:
            if path.endswith('multi') and x.get('media_type') not in ('movie', 'tv'):
                continue
            ty = 'movie' if 'movie' in path else 'tv' if '/tv' in path else x.get('media_type')
            best = compact(x, ty)
            break
        if best:
            break
    val = best or {'miss': int(time.time())}
    with DB_LOCK:
        db().execute('INSERT OR REPLACE INTO meta VALUES(?,?,?)', (g['k0'], json.dumps(val), int(time.time())))
        db().commit()


def refresh_trending(force=False):
    if not force and kv_get('trending', 6 * 3600) is not None:
        return
    reqs = [('/trending/all/day', {}, None)] + [('/trending/all/week', {'page': p}, None) for p in range(1, 6)] + \
           [('/movie/popular', {'region': 'ES'}, 'movie'), ('/tv/popular', {}, 'tv')]
    seen, out = set(), []
    for path, params, ty in reqs:
        try:
            res = tmdb(path, **params).get('results') or []
        except Exception as e:
            log('TMDB trending', e)
            continue
        for x in res:
            t = x.get('media_type') or ty
            if t not in ('movie', 'tv'):
                continue
            k = '%s:%s' % (t, x['id'])
            if k not in seen:
                seen.add(k)
                out.append(k)
    if out:
        kv_set('trending', out)


def refresh_genres():
    if kv_get('genres', 30 * DAY) is not None:
        return
    try:
        m = {}
        for p in ('/genre/movie/list', '/genre/tv/list'):
            for g in tmdb(p).get('genres', []):
                m[str(g['id'])] = g['name']
        m.update({'10759': 'Acción y aventura', '10765': 'Ciencia ficción y fantasía', '10768': 'Bélicas y política'})
        kv_set('genres', m)
    except Exception as e:
        log('TMDB genres', e)


CERT_AGE = {'A': 0, 'APTA': 0, 'TP': 0, 'ERI': 0, 'G': 0, 'U': 0, 'TV-Y': 0, 'TV-G': 0, 'ALL': 0, '0': 0,
            '7': 7, 'TV-Y7': 7, 'PG': 8, 'TV-PG': 8, '10': 10, '12': 12, 'PG-13': 12, '13': 13, 'TV-14': 14,
            '14': 14, '15': 15, '16': 16, 'R': 17, '18': 18, 'NC-17': 18, 'TV-MA': 18, 'X': 18}


def cert_age(c):
    c = (c or '').strip().upper()
    if c in CERT_AGE:
        return CERT_AGE[c]
    m = re.match(r'^\+?(\d{1,2})', c)
    return int(m.group(1)) if m else None


def fetch_details(key):
    """Ficha ampliada de TMDB (una vez por título): saga, duración y edad recomendada."""
    ty, tid = key.split(':', 1)
    try:
        if ty == 'movie':
            d = tmdb('/movie/%s' % tid, append_to_response='release_dates')
            col = d.get('belongs_to_collection')
            certs = {}
            for r in (d.get('release_dates') or {}).get('results', []):
                for rd in r.get('release_dates', []):
                    if rd.get('certification'):
                        certs.setdefault(r.get('iso_3166_1'), rd['certification'])
            rt = d.get('runtime') or 0
        else:
            d = tmdb('/tv/%s' % tid, append_to_response='content_ratings')
            col = None
            certs = {r.get('iso_3166_1'): r.get('rating') for r in (d.get('content_ratings') or {}).get('results', []) if r.get('rating')}
            rt = (d.get('episode_run_time') or [0])[0]
    except Exception as e:
        log('TMDB detalles', key, e)
        return
    age = None
    for cc in ('ES', 'US', 'GB', 'DE', 'FR'):
        if cc in certs:
            age = cert_age(certs[cc])
            if age is not None:
                break
    kv_set('det:' + key, {'col': [col['id'], col.get('name') or ''] if col else None, 'rt': rt or 0, 'age': age})


def load_details():
    return {r['k'][4:]: json.loads(r['v']) for r in db().execute("SELECT k, v FROM kv WHERE k LIKE 'det:%'")}


# ------------------------------------------------------------------ escaneo y biblioteca
STATUS = {'scanning': False, 'phase': '', 'done': 0, 'total': 0, 'files': 0, 'last_scan': 0, 'error': ''}
LIB = {'version': 0, 'json': b'{}', 'gz': gzip.compress(b'{}'), 'paths': {}}
LIB_LOCK = threading.Lock()
SCAN_EVENT = threading.Event()


def file_id(root_name, rel):
    return hashlib.sha1((root_name + '\0' + rel).encode('utf-8', 'surrogateescape')).hexdigest()[:12]


# carpetas que nunca contienen pelis: internas del Synology/Windows, papeleras, descargas a medias
SKIP_DIRS = re.compile(r'^[@#.$]|^(lost\+found|system volume information|recycle(\.bin)?|recycler|trash|papelera|incomplete|incompletos?|\.?tmp|temp)$', re.I)


def scan_files():
    min_size = CFG['min_size_mb'] * 1024 * 1024
    excl = {e.strip().lower() for e in CFG.get('exclude', []) if e.strip()}
    found = {}
    for root in CFG['media']:
        base = root['path']
        if not os.path.isdir(base):
            log('Carpeta no encontrada:', base)
            continue
        STATUS['phase'] = 'Leyendo ' + root['name']
        for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
            dirnames[:] = [d for d in dirnames if not SKIP_DIRS.search(d) and d.lower() not in excl]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in VIDEO_EXT or fn.startswith('.'):
                    continue
                if re.search(r'\bsample\b|\btrailer\b', fn, re.I):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size < min_size:
                    continue
                rel = os.path.relpath(full, base)
                found[file_id(root['name'], rel)] = (root['name'], rel, st.st_size, int(st.st_mtime))
                STATUS['files'] = len(found)
    now = int(time.time())
    with DB_LOCK:
        c = db()
        old = {r['id']: r['added'] for r in c.execute('SELECT id, added FROM files')}
        c.execute('DELETE FROM files')
        c.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
                      [(fid, r, rel, size, mt, old.get(fid) or min(mt, now)) for fid, (r, rel, size, mt) in found.items()])
        c.commit()
    return len(found), len(set(found) - set(old))


def load_files():
    out = []
    for r in db().execute('SELECT * FROM files'):
        parts = r['rel'].replace('\\', '/').split('/')
        out.append({'id': r['id'], 'root': r['root'], 'rel': r['rel'], 'name': parts[-1],
                    'path': list(reversed(parts[:-1]))[:4], 'size': r['size'], 'added': r['added']})
    return out


def load_meta():
    return {r['k0']: json.loads(r['json']) for r in db().execute('SELECT k0, json FROM meta')}


def group_files(files):
    groups = {}
    for f in files:
        info = parse_name(f['name'], f['path'])
        k0 = '%s|%s|%s' % (info['type'], norm(info['title']), (info['year'] or '') if info['type'] == 'movie' else '')
        g = groups.setdefault(k0, {'k0': k0, 'type': info['type'], 'title': info['title'], 'year': info['year'], 'files': []})
        if not g['year'] and info['year']:
            g['year'] = info['year']
        f2 = dict(f)
        f2['season'] = info.get('season')
        f2['episode'] = info.get('episode')
        g['files'].append(f2)
    return groups


def build_library():
    files = load_files()
    meta = load_meta()
    det = load_details()
    groups = group_files(files)
    items = {}
    for g in groups.values():
        m = meta.get(g['k0'])
        if m and m.get('hidden'):
            continue
        ok = bool(m) and not m.get('miss')
        ty = (m.get('ty') or g['type']) if ok else g['type']
        key = '%s:%s' % (ty, m['id']) if ok else 'x:' + g['k0']
        it = items.get(key)
        if not it:
            it = items[key] = {
                'key': key, 'type': ty, 'matched': ok, 'tmdbId': m['id'] if ok else None,
                'title': m['t'] if ok else g['title'], 'p': m['p'] if ok else '', 'b': m['b'] if ok else '',
                'o': m['o'] if ok else '', 'g': m['g'] if ok else [], 'r': m['r'] if ok else 0, 'v': m['v'] if ok else 0,
                'd': m['d'] if ok else ('%s-01-01' % g['year'] if g['year'] else ''), 'pop': m['pop'] if ok else 0, '_files': [], 'k0': []}
            dd = det.get(key)
            if dd:
                if dd.get('col'):
                    it['col'] = dd['col']
                if dd.get('rt'):
                    it['rt'] = dd['rt']
                if dd.get('age') is not None:
                    it['age'] = dd['age']
        it['_files'].extend(g['files'])
        it['k0'].append(g['k0'])
    paths = {}
    out_items, others = [], []
    for it in items.values():
        fs = it.pop('_files')
        for f in fs:
            paths[f['id']] = (f['root'], f['rel'])
        it['added'] = max(f['added'] for f in fs) * 1000
        if not it['matched'] and all(f.get('season') for f in fs):
            it['type'] = 'tv'
        if it['type'] == 'tv':
            by = {}
            for f in fs:
                k = (f.get('season') or 1, f.get('episode') or 0)
                if k not in by or f['size'] > by[k]['size']:
                    by[k] = dict(f, season=k[0], episode=k[1])
            eps = sorted(by.values(), key=lambda x: (x['season'], x['episode']))
            it['e'] = [[e['id'], e['season'], e['episode'], e['added']] for e in eps]
            it['newestEp'] = max(e['added'] for e in eps) * 1000
        else:
            fs.sort(key=lambda f: -f['size'])
            it['f'] = [[f['id'], f['name'], f['size'], f['added']] for f in fs]
        (out_items if it['matched'] else others).append(it)
    others.sort(key=lambda i: -i['added'])
    data = {'version': int(time.time() * 1000), 'items': out_items, 'others': others,
            'trending': kv_get('trending') or [], 'genres': kv_get('genres') or {}}
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    with LIB_LOCK:
        LIB.update(version=data['version'], json=raw, gz=gzip.compress(raw, 6), paths=paths,
                   keys=[i['key'] for i in out_items], det_keys=set(det))
    return groups, meta


def full_scan():
    if STATUS['scanning']:
        return
    STATUS.update(scanning=True, phase='Leyendo carpetas', done=0, total=0, error='')
    try:
        n, new = scan_files()
        log('Escaneo: %d vídeos (%d nuevos)' % (n, new))
        if CFG.get('tmdb_key'):
            STATUS['phase'] = 'Tendencias'
            refresh_genres()
            refresh_trending()
        groups, meta = build_library()
        pending = [g for g in groups.values()
                   if g['k0'] not in meta or (meta[g['k0']].get('miss') and time.time() - meta[g['k0']]['miss'] > 7 * DAY)]
        if pending and CFG.get('tmdb_key'):
            STATUS.update(phase='Buscando carátulas', done=0, total=len(pending))
            with concurrent.futures.ThreadPoolExecutor(6) as ex:
                for i, _ in enumerate(ex.map(lookup, pending), 1):
                    STATUS['done'] = i
                    if i % 200 == 0:
                        build_library()
            build_library()
        if CFG.get('tmdb_key'):
            with LIB_LOCK:
                need = [k for k in LIB.get('keys', []) if k not in LIB.get('det_keys', set())]
            if need:
                STATUS.update(phase='Edades y sagas', done=0, total=len(need))
                with concurrent.futures.ThreadPoolExecutor(6) as ex:
                    for i, _ in enumerate(ex.map(fetch_details, need), 1):
                        STATUS['done'] = i
                        if i % 300 == 0:
                            build_library()
                build_library()
        STATUS['last_scan'] = int(time.time())
    except Exception as e:
        STATUS['error'] = str(e)
        traceback.print_exc()
    finally:
        STATUS.update(scanning=False, phase='')


def scanner_loop():
    build_library()
    while True:
        full_scan()
        SCAN_EVENT.wait(CFG['scan_interval_min'] * 60)
        SCAN_EVENT.clear()


# ------------------------------------------------------------------ ffmpeg: audio compatible con navegadores
BROWSER_AUDIO = {'aac', 'mp3', 'opus', 'vorbis', 'flac'}
COPYABLE_VIDEO = {'h264', 'hevc', 'vp9', 'av1'}
SPANISH = {'spa', 'es', 'esp', 'spanish', 'cas', 'castellano', 'español'}


def tool(name):
    for cand in (CFG.get(name), shutil.which(name), os.path.expanduser('~/bin/' + name)):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def resolve(fid):
    with LIB_LOCK:
        loc = LIB['paths'].get(fid)
    if not loc:
        return None
    root = next((m for m in CFG['media'] if m['name'] == loc[0]), None)
    if not root:
        return None
    full = os.path.realpath(os.path.join(root['path'], loc[1]))
    if not full.startswith(os.path.realpath(root['path']) + os.sep) or not os.path.isfile(full):
        return None
    return full


def probe_file(fid, full):
    key = 'probe:%s:%d' % (fid, os.path.getsize(full))
    d = kv_get(key)
    if d is not None:
        return d
    fp = tool('ffprobe')
    if not fp:
        return None
    try:
        out = subprocess.run([fp, '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', full],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
        j = json.loads(out.stdout.decode('utf-8', 'replace') or '{}')
    except Exception as e:
        log('ffprobe', e)
        return None
    streams = j.get('streams') or []
    v = next((s for s in streams if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')), None)
    audio = []
    for s in streams:
        if s.get('codec_type') != 'audio':
            continue
        tags = s.get('tags') or {}
        audio.append({'index': s['index'], 'codec': s.get('codec_name', ''), 'lang': (tags.get('language') or '').lower(),
                      'title': tags.get('title') or '', 'ch': s.get('channels') or 0})
    d = {'duration': float((j.get('format') or {}).get('duration') or 0), 'vcodec': (v or {}).get('codec_name', ''), 'audio': audio}
    kv_set(key, d)
    return d


def audio_label(a):
    names = {'spa': 'Español', 'es': 'Español', 'eng': 'Inglés', 'en': 'Inglés', 'cat': 'Catalán', 'fre': 'Francés', 'fra': 'Francés',
             'ita': 'Italiano', 'ger': 'Alemán', 'deu': 'Alemán', 'jpn': 'Japonés', 'por': 'Portugués'}
    base = a['title'] or names.get(a['lang'], a['lang'].upper() or 'Pista %d' % a['index'])
    ch = {1: 'mono', 2: 'estéreo', 6: '5.1', 8: '7.1'}.get(a['ch'], '')
    return ' · '.join(x for x in (base, a['codec'].upper(), ch) if x)


def playback_plan(fid, full, no_mkv=False):
    """Decide si el navegador puede reproducir el archivo tal cual o hay que convertir el audio al vuelo."""
    ff = tool('ffmpeg')
    d = probe_file(fid, full)
    if not d:
        return {'ffmpeg': bool(ff), 'transcode': False, 'audio': [], 'duration': 0}
    audio = d['audio']
    pick = next((a for a in audio if a['lang'] in SPANISH and 'lat' not in a['title'].lower()), None) \
        or next((a for a in audio if a['lang'] in SPANISH), None) or (audio[0] if audio else None)
    ext = os.path.splitext(full)[1].lower()
    need = bool(pick) and (pick['codec'] not in BROWSER_AUDIO or pick is not audio[0]
                           or (ext == '.mkv' and no_mkv) or ext in ('.avi', '.ts', '.m2ts', '.mpg', '.mpeg', '.flv', '.wmv'))
    can = bool(ff) and d['vcodec'] in COPYABLE_VIDEO
    return {'ffmpeg': bool(ff), 'transcode': bool(need and can), 'duration': d['duration'], 'vcodec': d['vcodec'],
            'audio': [{'index': a['index'], 'label': audio_label(a)} for a in audio],
            'audio_index': pick['index'] if pick else None}


# ------------------------------------------------------------------ HTTP
# la web es el mismo index.html del repositorio (carpeta superior); 'web/' tiene prioridad si existe
WEB_DIR = os.path.join(HERE, 'web') if os.path.isdir(os.path.join(HERE, 'web')) else os.path.dirname(HERE)
STATIC = {'manifest.webmanifest', 'icon.svg', 'icon-192.png', 'icon-512.png'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'MiCine/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # --- utilidades
    def ip(self):
        return (self.headers.get('X-Forwarded-For') or self.client_address[0]).split(',')[0].strip()

    def token(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        ck = self.headers.get('Cookie', '')
        m = re.search(r'(?:^|;\s*)mc_session=([^;]+)', ck)
        if m:
            return m.group(1)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return (q.get('t') or [None])[0]

    def user(self):
        return user_for_token(self.token())

    def send(self, code, body=b'', ctype='application/json; charset=utf-8', headers=None, gz_ok=True):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        if gz_ok and len(body) > 1024 and 'gzip' in self.headers.get('Accept-Encoding', ''):
            body = gzip.compress(body, 6)
            headers = dict(headers or {}, **{'Content-Encoding': 'gzip'})
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def err(self, code, msg):
        self.send(code, {'error': msg})

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 4 * 1024 * 1024:
            raise ValueError('too big')
        raw = self.rfile.read(n) if n else b''
        return json.loads(raw.decode() or '{}')

    def route(self):
        p = urllib.parse.urlsplit(self.path).path
        base = CFG['base_url']
        if base and not (p == base or p.startswith(base + '/')):
            return None
        return p[len(base):] or '/'

    # --- métodos
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.err(500, 'error interno')
            except Exception:
                pass

    def do_POST(self):
        self._mutate('POST')

    def do_PUT(self):
        self._mutate('PUT')

    def do_DELETE(self):
        self._mutate('DELETE')

    def _mutate(self, method):
        try:
            if 'application/json' not in self.headers.get('Content-Type', '') and method != 'DELETE':
                return self.err(415, 'json requerido')
            self._api_write(method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.err(500, 'error interno')
            except Exception:
                pass

    def _get(self):
        r = self.route()
        if r is None:
            return self.err(404, 'no encontrado')
        if r == '/' and CFG['base_url'] and not urllib.parse.urlsplit(self.path).path.endswith('/'):
            self.send_response(301)
            self.send_header('Location', CFG['base_url'] + '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if r in ('/', '/index.html'):
            with open(os.path.join(WEB_DIR, 'index.html'), encoding='utf-8') as fh:
                html = fh.read().replace('<script src="config.js" onerror="void 0"></script>',
                                         '<script>window.MI_CINE_SERVER=true;</script>')
            return self.send(200, html, 'text/html; charset=utf-8')
        if r.lstrip('/') in STATIC:
            fp = os.path.join(WEB_DIR, r.lstrip('/'))
            with open(fp, 'rb') as fh:
                data = fh.read()
            ctype = 'application/manifest+json' if fp.endswith('.webmanifest') else (mimetypes.guess_type(fp)[0] or 'application/octet-stream')
            return self.send(200, data, ctype, {'Cache-Control': 'max-age=86400'}, gz_ok=False)
        if r.startswith('/stream/'):
            return self.stream(r[len('/stream/'):])
        if r.startswith('/api/'):
            return self._api_read(r[5:])
        self.err(404, 'no encontrado')

    def _api_read(self, ep):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if ep == 'health':
            return self.send(200, {'ok': True})
        u = self.user()
        if not u:
            return self.err(401, 'sesión requerida')
        if ep == 'token':
            return self.send(200, {'token': self.token()})
        if ep == 'me':
            return self.send(200, {'user': {'id': u['id'], 'username': u['username'], 'admin': bool(u['admin'])}})
        if ep == 'library':
            with LIB_LOCK:
                v, raw, gz = LIB['version'], LIB['json'], LIB['gz']
            if (q.get('v') or [''])[0] == str(v):
                return self.send(200, {'same': True, 'version': v})
            use_gz = 'gzip' in self.headers.get('Accept-Encoding', '')
            body = gz if use_gz else raw
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            if use_gz:
                self.send_header('Content-Encoding', 'gzip')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
            return
        if ep == 'stats':
            r = db().execute('SELECT json FROM stats WHERE user_id=?', (u['id'],)).fetchone()
            return self.send(200, r['json'] if r else '{}')
        if ep == 'probe':
            fid = (q.get('id') or [''])[0]
            full = resolve(fid)
            if not full:
                return self.err(404, 'vídeo no encontrado')
            return self.send(200, playback_plan(fid, full, (q.get('mkv') or ['1'])[0] == '0'))
        if ep == 'videos':
            ty, tid = (q.get('type') or [''])[0], (q.get('id') or [''])[0]
            if ty not in ('movie', 'tv') or not tid.isdigit():
                return self.err(400, 'parámetros')
            k = 'videos:%s:%s' % (ty, tid)
            data = kv_get(k, 7 * DAY)
            if data is None:
                try:
                    res = tmdb('/%s/%s/videos' % (ty, tid), include_video_language='es,en,null').get('results') or []
                    data = {'results': [{k2: v.get(k2) for k2 in ('key', 'site', 'type', 'iso_639_1', 'official')} for v in res]}
                    kv_set(k, data)
                except Exception:
                    data = {'results': []}
            return self.send(200, data)
        if ep == 'season':
            tv, s = (q.get('tv') or [''])[0], (q.get('s') or [''])[0]
            if not (tv.isdigit() and s.isdigit()):
                return self.err(400, 'parámetros')
            k = 'season:%s:%s' % (tv, s)
            data = kv_get(k, 7 * DAY)
            if data is None:
                try:
                    d = tmdb('/tv/%s/season/%s' % (tv, s))
                    data = {'episodes': [{k2: e.get(k2) for k2 in ('episode_number', 'name', 'overview', 'still_path', 'runtime')}
                                         for e in d.get('episodes', [])]}
                    kv_set(k, data)
                except Exception:
                    data = {'episodes': []}
            return self.send(200, data)
        if ep == 'admin/tmdbsearch' and u['admin']:
            qq = (q.get('q') or [''])[0].strip()
            ty = (q.get('type') or ['movie'])[0]
            if not qq or ty not in ('movie', 'tv'):
                return self.err(400, 'parámetros')
            try:
                res = tmdb('/search/%s' % ty, query=qq).get('results') or []
            except Exception as e:
                return self.err(502, 'TMDB: %s' % e)
            return self.send(200, {'results': [{'id': x['id'], 't': x.get('title') or x.get('name'),
                                                'y': (x.get('release_date') or x.get('first_air_date') or '')[:4],
                                                'p': x.get('poster_path') or '', 'o': (x.get('overview') or '')[:160]} for x in res[:12]]})
        if ep == 'admin/users' and u['admin']:
            rows = db().execute('''SELECT u.id, u.username, u.admin, u.disabled, u.created,
                                   (SELECT MAX(created) FROM sessions s WHERE s.user_id=u.id) AS last_login
                                   FROM users u ORDER BY u.username''').fetchall()
            return self.send(200, {'users': [dict(r) for r in rows]})
        if ep == 'admin/status' and u['admin']:
            with LIB_LOCK:
                n_items = LIB['json'].count(b'"key":')
            return self.send(200, dict(STATUS, items=n_items, ffmpeg=bool(tool('ffmpeg') and tool('ffprobe')), media=[{'name': m['name'], 'path': m['path'], 'ok': os.path.isdir(m['path'])} for m in CFG['media']]))
        self.err(404 if not ep.startswith('admin') else 403, 'no disponible')

    def _api_write(self, method):
        r = self.route()
        if r is None or not r.startswith('/api/'):
            return self.err(404, 'no encontrado')
        ep = r[5:]
        if ep == 'login' and method == 'POST':
            ip = self.ip()
            if too_many_fails(ip):
                return self.err(429, 'Demasiados intentos. Espera 15 minutos.')
            b = self.body()
            row = db().execute('SELECT * FROM users WHERE username=? AND disabled=0', (str(b.get('username', '')).strip(),)).fetchone()
            if not row or not check_pw(str(b.get('password', '')), row['pw']):
                FAILS.setdefault(ip, []).append(time.time())
                time.sleep(0.5)
                return self.err(401, 'Usuario o contraseña incorrectos')
            tok = new_session(row['id'], self.headers.get('User-Agent', ''))
            ck = 'mc_session=%s; Path=%s/; Max-Age=%d; HttpOnly; SameSite=Lax%s' % (
                tok, CFG['base_url'], CFG['session_days'] * DAY, '; Secure' if CFG['cookie_secure'] else '')
            return self.send(200, {'token': tok, 'user': {'id': row['id'], 'username': row['username'], 'admin': bool(row['admin'])}},
                             headers={'Set-Cookie': ck})
        u = self.user()
        if not u:
            return self.err(401, 'sesión requerida')
        if ep == 'logout':
            with DB_LOCK:
                db().execute('DELETE FROM sessions WHERE token=?', (self.token(),))
                db().commit()
            return self.send(200, {'ok': True}, headers={'Set-Cookie': 'mc_session=; Path=%s/; Max-Age=0' % CFG['base_url']})
        if ep == 'stats' and method == 'PUT':
            b = self.body()
            with DB_LOCK:
                db().execute('INSERT OR REPLACE INTO stats VALUES(?,?,?)', (u['id'], json.dumps(b), int(time.time())))
                db().commit()
            return self.send(200, {'ok': True})
        if ep == 'password' and method == 'POST':
            b = self.body()
            if not check_pw(str(b.get('old', '')), u['pw']):
                return self.err(400, 'La contraseña actual no es correcta')
            if len(str(b.get('new', ''))) < 6:
                return self.err(400, 'Mínimo 6 caracteres')
            with DB_LOCK:
                db().execute('UPDATE users SET pw=? WHERE id=?', (hash_pw(b['new']), u['id']))
                db().commit()
            return self.send(200, {'ok': True})
        if not u['admin']:
            return self.err(403, 'Solo administradores')
        if ep in ('admin/fix', 'admin/hide') and method == 'POST':
            b = self.body()
            k0s = [str(k) for k in (b.get('k0s') or []) if isinstance(k, str)][:50]
            if not k0s:
                return self.err(400, 'Falta el título')
            if ep == 'admin/hide':
                val = {'hidden': 1, 'manual': 1}
            else:
                ty, tid = b.get('type'), b.get('id')
                if ty not in ('movie', 'tv') or not str(tid).isdigit():
                    return self.err(400, 'parámetros')
                try:
                    d = tmdb('/%s/%s' % (ty, tid))
                except Exception as e:
                    return self.err(502, 'TMDB: %s' % e)
                d['genre_ids'] = [g['id'] for g in d.get('genres') or []]
                val = dict(compact(d, ty), manual=1)
                fetch_details('%s:%s' % (ty, tid))
            with DB_LOCK:
                for k0 in k0s:
                    db().execute('INSERT OR REPLACE INTO meta VALUES(?,?,?)', (k0, json.dumps(val), int(time.time())))
                db().commit()
            build_library()
            return self.send(200, {'ok': True})
        if ep == 'admin/scan' and method == 'POST':
            SCAN_EVENT.set()
            return self.send(200, {'ok': True})
        if ep == 'admin/users' and method == 'POST':
            b = self.body()
            name = str(b.get('username', '')).strip()
            pw = str(b.get('password', ''))
            if not re.match(r'^[\w.\-@ ]{2,32}$', name):
                return self.err(400, 'Nombre de usuario no válido (2-32 letras, números, . - _)')
            if len(pw) < 6:
                return self.err(400, 'La contraseña debe tener al menos 6 caracteres')
            try:
                with DB_LOCK:
                    db().execute('INSERT INTO users(username, pw, admin, created) VALUES(?,?,?,?)',
                                 (name, hash_pw(pw), 1 if b.get('admin') else 0, int(time.time())))
                    db().commit()
            except sqlite3.IntegrityError:
                return self.err(400, 'Ese usuario ya existe')
            return self.send(200, {'ok': True})
        m = re.match(r'^admin/users/(\d+)$', ep)
        if m:
            uid = int(m.group(1))
            target = db().execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not target:
                return self.err(404, 'Usuario no encontrado')
            if method == 'DELETE':
                if uid == u['id']:
                    return self.err(400, 'No puedes borrarte a ti mismo')
                with DB_LOCK:
                    for t in ('sessions', 'stats'):
                        db().execute('DELETE FROM %s WHERE user_id=?' % t, (uid,))
                    db().execute('DELETE FROM users WHERE id=?', (uid,))
                    db().commit()
                return self.send(200, {'ok': True})
            b = self.body()
            with DB_LOCK:
                if 'password' in b:
                    if len(str(b['password'])) < 6:
                        return self.err(400, 'La contraseña debe tener al menos 6 caracteres')
                    db().execute('UPDATE users SET pw=? WHERE id=?', (hash_pw(str(b['password'])), uid))
                    db().execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                if 'disabled' in b and uid != u['id']:
                    db().execute('UPDATE users SET disabled=? WHERE id=?', (1 if b['disabled'] else 0, uid))
                    if b['disabled']:
                        db().execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                if 'admin' in b and uid != u['id']:
                    db().execute('UPDATE users SET admin=? WHERE id=?', (1 if b['admin'] else 0, uid))
                db().commit()
            return self.send(200, {'ok': True})
        self.err(404, 'no encontrado')

    # --- streaming con soporte de saltos (Range)
    def stream(self, fid):
        if not self.user():
            return self.err(401, 'sesión requerida')
        fid = fid.split('?')[0].split('.')[0]
        full = resolve(fid)
        if not full:
            return self.err(404, 'vídeo no encontrado')
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if 'a' in q:
            return self.transcode(fid, full, q)
        size = os.path.getsize(full)
        ext = os.path.splitext(full)[1].lower()
        ctype = {'.mkv': 'video/x-matroska', '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
                 '.avi': 'video/x-msvideo', '.mov': 'video/quicktime', '.ts': 'video/mp2t'}.get(ext, 'application/octet-stream')
        start, end = 0, size - 1
        rng = self.headers.get('Range', '')
        m = re.match(r'bytes=(\d*)-(\d*)', rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'private, max-age=3600')
        self.end_headers()
        if self.command == 'HEAD':
            return
        with open(full, 'rb') as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(512 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


    def transcode(self, fid, full, q):
        """Vídeo copiado tal cual + audio convertido a AAC estéreo, en MP4 fragmentado (empieza en ?start=segundos)."""
        ff = tool('ffmpeg')
        if not ff:
            return self.err(501, 'ffmpeg no está instalado en el servidor')
        try:
            aidx = int(q['a'][0])
            start = max(0.0, float((q.get('start') or ['0'])[0]))
        except ValueError:
            return self.err(400, 'parámetros')
        d = probe_file(fid, full) or {}
        cmd = [ff, '-hide_banner', '-loglevel', 'error', '-nostdin']
        if start:
            cmd += ['-ss', '%.2f' % start]
        cmd += ['-i', full, '-map', '0:v:0', '-map', '0:%d' % aidx, '-c:v', 'copy']
        if d.get('vcodec') == 'hevc':
            cmd += ['-tag:v', 'hvc1']
        cmd += ['-c:a', 'aac', '-ac', '2', '-b:a', '192k', '-sn', '-dn', '-max_muxing_queue_size', '4096',
                '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof', 'pipe:1']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            if self.command == 'HEAD':
                return
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


# ------------------------------------------------------------------ línea de comandos
def ask_pw():
    while True:
        a = getpass.getpass('Contraseña (mín. 6): ')
        if len(a) < 6:
            print('Demasiado corta.')
            continue
        if getpass.getpass('Repite la contraseña: ') == a:
            return a
        print('No coinciden.')


def main():
    db_init()
    args = sys.argv[1:] or ['serve']
    cmd = args[0]
    if cmd == 'serve':
        threading.Thread(target=scanner_loop, daemon=True).start()
        srv = ThreadingHTTPServer((CFG['host'], int(CFG['port'])), Handler)
        srv.daemon_threads = True
        log('Mi Cine escuchando en http://%s:%s%s/' % (CFG['host'], CFG['port'], CFG['base_url']))
        srv.serve_forever()
    elif cmd == 'scan':
        full_scan()
        print('Listo:', STATUS)
    elif cmd == 'adduser' and len(args) >= 2:
        pw = ask_pw()
        try:
            db().execute('INSERT INTO users(username, pw, admin, created) VALUES(?,?,?,?)',
                         (args[1], hash_pw(pw), 1 if '--admin' in args else 0, int(time.time())))
            db().commit()
            print('Usuario creado:', args[1], '(administrador)' if '--admin' in args else '')
        except sqlite3.IntegrityError:
            print('Ese usuario ya existe.')
    elif cmd == 'passwd' and len(args) >= 2:
        pw = ask_pw()
        n = db().execute('UPDATE users SET pw=? WHERE username=?', (hash_pw(pw), args[1])).rowcount
        db().execute('DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)', (args[1],))
        db().commit()
        print('Contraseña cambiada.' if n else 'No existe ese usuario.')
    elif cmd == 'deluser' and len(args) >= 2:
        n = db().execute('DELETE FROM users WHERE username=?', (args[1],)).rowcount
        db().commit()
        print('Usuario borrado.' if n else 'No existe ese usuario.')
    elif cmd == 'users':
        for r in db().execute('SELECT username, admin, disabled FROM users ORDER BY username'):
            print(r['username'], '(admin)' if r['admin'] else '', '(desactivado)' if r['disabled'] else '')
    else:
        print(__doc__)


if __name__ == '__main__':
    main()

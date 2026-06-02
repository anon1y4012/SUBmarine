import os
import re
import sqlite3
import requests
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from defusedxml import ElementTree as ET
from flask import Flask, jsonify, render_template, request, Response

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('plexarr')

app = Flask(__name__)

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    return response

# --- Config ---
def _bounded_workers(value, fallback=8):
    try:
        return max(1, min(int(value), 32))
    except (TypeError, ValueError):
        return fallback

TMDB_API_KEY = os.getenv('TMDB_API_KEY', '')
PLEX_IP      = os.getenv('PLEX_IP', '')
PLEX_PORT    = os.getenv('PLEX_PORT', '32400')
PLEX_TOKEN   = os.getenv('PLEX_TOKEN', '')
MOVIE_LIB_ID = os.getenv('MOVIE_LIBRARY_ID', '1')
TV_LIB_ID    = os.getenv('TV_LIBRARY_ID', '2')
DB_PATH      = os.getenv('DB_PATH', '/data/submarine.db')
# How many parallel TMDB workers during sync (stay under rate limit)
SYNC_WORKERS = _bounded_workers(os.getenv('SYNC_WORKERS', '8'))
MAX_THUMB_BYTES = 10 * 1024 * 1024

log.info("=== CONFIG ===")
log.info(f"  PLEX_IP:          {PLEX_IP}")
log.info(f"  PLEX_PORT:        {PLEX_PORT}")
log.info(f"  PLEX_TOKEN:       {'SET' if PLEX_TOKEN else 'NOT SET'}")
log.info(f"  MOVIE_LIBRARY_ID: {MOVIE_LIB_ID}")
log.info(f"  TV_LIBRARY_ID:    {TV_LIB_ID}")
log.info(f"  TMDB_API_KEY:     {'SET' if TMDB_API_KEY else 'NOT SET'}")
log.info(f"  DB_PATH:          {DB_PATH}")
log.info(f"  SYNC_WORKERS:     {SYNC_WORKERS}")

# --- Services ---
# Canonical US subscription streaming services (flatrate only), ordered by prominence.
# These exact strings must match what TMDB returns after alias normalization.
SERVICES = [
    'Netflix',
    'Hulu',
    'Disney Plus',
    'Max',
    'Amazon Prime Video',
    'Apple TV',
    'Peacock Premium',
    'Paramount Plus',
    'Starz',
    'MGM Plus',
    'AMC+',
    'Shudder',
    'BritBox',
    'Acorn TV',
    'Criterion Channel',
    'MUBI',
    'Crunchyroll',
    'fuboTV',
    'Discovery+',
    'Tubi TV',
    'The Roku Channel',
    'Pluto TV',
    'Curiosity Stream',
    'Sundance Now',
    'HiDive',
    'Fandor',
    'Pure Flix',
    'ALLBLK',
    'History Vault',
    'Hallmark+',
    'PBS',
]

# Normalize TMDB provider names that have been renamed or have variants
PROVIDER_ALIASES = {
    'HBO Max':              'Max',
    'Apple TV+':            'Apple TV',
    'Peacock':              'Peacock Premium',
    'Paramount+':           'Paramount Plus',
    'Paramount Plus Premium': 'Paramount Plus',
    'Paramount Plus Essential': 'Paramount Plus',
    'MGM+':                 'MGM Plus',
    'AMC Plus':             'AMC+',
    'Discovery +':          'Discovery+',
    'Hallmark Movies Now':  'Hallmark+',
}

# --- DB Setup ---
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db

def init_db():
    log.info(f"Initializing DB at {DB_PATH}")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plex_rating_key TEXT UNIQUE,
            title TEXT NOT NULL,
            year TEXT,
            media_type TEXT NOT NULL,
            thumb_url TEXT,
            tmdb_id TEXT,
            last_updated REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS provider_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            FOREIGN KEY (title_id) REFERENCES titles(id),
            UNIQUE(title_id, provider_name)
        );
        CREATE TABLE IF NOT EXISTS sync_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_sync REAL DEFAULT 0,
            is_syncing INTEGER DEFAULT 0,
            sync_message TEXT DEFAULT 'Never synced',
            synced_count INTEGER DEFAULT 0,
            total_count INTEGER DEFAULT 0
        );
        INSERT OR IGNORE INTO sync_status (id, last_sync, is_syncing, sync_message)
        VALUES (1, 0, 0, 'Never synced');
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
    ''')

    # Migrate sync_status: add progress columns if missing
    existing_cols = {row[1] for row in db.execute("PRAGMA table_info(sync_status)")}
    for col, defval in [('synced_count', '0'), ('total_count', '0')]:
        if col not in existing_cols:
            log.info(f"Migrating DB: adding sync_status.{col}")
            db.execute(f"ALTER TABLE sync_status ADD COLUMN {col} INTEGER DEFAULT {defval}")

    # Migrate provider_links: drop leaving_date column if present (recreate table)
    pl_cols = {row[1] for row in db.execute("PRAGMA table_info(provider_links)")}
    if 'leaving_date' in pl_cols:
        log.info("Migrating DB: rebuilding provider_links without leaving_date")
        db.executescript('''
            CREATE TABLE provider_links_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title_id INTEGER NOT NULL,
                provider_name TEXT NOT NULL,
                FOREIGN KEY (title_id) REFERENCES titles(id),
                UNIQUE(title_id, provider_name)
            );
            INSERT OR IGNORE INTO provider_links_new (title_id, provider_name)
                SELECT title_id, provider_name FROM provider_links;
            DROP TABLE provider_links;
            ALTER TABLE provider_links_new RENAME TO provider_links;
        ''')
        log.info("Migration complete: provider_links rebuilt")

    # Seed default settings from env vars (only if key not already set)
    defaults = {
        'plex_ip':           os.getenv('PLEX_IP', ''),
        'plex_port':         os.getenv('PLEX_PORT', '32400'),
        'plex_token':        os.getenv('PLEX_TOKEN', ''),
        'movie_library_id':  os.getenv('MOVIE_LIBRARY_ID', '1'),
        'tv_library_id':     os.getenv('TV_LIBRARY_ID', '2'),
        'tmdb_api_key':      os.getenv('TMDB_API_KEY', ''),
        'sync_workers':      os.getenv('SYNC_WORKERS', '8'),
        'radarr_url':        '',
        'radarr_api_key':    '',
        'sonarr_url':        '',
        'sonarr_api_key':    '',
    }
    for k, v in defaults.items():
        db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))

    # Reset any stuck is_syncing flag from a previous crash
    db.execute("UPDATE sync_status SET is_syncing=0 WHERE is_syncing=1")

    # Mark setup as complete if core creds are already present (upgrade path)
    existing = get_all_settings_db(db)
    if existing.get('plex_token') and existing.get('tmdb_api_key'):
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('setup_complete', '1')")

    db.commit()
    db.close()
    log.info("DB initialized OK")

# --- Settings helpers ---
def get_all_settings_db(db):
    """Read all settings using an existing db connection (used inside init_db)."""
    try:
        rows = db.execute('SELECT key, value FROM settings').fetchall()
        return {r['key']: r['value'] for r in rows}
    except Exception:
        return {}

def is_setup_complete():
    """Return True if the user has completed first-run setup."""
    return get_setting('setup_complete') == '1'

def get_setting(key, fallback=''):
    """Read a setting from DB, falling back to env var default."""
    try:
        db = get_db()
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        db.close()
        return row['value'] if row and row['value'] else fallback
    except Exception:
        return fallback

def get_all_settings():
    try:
        db = get_db()
        rows = db.execute('SELECT key, value FROM settings').fetchall()
        db.close()
        return {r['key']: r['value'] for r in rows}
    except Exception:
        return {}

def get_sync_workers():
    """Read the configured worker count while enforcing a conservative ceiling."""
    return _bounded_workers(get_setting('sync_workers', str(SYNC_WORKERS)), SYNC_WORKERS)

# --- Plex ---
def _plex_headers(token):
    """Send the Plex token in a header so it cannot leak through URLs or logs."""
    return {'X-Plex-Token': token} if token else {}

def _without_plex_token(url):
    """Remove legacy Plex tokens from stored thumbnail URLs."""
    parts = urlsplit(url)
    query = urlencode([
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != 'x-plex-token'
    ])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

def fetch_plex_library(library_id, media_tag):
    _ip    = get_setting('plex_ip',    PLEX_IP)
    _port  = get_setting('plex_port',  PLEX_PORT)
    _token = get_setting('plex_token', PLEX_TOKEN)
    url = f'http://{_ip}:{_port}/library/sections/{library_id}/all'
    log.info(f"[PLEX] Fetching library {library_id} (tag=<{media_tag}>)")
    try:
        r = requests.get(url, headers=_plex_headers(_token), timeout=20)
        log.info(f"[PLEX] HTTP {r.status_code} — {len(r.content)} bytes")
        r.raise_for_status()
        root = ET.fromstring(r.content)
        child_tags = set(el.tag for el in root.iter())
        log.info(f"[PLEX] XML tags found: {child_tags}")
        items = []
        for el in root.iter(media_tag):
            key   = el.attrib.get('ratingKey', '')
            title = el.attrib.get('title', '')
            year  = el.attrib.get('year', '')
            thumb = el.attrib.get('thumb', '')
            if title:
                thumb_url = (
                    f'http://{_ip}:{_port}{thumb}'
                    if thumb else ''
                )
                items.append({'key': key, 'title': title, 'year': year, 'thumb': thumb_url})
        log.info(f"[PLEX] Found {len(items)} items")
        return items
    except requests.exceptions.ConnectionError as e:
        log.error(f"[PLEX] Connection error: {e}")
        return []
    except requests.exceptions.Timeout:
        log.error(f"[PLEX] Timeout after 20s")
        return []
    except requests.exceptions.HTTPError as e:
        log.error(f"[PLEX] HTTP {r.status_code}: {e}")
        if r.status_code == 401:
            log.error("[PLEX] 401 — check PLEX_TOKEN")
        return []
    except ET.ParseError as e:
        log.error(f"[PLEX] XML parse error: {e}")
        return []
    except Exception as e:
        log.error(f"[PLEX] Unexpected: {e}", exc_info=True)
        return []

# --- TMDB (thread-safe, no sleeps — rate limiting handled by pool size) ---
_tmdb_local = threading.local()

def _get_tmdb_session():
    session = getattr(_tmdb_local, 'session', None)
    if session is None:
        session = requests.Session()
        _tmdb_local.session = session
    return session

def tmdb_search(title, media_type):
    endpoint = 'movie' if media_type == 'movie' else 'tv'
    try:
        _tmdb_key = get_setting('tmdb_api_key', TMDB_API_KEY)
        r = _get_tmdb_session().get(
            f'https://api.themoviedb.org/3/search/{endpoint}',
            params={'api_key': _tmdb_key, 'query': title},
            timeout=10
        )
        r.raise_for_status()
        results = r.json().get('results', [])
        if results:
            return str(results[0]['id'])
        return None
    except Exception as e:
        log.warning(f"[TMDB] Search failed for '{title}': {type(e).__name__}")
        return None

def tmdb_providers(tmdb_id, media_type):
    endpoint = 'movie' if media_type == 'movie' else 'tv'
    try:
        _tmdb_key = get_setting('tmdb_api_key', TMDB_API_KEY)
        r = _get_tmdb_session().get(
            f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/watch/providers',
            params={'api_key': _tmdb_key},
            timeout=10
        )
        r.raise_for_status()
        flatrate = r.json().get('results', {}).get('US', {}).get('flatrate', [])
        raw = [p['provider_name'] for p in flatrate]
        normalized = [PROVIDER_ALIASES.get(n, n) for n in raw]
        matched = [p for p in normalized if p in SERVICES]
        return matched
    except Exception as e:
        log.warning(f"[TMDB] Providers failed for ID {tmdb_id}: {type(e).__name__}")
        return []

# --- Sync worker (runs one title, returns result dict) ---
def process_title(item, mtype, existing_tmdb_id):
    """
    Resolve TMDB ID and fetch providers for a single title.
    Returns dict with title key + provider list (or error).
    Called from thread pool — no DB access here.
    """
    tmdb_id = existing_tmdb_id
    if not tmdb_id:
        tmdb_id = tmdb_search(item['title'], mtype)
        if not tmdb_id:
            return {'key': item['key'], 'tmdb_id': None, 'providers': []}

    providers = tmdb_providers(tmdb_id, mtype)
    return {'key': item['key'], 'tmdb_id': tmdb_id, 'providers': providers}

# --- Sync orchestrator ---
_sync_lock = threading.Lock()

def claim_sync():
    """Claim the shared SQLite sync flag atomically across Gunicorn workers."""
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        cursor = db.execute(
            'UPDATE sync_status SET sync_message=?, is_syncing=1 WHERE id=1 AND is_syncing=0',
            ('Fetching Plex libraries...',)
        )
        db.commit()
        return cursor.rowcount == 1
    finally:
        db.close()

def set_sync_status(msg, syncing=True, synced=None, total=None):
    db = get_db()
    if synced is not None and total is not None:
        db.execute(
            'UPDATE sync_status SET sync_message=?, is_syncing=?, synced_count=?, total_count=? WHERE id=1',
            (msg, 1 if syncing else 0, synced, total)
        )
    else:
        db.execute('UPDATE sync_status SET sync_message=?, is_syncing=? WHERE id=1',
                   (msg, 1 if syncing else 0))
    db.commit()
    db.close()

def run_sync():
    with _sync_lock:
        if not claim_sync():
            log.warning("[SYNC] Already in progress, skipping")
            return

        log.info("[SYNC] Starting...")

        movies = fetch_plex_library(get_setting('movie_library_id', MOVIE_LIB_ID), 'Video')
        tv     = fetch_plex_library(get_setting('tv_library_id', TV_LIB_ID), 'Directory')

        if not movies and not tv:
            log.error("[SYNC] Nothing from Plex — check connection/token/library IDs")
            set_sync_status('ERROR: No titles from Plex. Check logs.', syncing=False)
            return

        all_items = [(m, 'movie') for m in movies] + [(t, 'tv') for t in tv]
        total = len(all_items)
        sync_workers = get_sync_workers()
        log.info(f"[SYNC] {total} titles to process with {sync_workers} workers")

        # Upsert all titles into DB first (fast, serial)
        db = get_db()
        for item, mtype in all_items:
            db.execute('''
                INSERT INTO titles (plex_rating_key, title, year, media_type, thumb_url, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plex_rating_key) DO UPDATE SET
                    title=excluded.title, year=excluded.year,
                    thumb_url=excluded.thumb_url, last_updated=excluded.last_updated
            ''', (item['key'], item['title'], item['year'], mtype, item['thumb'], time.time()))
        db.commit()

        # Fetch existing TMDB IDs to avoid re-querying
        keys = [item['key'] for item, _ in all_items]
        placeholders = ','.join('?' * len(keys))
        rows = db.execute(
            f'SELECT plex_rating_key, id, tmdb_id FROM titles WHERE plex_rating_key IN ({placeholders})',
            keys
        ).fetchall()
        db.close()

        key_to_row = {r['plex_rating_key']: {'id': r['id'], 'tmdb_id': r['tmdb_id']} for r in rows}

        # Parallel TMDB resolution
        set_sync_status(f'Resolving {total} titles via TMDB...', synced=0, total=total)
        results = {}
        completed = 0

        with ThreadPoolExecutor(max_workers=sync_workers) as pool:
            future_to_key = {
                pool.submit(
                    process_title,
                    item,
                    mtype,
                    key_to_row.get(item['key'], {}).get('tmdb_id')
                ): item['key']
                for item, mtype in all_items
            }
            for future in as_completed(future_to_key):
                plex_key = future_to_key[future]
                try:
                    results[plex_key] = future.result()
                except Exception as e:
                    log.error(f"[SYNC] Worker error for key {plex_key}: {e}")
                    results[plex_key] = {'key': plex_key, 'tmdb_id': None, 'providers': []}
                completed += 1
                if completed % 20 == 0 or completed == total:
                    pct = int(completed / total * 100)
                    set_sync_status(
                        f'Processed {completed}/{total} titles ({pct}%)',
                        synced=completed, total=total
                    )
                    log.info(f"[SYNC] {completed}/{total}")

        # Write results back to DB (serial — SQLite doesn't like concurrent writes)
        set_sync_status('Writing results to database...', synced=completed, total=total)
        db = get_db()
        for plex_key, result in results.items():
            row_info = key_to_row.get(plex_key)
            if not row_info:
                continue
            title_db_id = row_info['id']

            if result.get('tmdb_id') and result['tmdb_id'] != row_info.get('tmdb_id'):
                db.execute('UPDATE titles SET tmdb_id=? WHERE id=?',
                           (result['tmdb_id'], title_db_id))

            db.execute('DELETE FROM provider_links WHERE title_id=?', (title_db_id,))
            for pname in result.get('providers', []):
                db.execute(
                    'INSERT OR IGNORE INTO provider_links (title_id, provider_name) VALUES (?, ?)',
                    (title_db_id, pname)
                )
        db.commit()

        finish_msg = f'Last sync: {datetime.now().strftime("%b %d %Y %H:%M")} ({total} titles)'
        db.execute(
            'UPDATE sync_status SET last_sync=?, is_syncing=0, sync_message=?, synced_count=?, total_count=? WHERE id=1',
            (time.time(), finish_msg, total, total)
        )
        db.commit()
        db.close()
        log.info(f"[SYNC] Complete. {total} titles processed.")

# --- API Routes ---
@app.route('/')
def index():
    return render_template('index.html', services=SERVICES)

@app.route('/api/titles')
def api_titles():
    media_type = request.args.get('type', 'all')
    # Accept comma-separated list of services
    services_param = request.args.get('services', '')
    selected_services = [s for s in services_param.split(',') if s] if services_param else []

    db = get_db()
    query = '''
        SELECT t.id, t.title, t.year, t.media_type, t.thumb_url,
               GROUP_CONCAT(pl.provider_name, ';;') as providers
        FROM titles t
        LEFT JOIN provider_links pl ON pl.title_id = t.id
    '''
    conditions, params = [], []

    if media_type in ('movie', 'tv'):
        conditions.append('t.media_type = ?')
        params.append(media_type)

    if selected_services:
        placeholders = ','.join('?' * len(selected_services))
        conditions.append(f'''EXISTS (
            SELECT 1 FROM provider_links pl2
            WHERE pl2.title_id=t.id AND pl2.provider_name IN ({placeholders})
        )''')
        params.extend(selected_services)

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' GROUP BY t.id ORDER BY t.title'

    rows = db.execute(query, params).fetchall()
    db.close()

    titles = []
    for row in rows:
        provider_list = []
        if row['providers']:
            provider_list = [p for p in row['providers'].split(';;') if p]
        titles.append({
            'id':        row['id'],
            'title':     row['title'],
            'year':      row['year'],
            'type':      row['media_type'],
            'thumb':     f"/api/thumb/{row['id']}" if row['thumb_url'] else '',
            'providers': provider_list,
        })
    return jsonify(titles)

@app.route('/api/service_counts')
def api_service_counts():
    """Return how many titles overlap with each service, plus overlap stats."""
    db = get_db()
    rows = db.execute('''
        SELECT provider_name, COUNT(DISTINCT title_id) as cnt
        FROM provider_links
        GROUP BY provider_name
    ''').fetchall()
    counts = {r['provider_name']: r['cnt'] for r in rows}

    # Titles available on at least one service
    overlap = db.execute('''
        SELECT COUNT(DISTINCT title_id) as c FROM provider_links
    ''').fetchone()['c']

    total = db.execute('SELECT COUNT(*) as c FROM titles').fetchone()['c']
    db.close()
    return jsonify({
        'service_counts': counts,
        'overlap': overlap,
        'total': total,
        'unavailable': total - overlap,
    })

@app.route('/api/sync', methods=['POST'])
def api_sync():
    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    return jsonify({'status': 'started'})

@app.route('/api/status')
def api_status():
    db = get_db()
    row    = db.execute('SELECT * FROM sync_status WHERE id=1').fetchone()
    counts = db.execute('''
        SELECT COUNT(*) as total,
               SUM(CASE WHEN media_type="movie" THEN 1 ELSE 0 END) as movies,
               SUM(CASE WHEN media_type="tv"    THEN 1 ELSE 0 END) as tv
        FROM titles
    ''').fetchone()
    db.close()
    return jsonify({
        'last_sync':    row['last_sync'],
        'is_syncing':   bool(row['is_syncing']),
        'sync_message': row['sync_message'],
        'synced_count': row['synced_count'],
        'total_count':  row['total_count'],
        'total':        counts['total'],
        'movies':       counts['movies'],
        'tv':           counts['tv'],
    })

@app.route('/api/thumb/<int:title_id>')
def api_thumb(title_id):
    db = get_db()
    row = db.execute('SELECT thumb_url FROM titles WHERE id=?', (title_id,)).fetchone()
    db.close()
    if not row or not row['thumb_url']:
        return '', 404
    try:
        url = _without_plex_token(row['thumb_url'])
        token = get_setting('plex_token', PLEX_TOKEN)
        with requests.get(url, headers=_plex_headers(token), timeout=8, stream=True) as r:
            r.raise_for_status()
            content_type = r.headers.get('Content-Type', 'image/jpeg').split(';', 1)[0].strip().lower()
            if not content_type.startswith('image/'):
                log.warning(f"[THUMB] Rejected non-image content for title {title_id}")
                return '', 502
            content = bytearray()
            for chunk in r.iter_content(chunk_size=64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_THUMB_BYTES:
                    log.warning(f"[THUMB] Rejected oversized image for title {title_id}")
                    return '', 502
        return Response(
            bytes(content),
            content_type=content_type,
            headers={'Cache-Control': 'public, max-age=86400'}
        )
    except Exception as e:
        log.warning(f"[THUMB] Failed to proxy thumb for title {title_id}: {e}")
        return '', 502

@app.route('/api/setup/status')
def api_setup_status():
    complete = is_setup_complete()
    return jsonify({'complete': complete})

def _json_object():
    """Return a JSON object body, or None for malformed and non-object payloads."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None

@app.route('/api/setup/save', methods=['POST'])
def api_setup_save():
    """Save initial configuration from the setup wizard."""
    data = _json_object()
    if data is None:
        return jsonify({'ok': False, 'error': 'Expected a JSON object'}), 400
    required = ['plex_ip', 'plex_token', 'tmdb_api_key']
    for key in required:
        if not str(data.get(key, '')).strip():
            return jsonify({'ok': False, 'error': f'Missing required field: {key}'}), 400

    allowed = {
        'plex_ip', 'plex_port', 'plex_token',
        'movie_library_id', 'tv_library_id',
        'tmdb_api_key', 'sync_workers',
        'radarr_url', 'radarr_api_key',
        'sonarr_url', 'sonarr_api_key',
    }
    db = get_db()
    for key, value in data.items():
        if key in allowed and value is not None:
            db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                       (key, str(value).strip()))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('setup_complete', '1')")
    db.commit()
    db.close()
    log.info("[SETUP] First-run configuration saved")
    return jsonify({'ok': True})

@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    s = get_all_settings()
    return jsonify({
        'plex_ip':          s.get('plex_ip', ''),
        'plex_port':        s.get('plex_port', '32400'),
        'plex_token':       '',
        'plex_token_set':   bool(s.get('plex_token', '')),
        'movie_library_id': s.get('movie_library_id', '1'),
        'tv_library_id':    s.get('tv_library_id', '2'),
        'tmdb_api_key':     '',
        'tmdb_api_key_set': bool(s.get('tmdb_api_key', '')),
        'sync_workers':     s.get('sync_workers', '8'),
        'radarr_url':       s.get('radarr_url', ''),
        'radarr_api_key':   '',
        'radarr_api_key_set': bool(s.get('radarr_api_key', '')),
        'sonarr_url':       s.get('sonarr_url', ''),
        'sonarr_api_key':   '',
        'sonarr_api_key_set': bool(s.get('sonarr_api_key', '')),
    })

@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    data = _json_object()
    if data is None:
        return jsonify({'ok': False, 'error': 'Expected a JSON object'}), 400
    # Allowed keys
    allowed = {
        'plex_ip', 'plex_port', 'plex_token', 'movie_library_id',
        'tv_library_id', 'tmdb_api_key', 'sync_workers',
        'radarr_url', 'radarr_api_key', 'sonarr_url', 'sonarr_api_key',
    }
    db = get_db()
    updated = []
    for key, value in data.items():
        if key not in allowed:
            continue
        # Skip masked placeholder values (user didn't edit the field)
        if '••••••••' in str(value):
            continue
        db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                   (key, str(value).strip()))
        updated.append(key)
    db.commit()
    db.close()
    log.info(f"[SETTINGS] Updated: {updated}")
    return jsonify({'ok': True, 'updated': updated})

def _normalize_url(url):
    """Ensure URL has a scheme and replace localhost/0.0.0.0 with docker host."""
    url = str(url or '').strip().rstrip('/')
    if not url:
        return url
    # Add scheme if missing
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url
    # Inside Docker, localhost/0.0.0.0/127.0.0.1 all mean the container itself.
    # Replace with host.docker.internal so it routes to the Mac Mini.
    url = re.sub(r'(https?://)(localhost|0\.0\.0\.0|127\.0\.0\.1)', r'\1host.docker.internal', url)
    return url

def _test_arr(url, key, app_name):
    """Test a Radarr or Sonarr connection. Tries v3 then v1 API."""
    url = _normalize_url(url)
    if not url:
        return {'ok': False, 'msg': f'No URL provided for {app_name}'}
    if not key:
        return {'ok': False, 'msg': f'No API key provided for {app_name}'}

    last_err = None
    for api_path in ['/api/v3/system/status', '/api/v1/system/status']:
        for auth_style in ['header', 'param']:
            try:
                kwargs = dict(timeout=8)
                if auth_style == 'header':
                    kwargs['headers'] = {'X-Api-Key': key}
                    test_url = url + api_path
                else:
                    test_url = url + api_path + f'?apikey={key}'
                log.info(f"[TEST] {app_name} {auth_style} → {url}{api_path}")
                r = requests.get(test_url, **kwargs)
                if r.status_code == 401:
                    return {'ok': False, 'msg': f'HTTP 401 — API key rejected by {app_name}'}
                if r.status_code == 404:
                    last_err = f'HTTP 404 on {api_path} — trying alternate API path'
                    continue
                r.raise_for_status()
                d = r.json()
                version = d.get('version', '?')
                return {'ok': True, 'msg': f'{app_name} v{version} — connected at {url}'}
            except requests.exceptions.ConnectionError:
                last_err = f'Connection refused at {url} — is {app_name} running?'
                log.warning(f"[TEST] {app_name} connection failed at {url}")
                break  # No point trying auth styles if host is unreachable
            except requests.exceptions.Timeout:
                last_err = f'Timed out connecting to {url}'
                break
            except requests.exceptions.HTTPError as e:
                last_err = f'HTTP {e.response.status_code} from {app_name}'
            except Exception as e:
                last_err = f'{type(e).__name__} while testing {app_name}'

    return {'ok': False, 'msg': last_err or f'Could not connect to {app_name}'}

@app.route('/api/settings/test', methods=['POST'])
def api_settings_test():
    data = _json_object()
    if data is None:
        return jsonify({'ok': False, 'msg': 'Expected a JSON object'}), 400
    target = data.get('target')
    try:
        if target == 'plex':
            ip    = _normalize_url(data.get('plex_ip', get_setting('plex_ip'))).replace('http://','').replace('https://','')
            port  = data.get('plex_port', get_setting('plex_port', '32400')) or '32400'
            token = str(data.get('plex_token', '') or '')
            if '••••' in token or not token: token = get_setting('plex_token')
            url = f'http://{ip}:{port}/identity'
            log.info(f"[TEST] Plex → {url}")
            r = requests.get(url, headers=_plex_headers(token), timeout=8)
            if r.status_code == 401:
                return jsonify({'ok': False, 'msg': 'HTTP 401 — invalid Plex token'})
            r.raise_for_status()
            try:
                root = ET.fromstring(r.content)
                name = root.attrib.get('friendlyName', 'Plex Server')
            except Exception:
                name = 'Plex Server'
            return jsonify({'ok': True, 'msg': f'Connected — {name}'})

        elif target == 'tmdb':
            key = str(data.get('tmdb_api_key', '') or '')
            if '••••' in key or not key: key = get_setting('tmdb_api_key')
            if not key:
                return jsonify({'ok': False, 'msg': 'No TMDB API key configured'})
            r = requests.get('https://api.themoviedb.org/3/configuration',
                             params={'api_key': key}, timeout=8)
            if r.status_code == 401:
                return jsonify({'ok': False, 'msg': 'HTTP 401 — TMDB key invalid'})
            r.raise_for_status()
            return jsonify({'ok': True, 'msg': 'TMDB API key valid'})

        elif target == 'radarr':
            url = data.get('radarr_url', '') or get_setting('radarr_url')
            key = str(data.get('radarr_api_key', '') or '')
            if '••••' in key or not key: key = get_setting('radarr_api_key')
            return jsonify(_test_arr(url, key, 'Radarr'))

        elif target == 'sonarr':
            url = data.get('sonarr_url', '') or get_setting('sonarr_url')
            key = str(data.get('sonarr_api_key', '') or '')
            if '••••' in key or not key: key = get_setting('sonarr_api_key')
            return jsonify(_test_arr(url, key, 'Sonarr'))

        else:
            return jsonify({'ok': False, 'msg': 'Unknown test target'})

    except requests.exceptions.ConnectionError:
        return jsonify({'ok': False, 'msg': 'Connection refused'})
    except requests.exceptions.Timeout:
        return jsonify({'ok': False, 'msg': 'Connection timed out after 8s'})
    except requests.exceptions.HTTPError as e:
        return jsonify({'ok': False, 'msg': f'HTTP {e.response.status_code} — check credentials'})
    except Exception as e:
        log.error(f"[TEST] Unexpected: {type(e).__name__}")
        return jsonify({'ok': False, 'msg': 'Unexpected connection test error'})

@app.route('/api/settings/discover', methods=['POST'])
def api_settings_discover():
    """
    Probe common LAN addresses and ports to auto-detect Radarr/Sonarr.
    Scans host.docker.internal (the Mac Mini host) on the standard ports.
    """
    data = _json_object()
    if data is None:
        return jsonify({'ok': False, 'error': 'Expected a JSON object'}), 400
    targets = data.get('targets', ['radarr', 'sonarr'])

    # Ports to probe per service
    ARR_PORTS = {
        'radarr': [7878, 7879],
        'sonarr': [8989, 8990],
    }

    # Hosts to try: docker host gateway, plus any custom hint from the UI
    base_hosts = ['host.docker.internal']
    custom_hint = str(data.get('hint') or '').strip()
    if custom_hint:
        h = custom_hint.replace('http://','').replace('https://','').split('/')[0].split(':')[0]
        if h and h not in base_hosts:
            base_hosts.insert(0, h)

    found = {}

    def probe(app_name, host, port):
        url = f'http://{host}:{port}'
        # Try without a key first — if we get a 401 the service is there
        try:
            r = requests.get(f'{url}/api/v3/system/status', timeout=3)
            if r.status_code in (200, 401, 403):
                log.info(f"[DISCOVER] {app_name} found at {url} (HTTP {r.status_code})")
                return url
        except Exception:
            pass
        # Also try v1
        try:
            r = requests.get(f'{url}/api/v1/system/status', timeout=3)
            if r.status_code in (200, 401, 403):
                log.info(f"[DISCOVER] {app_name} found at {url} via v1 (HTTP {r.status_code})")
                return url
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {}
        for app_name in targets:
            if app_name not in ARR_PORTS:
                continue
            for host in base_hosts:
                for port in ARR_PORTS[app_name]:
                    f = pool.submit(probe, app_name, host, port)
                    futures[f] = (app_name, host, port)

        for future, (app_name, host, port) in futures.items():
            result = future.result()
            if result and app_name not in found:
                found[app_name] = result

    log.info(f"[DISCOVER] Results: {found}")
    return jsonify({'found': found})

@app.route('/api/debug/plex')
def api_debug_plex():
    results = {}
    _ip    = get_setting('plex_ip',    PLEX_IP)
    _port  = get_setting('plex_port',  PLEX_PORT)
    _token = get_setting('plex_token', PLEX_TOKEN)
    _ml    = get_setting('movie_library_id', MOVIE_LIB_ID)
    _tl    = get_setting('tv_library_id',    TV_LIB_ID)
    for lib_id, tag, label in [
        (_ml, 'Video', 'movies'),
        (_tl, 'Directory', 'tv'),
    ]:
        url = f'http://{_ip}:{_port}/library/sections/{lib_id}/all'
        try:
            r = requests.get(url, headers=_plex_headers(_token), timeout=10)
            root = ET.fromstring(r.content)
            all_tags = list(set(el.tag for el in root.iter()))
            items = [el.attrib.get('title', '') for el in root.iter(tag)][:5]
            results[label] = {
                'status': r.status_code,
                'xml_tags_found': all_tags,
                'items_with_tag': len([el for el in root.iter(tag)]),
                'sample_titles': items,
            }
        except Exception as e:
            results[label] = {'error': type(e).__name__}
    try:
        r = requests.get(
            'https://api.themoviedb.org/3/search/movie',
            params={'api_key': get_setting('tmdb_api_key', TMDB_API_KEY), 'query': 'The Godfather'},
            timeout=10
        )
        results['tmdb'] = {'status': r.status_code, 'ok': r.status_code == 200}
    except Exception as e:
        results['tmdb'] = {'error': type(e).__name__}
    return jsonify(results)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)

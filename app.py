import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from defusedxml import ElementTree as ET
from flask import Flask, jsonify, render_template, request, Response

# --- Logging ---
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
}

def _normalize_log_level(value, fallback='INFO'):
    level = str(value or '').strip().upper()
    return level if level in LOG_LEVELS else fallback

DEFAULT_LOG_LEVEL = _normalize_log_level(os.getenv('LOG_LEVEL', 'INFO'))

logging.basicConfig(
    level=LOG_LEVELS[DEFAULT_LOG_LEVEL],
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('plexarr')
log.setLevel(LOG_LEVELS[DEFAULT_LOG_LEVEL])

def apply_log_level(level):
    """Apply the selected log level to stdout logging used by Docker logs."""
    normalized = _normalize_log_level(level, DEFAULT_LOG_LEVEL)
    numeric_level = LOG_LEVELS[normalized]
    logging.getLogger().setLevel(numeric_level)
    log.setLevel(numeric_level)
    logging.getLogger('werkzeug').setLevel(numeric_level)
    return normalized

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    return response

def _same_origin(origin, host_url):
    try:
        origin_parts = urlsplit(origin)
        host_parts = urlsplit(host_url)
    except ValueError:
        return False
    return (
        origin_parts.scheme == host_parts.scheme
        and origin_parts.hostname == host_parts.hostname
        and (origin_parts.port or _default_port(origin_parts.scheme))
            == (host_parts.port or _default_port(host_parts.scheme))
    )

def _default_port(scheme):
    return 443 if scheme == 'https' else 80

@app.before_request
def reject_cross_site_writes():
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return None

    fetch_site = request.headers.get('Sec-Fetch-Site', '').lower()
    if fetch_site == 'cross-site':
        return jsonify({'ok': False, 'error': 'Cross-site write rejected'}), 403

    origin = request.headers.get('Origin')
    if origin and not _same_origin(origin, request.host_url):
        return jsonify({'ok': False, 'error': 'Cross-origin write rejected'}), 403

    return None

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
log.info(f"  LOG_LEVEL:        {DEFAULT_LOG_LEVEL}")

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
            plex_signature TEXT DEFAULT '',
            last_updated REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS provider_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            FOREIGN KEY (title_id) REFERENCES titles(id),
            UNIQUE(title_id, provider_name)
        );
        CREATE TABLE IF NOT EXISTS partial_provider_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            seasons TEXT NOT NULL,
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

    title_cols = {row[1] for row in db.execute("PRAGMA table_info(titles)")}
    if 'plex_signature' not in title_cols:
        log.info("Migrating DB: adding titles.plex_signature")
        db.execute("ALTER TABLE titles ADD COLUMN plex_signature TEXT DEFAULT ''")

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
        'cleanuparr_url':    '',
        'cleanuparr_api_key': '',
        'torrent_client_type': '',
        'torrent_client_url': '',
        'torrent_client_username': '',
        'torrent_client_password': '',
        'log_level':         DEFAULT_LOG_LEVEL,
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
    apply_log_level(get_setting('log_level', DEFAULT_LOG_LEVEL))
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

def _thumb_proxy_url(title_id, thumb_url):
    """Version proxied artwork by its Plex source so browser caches cannot mix titles."""
    source = _without_plex_token(thumb_url)
    version = hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]
    return f'/api/thumb/{title_id}?v={version}'

def _plex_item_signature(item, media_type):
    """Fingerprint Plex metadata that affects matching/provider coverage."""
    seasons = ','.join(str(season) for season in sorted(item.get('plex_seasons') or []))
    raw = '\x1f'.join([
        str(media_type or ''),
        str(item.get('title') or ''),
        str(item.get('year') or ''),
        _without_plex_token(str(item.get('thumb') or '')),
        seasons,
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

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

def fetch_plex_tv_seasons(library_id):
    """Return the regular-season numbers represented by episodes in the Plex TV library."""
    _ip    = get_setting('plex_ip',    PLEX_IP)
    _port  = get_setting('plex_port',  PLEX_PORT)
    _token = get_setting('plex_token', PLEX_TOKEN)
    url = f'http://{_ip}:{_port}/library/sections/{library_id}/all'
    log.info(f"[PLEX] Fetching TV episode inventory for library {library_id}")
    try:
        r = requests.get(url, headers=_plex_headers(_token), params={'type': 4}, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        seasons_by_show = {}
        episode_count = 0
        for el in root.iter('Video'):
            if el.attrib.get('type') not in ('', 'episode'):
                continue
            show_key = el.attrib.get('grandparentRatingKey', '')
            try:
                season_number = int(el.attrib.get('parentIndex', ''))
            except ValueError:
                continue
            if show_key and season_number > 0:
                seasons_by_show.setdefault(show_key, set()).add(season_number)
                episode_count += 1
        log.info(f"[PLEX] Found {episode_count} regular TV episodes across {len(seasons_by_show)} shows")
        return seasons_by_show
    except Exception as e:
        log.warning(f"[PLEX] TV episode inventory unavailable: {type(e).__name__}")
        return None

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

def _matched_tmdb_providers(payload):
    flatrate = payload.get('results', {}).get('US', {}).get('flatrate', [])
    normalized = [PROVIDER_ALIASES.get(p['provider_name'], p['provider_name']) for p in flatrate]
    return [provider for provider in normalized if provider in SERVICES]

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
        return _matched_tmdb_providers(r.json())
    except Exception as e:
        log.warning(f"[TMDB] Providers failed for ID {tmdb_id}: {type(e).__name__}")
        return []

def tmdb_tv_season_providers(tmdb_id, season_number):
    """Return US subscription providers for one TV season."""
    try:
        _tmdb_key = get_setting('tmdb_api_key', TMDB_API_KEY)
        r = _get_tmdb_session().get(
            f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}/watch/providers',
            params={'api_key': _tmdb_key},
            timeout=10
        )
        r.raise_for_status()
        return _matched_tmdb_providers(r.json())
    except Exception as e:
        log.warning(f"[TMDB] Providers failed for TV ID {tmdb_id} season {season_number}: {type(e).__name__}")
        return None

def tmdb_tv_coverage(tmdb_id, plex_seasons):
    """Return full providers and partial provider seasons for the TV seasons held in Plex."""
    seasons = sorted(plex_seasons)
    if not seasons:
        return tmdb_providers(tmdb_id, 'tv'), {}

    seasons_by_provider = {}
    for season_number in seasons:
        season_providers = tmdb_tv_season_providers(tmdb_id, season_number)
        if season_providers is None:
            log.warning(f"[TMDB] Falling back to title-level providers for TV ID {tmdb_id}")
            return tmdb_providers(tmdb_id, 'tv'), {}
        for provider in season_providers:
            seasons_by_provider.setdefault(provider, []).append(season_number)

    full, partial = [], {}
    for provider in SERVICES:
        provider_seasons = seasons_by_provider.get(provider, [])
        if len(provider_seasons) == len(seasons):
            full.append(provider)
        elif provider_seasons:
            partial[provider] = provider_seasons
    return full, partial

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
            return {'key': item['key'], 'tmdb_id': None, 'providers': [], 'partial_providers': {}}

    partial_providers = {}
    if mtype == 'tv' and item.get('plex_seasons'):
        providers, partial_providers = tmdb_tv_coverage(tmdb_id, item['plex_seasons'])
    else:
        providers = tmdb_providers(tmdb_id, mtype)
    return {
        'key': item['key'],
        'tmdb_id': tmdb_id,
        'providers': providers,
        'partial_providers': partial_providers,
    }

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
        tv_library_id = get_setting('tv_library_id', TV_LIB_ID)
        tv = fetch_plex_library(tv_library_id, 'Directory')
        tv_seasons = fetch_plex_tv_seasons(tv_library_id) if tv else None
        if tv_seasons is not None:
            for item in tv:
                item['plex_seasons'] = tv_seasons.get(item['key'], set())

        if not movies and not tv:
            log.error("[SYNC] Nothing from Plex — check connection/token/library IDs")
            set_sync_status('ERROR: No titles from Plex. Check logs.', syncing=False)
            return

        all_items = [(m, 'movie') for m in movies] + [(t, 'tv') for t in tv]
        total = len(all_items)
        sync_workers = get_sync_workers()
        log.info(f"[SYNC] {total} Plex titles found")

        db = get_db()
        existing_rows = db.execute(
            'SELECT id, plex_rating_key, tmdb_id, plex_signature FROM titles'
        ).fetchall()
        existing_by_key = {
            r['plex_rating_key']: {
                'id': r['id'],
                'tmdb_id': r['tmdb_id'],
                'plex_signature': r['plex_signature'] or '',
            }
            for r in existing_rows
        }

        seen_keys = {item['key'] for item, _ in all_items}
        stale_ids = [r['id'] for r in existing_rows if r['plex_rating_key'] not in seen_keys]
        if stale_ids:
            placeholders = ','.join('?' * len(stale_ids))
            db.execute(f'DELETE FROM provider_links WHERE title_id IN ({placeholders})', stale_ids)
            db.execute(f'DELETE FROM partial_provider_links WHERE title_id IN ({placeholders})', stale_ids)
            db.execute(f'DELETE FROM titles WHERE id IN ({placeholders})', stale_ids)
            log.info(f"[SYNC] Removed {len(stale_ids)} stale title{'s' if len(stale_ids) != 1 else ''} no longer present in Plex")

        changed_items = []
        cached_count = 0
        now = time.time()
        for item, mtype in all_items:
            item['plex_signature'] = _plex_item_signature(item, mtype)
            existing = existing_by_key.get(item['key'])
            changed = not existing or existing.get('plex_signature') != item['plex_signature'] or not existing.get('tmdb_id')
            if changed:
                changed_items.append((item, mtype))
            else:
                cached_count += 1
            db.execute('''
                INSERT INTO titles (plex_rating_key, title, year, media_type, thumb_url, tmdb_id, plex_signature, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plex_rating_key) DO UPDATE SET
                    title=excluded.title, year=excluded.year,
                    media_type=excluded.media_type, thumb_url=excluded.thumb_url,
                    plex_signature=excluded.plex_signature, last_updated=excluded.last_updated
            ''', (
                item['key'],
                item['title'],
                item['year'],
                mtype,
                item['thumb'],
                existing.get('tmdb_id') if existing else None,
                item['plex_signature'],
                now,
            ))
        db.commit()

        keys = [item['key'] for item, _ in all_items]
        placeholders = ','.join('?' * len(keys))
        rows = db.execute(
            f'SELECT plex_rating_key, id, tmdb_id FROM titles WHERE plex_rating_key IN ({placeholders})',
            keys
        ).fetchall() if keys else []
        db.close()

        key_to_row = {r['plex_rating_key']: {'id': r['id'], 'tmdb_id': r['tmdb_id']} for r in rows}
        log.info(
            f"[SYNC] Cache: {cached_count} unchanged, {len(changed_items)} changed/new, "
            f"{len(stale_ids)} removed; TMDB workers={sync_workers}"
        )

        set_sync_status(
            f'{cached_count} cached; resolving {len(changed_items)} changed title{"s" if len(changed_items) != 1 else ""} via TMDB...',
            synced=cached_count,
            total=total,
        )
        results = {}
        completed = cached_count

        with ThreadPoolExecutor(max_workers=sync_workers) as pool:
            future_to_key = {
                pool.submit(
                    process_title,
                    item,
                    mtype,
                    key_to_row.get(item['key'], {}).get('tmdb_id')
                ): item['key']
                for item, mtype in changed_items
            }
            for future in as_completed(future_to_key):
                plex_key = future_to_key[future]
                try:
                    results[plex_key] = future.result()
                except Exception as e:
                    log.error(f"[SYNC] Worker error for key {plex_key}: {e}")
                    results[plex_key] = {
                        'key': plex_key,
                        'tmdb_id': None,
                        'providers': [],
                        'partial_providers': {},
                    }
                completed += 1
                if completed % 20 == 0 or completed == total:
                    pct = int(completed / total * 100)
                    set_sync_status(
                        f'Processed {completed}/{total} titles ({pct}%)',
                        synced=completed, total=total
                    )
                    log.info(f"[SYNC] {completed}/{total}")

        if not changed_items:
            set_sync_status(f'All {total} titles unchanged; using cached providers.', synced=total, total=total)

        # Write changed TMDB results back to DB (serial — SQLite doesn't like concurrent writes)
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
            db.execute('DELETE FROM partial_provider_links WHERE title_id=?', (title_db_id,))
            for pname, seasons in result.get('partial_providers', {}).items():
                db.execute(
                    'INSERT OR REPLACE INTO partial_provider_links (title_id, provider_name, seasons) VALUES (?, ?, ?)',
                    (title_db_id, pname, ','.join(str(season) for season in seasons))
                )
        db.commit()

        finish_msg = (
            f'Last sync: {datetime.now().strftime("%b %d %Y %H:%M")} '
            f'({total} titles; {cached_count} cached, {len(changed_items)} refreshed, {len(stale_ids)} removed)'
        )
        db.execute(
            'UPDATE sync_status SET last_sync=?, is_syncing=0, sync_message=?, synced_count=?, total_count=? WHERE id=1',
            (time.time(), finish_msg, total, total)
        )
        db.commit()
        db.close()
        log.info(
            f"[SYNC] Complete. {total} titles processed "
            f"({cached_count} cached, {len(changed_items)} refreshed, {len(stale_ids)} removed)."
        )

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
        conditions.append(f'''(
            EXISTS (
                SELECT 1 FROM provider_links pl2
                WHERE pl2.title_id=t.id AND pl2.provider_name IN ({placeholders})
            )
            OR EXISTS (
                SELECT 1 FROM partial_provider_links ppl2
                WHERE ppl2.title_id=t.id AND ppl2.provider_name IN ({placeholders})
            )
        )''')
        params.extend(selected_services)
        params.extend(selected_services)

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' GROUP BY t.id ORDER BY t.title'

    rows = db.execute(query, params).fetchall()
    partial_by_title = {}
    if rows:
        title_ids = [row['id'] for row in rows]
        placeholders = ','.join('?' * len(title_ids))
        partial_rows = db.execute(
            f'''SELECT title_id, provider_name, seasons
                FROM partial_provider_links
                WHERE title_id IN ({placeholders})
                ORDER BY title_id, provider_name''',
            title_ids
        ).fetchall()
        for row in partial_rows:
            seasons = [int(season) for season in row['seasons'].split(',') if season]
            partial_by_title.setdefault(row['title_id'], []).append({
                'name': row['provider_name'],
                'seasons': seasons,
            })
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
            'thumb':     _thumb_proxy_url(row['id'], row['thumb_url']) if row['thumb_url'] else '',
            'providers': provider_list,
            'partial_providers': partial_by_title.get(row['id'], []),
        })
    return jsonify(titles)

@app.route('/api/service_counts')
def api_service_counts():
    """Return how many titles overlap with each service, plus overlap stats."""
    db = get_db()
    rows = db.execute('''
        SELECT provider_name, COUNT(DISTINCT title_id) as cnt
        FROM (
            SELECT title_id, provider_name FROM provider_links
            UNION
            SELECT title_id, provider_name FROM partial_provider_links
        )
        GROUP BY provider_name
    ''').fetchall()
    counts = {r['provider_name']: r['cnt'] for r in rows}

    # Titles available on at least one service
    overlap = db.execute('''
        SELECT COUNT(DISTINCT title_id) as c
        FROM (
            SELECT title_id FROM provider_links
            UNION
            SELECT title_id FROM partial_provider_links
        )
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
        'cleanuparr_url', 'cleanuparr_api_key',
        'torrent_client_type', 'torrent_client_url',
        'torrent_client_username', 'torrent_client_password',
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
        'cleanuparr_url':   s.get('cleanuparr_url', ''),
        'cleanuparr_api_key': '',
        'cleanuparr_api_key_set': bool(s.get('cleanuparr_api_key', '')),
        'torrent_client_type': s.get('torrent_client_type', ''),
        'torrent_client_url': s.get('torrent_client_url', ''),
        'torrent_client_username': s.get('torrent_client_username', ''),
        'torrent_client_password': '',
        'torrent_client_password_set': bool(s.get('torrent_client_password', '')),
        'log_level':        _normalize_log_level(s.get('log_level', DEFAULT_LOG_LEVEL), DEFAULT_LOG_LEVEL),
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
        'cleanuparr_url', 'cleanuparr_api_key',
        'torrent_client_type', 'torrent_client_url',
        'torrent_client_username', 'torrent_client_password',
        'log_level',
    }
    db = get_db()
    updated = []
    for key, value in data.items():
        if key not in allowed:
            continue
        # Skip masked placeholder values (user didn't edit the field)
        if '••••••••' in str(value):
            continue
        if key == 'log_level':
            value = _normalize_log_level(value, '')
            if not value:
                return jsonify({'ok': False, 'error': 'Invalid log level'}), 400
        if key == 'torrent_client_type':
            value = _torrent_client_type(value)
        db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                   (key, str(value).strip()))
        updated.append(key)
    db.commit()
    db.close()
    if 'log_level' in updated:
        apply_log_level(data.get('log_level'))
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

def _removal_title(title_id):
    """Load the internal identifiers needed for a remote removal."""
    db = get_db()
    row = db.execute(
        '''SELECT id, title, year, media_type, plex_rating_key, tmdb_id
           FROM titles WHERE id=?''',
        (title_id,)
    ).fetchone()
    db.close()
    return dict(row) if row else None

def _response_excerpt(response, limit=600):
    text = str(getattr(response, 'text', '') or '').replace('\n', ' ').strip()
    return text[:limit] if text else '<empty>'

def _torrent_client_type(value=None):
    value = str(value if value is not None else get_setting('torrent_client_type')).strip().lower()
    return value if value in ('qbittorrent', 'transmission') else ''

def _torrent_client_configured():
    return bool(_torrent_client_type() and _normalize_url(get_setting('torrent_client_url')))

def _qbittorrent_session(url=None, username=None, password=None):
    url = _normalize_url(url if url is not None else get_setting('torrent_client_url'))
    username = get_setting('torrent_client_username') if username is None else username
    password = get_setting('torrent_client_password') if password is None else password
    session = requests.Session()
    if username or password:
        response = session.post(
            url + '/api/v2/auth/login',
            data={'username': username, 'password': password},
            timeout=8,
        )
        if response.status_code >= 400 or response.text.strip().lower() not in ('ok.', 'ok'):
            log.warning(f"[TORRENT] qBittorrent login failed HTTP {response.status_code}: {_response_excerpt(response)}")
            response.raise_for_status()
            raise ValueError('qBittorrent login failed')
    return url, session

def _qbittorrent_list_torrents(url=None, username=None, password=None):
    url, session = _qbittorrent_session(url, username, password)
    response = session.get(url + '/api/v2/torrents/info', timeout=12)
    if response.status_code >= 400:
        log.warning(f"[TORRENT] qBittorrent list failed HTTP {response.status_code}: {_response_excerpt(response)}")
    response.raise_for_status()
    torrents = response.json()
    if not isinstance(torrents, list):
        raise ValueError('qBittorrent torrent list was not an array')
    return [
        {
            'id': str(item.get('hash') or ''),
            'name': str(item.get('name') or ''),
            'category': str(item.get('category') or ''),
            'tags': str(item.get('tags') or ''),
            'status': str(item.get('state') or ''),
            'save_path': str(item.get('save_path') or ''),
            'content_path': str(item.get('content_path') or ''),
            'client': 'qBittorrent',
        }
        for item in torrents
        if item.get('hash')
    ]

def _qbittorrent_delete_torrents(ids, delete_files=True):
    url, session = _qbittorrent_session()
    response = session.post(
        url + '/api/v2/torrents/delete',
        data={'hashes': '|'.join(ids), 'deleteFiles': 'true' if delete_files else 'false'},
        timeout=12,
    )
    if response.status_code >= 400:
        log.warning(f"[TORRENT] qBittorrent delete failed HTTP {response.status_code}: {_response_excerpt(response)}")
    response.raise_for_status()

def _transmission_rpc_url(url):
    url = _normalize_url(url).rstrip('/')
    return url if url.endswith('/rpc') else url + '/transmission/rpc'

def _transmission_rpc(method, arguments=None, url=None, username=None, password=None):
    rpc_url = _transmission_rpc_url(url if url is not None else get_setting('torrent_client_url'))
    username = get_setting('torrent_client_username') if username is None else username
    password = get_setting('torrent_client_password') if password is None else password
    auth = (username, password) if username or password else None
    headers = {}
    payload = {'method': method, 'arguments': arguments or {}}
    response = requests.post(rpc_url, json=payload, headers=headers, auth=auth, timeout=12)
    if response.status_code == 409:
        session_id = response.headers.get('X-Transmission-Session-Id')
        headers['X-Transmission-Session-Id'] = session_id
        response = requests.post(rpc_url, json=payload, headers=headers, auth=auth, timeout=12)
    if response.status_code >= 400:
        log.warning(f"[TORRENT] Transmission RPC failed HTTP {response.status_code}: {_response_excerpt(response)}")
    response.raise_for_status()
    data = response.json()
    if data.get('result') != 'success':
        raise ValueError(f"Transmission RPC failed: {data.get('result', 'unknown')}")
    return data.get('arguments', {})

def _transmission_list_torrents(url=None, username=None, password=None):
    data = _transmission_rpc(
        'torrent-get',
        {'fields': ['id', 'hashString', 'name', 'downloadDir', 'status', 'labels']},
        url,
        username,
        password,
    )
    torrents = data.get('torrents', [])
    if not isinstance(torrents, list):
        raise ValueError('Transmission torrent list was not an array')
    return [
        {
            'id': item.get('id'),
            'hash': str(item.get('hashString') or ''),
            'name': str(item.get('name') or ''),
            'category': ','.join(item.get('labels') or []),
            'tags': '',
            'status': str(item.get('status') or ''),
            'save_path': str(item.get('downloadDir') or ''),
            'content_path': str(item.get('downloadDir') or ''),
            'client': 'Transmission',
        }
        for item in torrents
        if item.get('id') is not None
    ]

def _transmission_delete_torrents(ids, delete_files=True):
    _transmission_rpc('torrent-remove', {'ids': ids, 'delete-local-data': delete_files})

def _list_torrent_client_entries(client_type=None, url=None, username=None, password=None):
    client_type = _torrent_client_type(client_type)
    if client_type == 'qbittorrent':
        return _qbittorrent_list_torrents(url, username, password)
    if client_type == 'transmission':
        return _transmission_list_torrents(url, username, password)
    raise ValueError('Unsupported torrent client')

def _delete_torrent_client_entries(ids, delete_files=True):
    client_type = _torrent_client_type()
    log.info(f"[TORRENT] Delete start client={client_type} count={len(ids)} delete_files={delete_files}")
    if client_type == 'qbittorrent':
        return _qbittorrent_delete_torrents([str(item) for item in ids], delete_files)
    if client_type == 'transmission':
        return _transmission_delete_torrents(ids, delete_files)
    raise ValueError('Unsupported torrent client')

def _match_text(value):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower())).strip()

def _torrent_match_terms(title, year='', radarr_movie=None):
    title_text = _match_text(title)
    year = str(year or '').strip()
    terms = [title_text]
    if year:
        terms.append(f"{title_text} {year}")
    if radarr_movie:
        for value in (radarr_movie.get('title'), radarr_movie.get('path'), radarr_movie.get('movie_file_path')):
            text = _match_text(value)
            if text:
                terms.append(text)
    return [term for term in dict.fromkeys(terms) if term]

def _torrent_entry_matches(entry, terms, year=''):
    text = _match_text(' '.join(str(entry.get(key) or '') for key in ('name', 'save_path', 'content_path', 'category', 'tags')))
    if not text:
        return False
    year = str(year or '').strip()
    for term in terms:
        if term and term in text and (not year or year in text or len(term.split()) > 3):
            return True
    title_tokens = [token for token in terms[0].split() if len(token) > 2] if terms else []
    if len(title_tokens) >= 2 and all(token in text for token in title_tokens) and (not year or year in text):
        return True
    return False

def _torrent_matches_for_title(title, radarr=None):
    if not _torrent_client_configured():
        return {'configured': False, 'matches': [], 'error': ''}
    try:
        entries = _list_torrent_client_entries()
        year = str(title.get('year') or '')
        terms = _torrent_match_terms(title.get('title'), year, radarr.get('movie') if radarr else None)
        matches = []
        for entry in entries:
            if not _torrent_entry_matches(entry, terms, year):
                continue
            matches.append({
                'id': entry['id'],
                'name': entry['name'],
                'category': entry.get('category', ''),
                'status': entry.get('status', ''),
                'save_path': entry.get('save_path', ''),
                'client': entry.get('client', ''),
            })
        log.info(f"[TORRENT] Matched torrent-client entries count={len(matches)} title={title.get('title')!r}")
        return {'configured': True, 'matches': matches, 'error': ''}
    except Exception as exc:
        log.warning(f"[TORRENT] Torrent-client match failed: {type(exc).__name__}")
        return {'configured': True, 'matches': [], 'error': 'Could not inspect torrent client'}

def _cleanuparr_configured():
    return bool(_normalize_url(get_setting('cleanuparr_url')) and get_setting('cleanuparr_api_key'))

def _cleanuparr_headers():
    key = get_setting('cleanuparr_api_key')
    return {'X-Api-Key': key} if key else {}

def _trigger_cleanuparr_download_cleaner():
    url = _normalize_url(get_setting('cleanuparr_url'))
    if not url or not get_setting('cleanuparr_api_key'):
        raise ValueError('Cleanuparr is not configured')
    log.info("[REMOVE] Cleanuparr DownloadCleaner trigger start")
    response = requests.post(
        url + '/api/jobs/DownloadCleaner/trigger',
        headers=_cleanuparr_headers(),
        timeout=8,
    )
    log.info(f"[REMOVE] Cleanuparr DownloadCleaner trigger HTTP {response.status_code}")
    if response.status_code >= 400:
        log.warning(f"[REMOVE] Cleanuparr trigger failed HTTP {response.status_code}: {_response_excerpt(response)}")
    response.raise_for_status()

def _queue_records_from_response(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        records = payload.get('records', [])
        return records if isinstance(records, list) else []
    return []

def _matching_radarr_downloads(records, movie_id):
    downloads = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            if int(record.get('movieId', -1)) != movie_id:
                continue
            downloads.append({
                'id': int(record['id']),
                'title': str(record.get('title') or 'Active download'),
                'status': str(record.get('status') or ''),
            })
        except (TypeError, ValueError, KeyError):
            continue
    return downloads

def _log_queue_sample(records, source):
    if not log.isEnabledFor(logging.DEBUG):
        return
    sample = [
        {
            'id': record.get('id'),
            'movieId': record.get('movieId'),
            'status': record.get('status'),
            'title': record.get('title'),
        }
        for record in records[:5]
        if isinstance(record, dict)
    ]
    log.debug(f"[REMOVE] Radarr queue sample source={source} records={sample}")

def _radarr_context(tmdb_id):
    """Resolve a movie and its active queue records without exposing Radarr IDs to writes."""
    url = _normalize_url(get_setting('radarr_url'))
    key = get_setting('radarr_api_key')
    result = {
        'configured': bool(url and key),
        'movie': None,
        'downloads': [],
        'error': '',
        'queue_error': '',
    }
    if not result['configured']:
        result['error'] = 'Radarr is not configured'
        return result
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        result['error'] = 'This movie does not have a TMDB match for Radarr lookup'
        return result

    headers = {'X-Api-Key': key}
    try:
        log.debug(f"[REMOVE] Radarr movie lookup start tmdb_id={tmdb_id}")
        response = requests.get(
            url + '/api/v3/movie',
            headers=headers,
            params={'tmdbId': tmdb_id},
            timeout=8,
        )
        log.debug(f"[REMOVE] Radarr movie lookup HTTP {response.status_code} tmdb_id={tmdb_id}")
        if response.status_code >= 400:
            log.warning(f"[REMOVE] Radarr movie lookup failed HTTP {response.status_code}: {_response_excerpt(response)}")
        response.raise_for_status()
        movies = response.json()
        if not isinstance(movies, list):
            raise ValueError('Radarr movie response was not a list')
        movie = None
        for item in movies:
            try:
                if int(item.get('tmdbId', -1)) == tmdb_id:
                    movie = item
                    break
            except (AttributeError, TypeError, ValueError):
                continue
        if not movie:
            result['error'] = 'Movie was not found in Radarr'
            return result
        movie_id = int(movie['id'])
        result['movie'] = {
            'id': movie_id,
            'title': str(movie.get('title') or ''),
            'path': str(movie.get('path') or ''),
            'movie_file_path': str((movie.get('movieFile') or {}).get('path') or ''),
        }
        log.info(f"[REMOVE] Radarr matched tmdb_id={tmdb_id} radarr_id={movie_id} title={result['movie']['title']!r}")
    except Exception as exc:
        log.warning(f"[REMOVE] Radarr movie lookup failed: {type(exc).__name__}")
        result['error'] = 'Could not look up this movie in Radarr'
        return result

    queue_sources = [
        ('details', url + '/api/v3/queue/details', {'movieId': movie_id}),
        ('paged', url + '/api/v3/queue', [('pageSize', 100), ('movieIds', movie_id)]),
    ]
    last_error = ''
    queue_lookup_ok = False
    for source, queue_url, params in queue_sources:
        try:
            log.debug(f"[REMOVE] Radarr queue lookup start source={source} radarr_id={movie_id}")
            response = requests.get(queue_url, headers=headers, params=params, timeout=8)
            log.debug(f"[REMOVE] Radarr queue lookup HTTP {response.status_code} source={source} radarr_id={movie_id}")
            if response.status_code >= 400:
                log.warning(f"[REMOVE] Radarr queue lookup failed HTTP {response.status_code} source={source}: {_response_excerpt(response)}")
            response.raise_for_status()
            records = _queue_records_from_response(response.json())
            queue_lookup_ok = True
            log.debug(f"[REMOVE] Radarr queue lookup returned records={len(records)} source={source} radarr_id={movie_id}")
            _log_queue_sample(records, source)
            result['downloads'] = _matching_radarr_downloads(records, movie_id)
            log.info(f"[REMOVE] Radarr queue matched downloads={len(result['downloads'])} source={source} radarr_id={movie_id}")
            if result['downloads']:
                break
        except Exception as exc:
            last_error = type(exc).__name__
            log.warning(f"[REMOVE] Radarr queue lookup failed source={source}: {last_error}")
    if last_error and not queue_lookup_ok:
        result['queue_error'] = 'Could not look up active Radarr downloads'
    return result

def _plex_deletion_available(title):
    rating_key = str(title.get('plex_rating_key') or '')
    return bool(
        rating_key.isdigit()
        and get_setting('plex_ip', PLEX_IP)
        and get_setting('plex_token', PLEX_TOKEN)
    )

def _delete_local_title(title_id):
    """Remove a Plex-deleted title from the local cache immediately."""
    db = get_db()
    db.execute('DELETE FROM provider_links WHERE title_id=?', (title_id,))
    db.execute('DELETE FROM partial_provider_links WHERE title_id=?', (title_id,))
    db.execute('DELETE FROM titles WHERE id=?', (title_id,))
    db.commit()
    db.close()

@app.route('/api/remove/<int:title_id>/preview')
def api_remove_preview(title_id):
    title = _removal_title(title_id)
    if not title:
        return jsonify({'ok': False, 'error': 'Title not found'}), 404
    if title['media_type'] != 'movie':
        return jsonify({'ok': False, 'error': 'Sonarr removal is not wired yet'}), 400

    radarr = _radarr_context(title.get('tmdb_id'))
    torrent = _torrent_matches_for_title(title, radarr)
    return jsonify({
        'ok': True,
        'title': title['title'],
        'plex_available': _plex_deletion_available(title),
        'radarr_configured': radarr['configured'],
        'radarr_movie': radarr['movie'],
        'downloads': radarr['downloads'],
        'cleanuparr_available': _cleanuparr_configured(),
        'torrent_client_configured': torrent['configured'],
        'torrent_matches': torrent['matches'],
        'torrent_error': torrent['error'],
        'radarr_error': radarr['error'],
        'queue_error': radarr['queue_error'],
    })

@app.route('/api/remove/<int:title_id>', methods=['POST'])
def api_remove_title(title_id):
    data = _json_object()
    if data is None:
        return jsonify({'ok': False, 'error': 'Expected a JSON object'}), 400
    if data.get('confirmed') is not True:
        return jsonify({'ok': False, 'error': 'Deletion confirmation is required'}), 400

    remove_radarr = data.get('remove_radarr') is True
    delete_radarr_files = data.get('delete_radarr_files') is True
    delete_plex = data.get('delete_plex') is True
    remove_downloads = data.get('remove_downloads') is True
    trigger_cleanuparr = data.get('trigger_cleanuparr') is True
    delete_torrent_matches = data.get('delete_torrent_matches') is True
    if delete_radarr_files:
        remove_radarr = True
    if not any((remove_radarr, delete_plex, remove_downloads, delete_torrent_matches)):
        return jsonify({'ok': False, 'error': 'Select at least one removal action'}), 400

    title = _removal_title(title_id)
    if not title:
        return jsonify({'ok': False, 'error': 'Title not found'}), 404
    if title['media_type'] != 'movie':
        return jsonify({'ok': False, 'error': 'Sonarr removal is not wired yet'}), 400

    log.info(
        f"[REMOVE] Confirmed title_id={title_id} title={title['title']!r} "
        f"remove_radarr={remove_radarr} delete_radarr_files={delete_radarr_files} delete_plex={delete_plex} "
        f"remove_downloads={remove_downloads} delete_torrent_matches={delete_torrent_matches} "
        f"trigger_cleanuparr={trigger_cleanuparr}"
    )

    radarr = None
    if remove_radarr or remove_downloads:
        radarr = _radarr_context(title.get('tmdb_id'))
        if not radarr['movie']:
            return jsonify({'ok': False, 'error': radarr['error'] or 'Movie was not found in Radarr'}), 400
        if remove_downloads and radarr['queue_error']:
            return jsonify({'ok': False, 'error': radarr['queue_error']}), 502
        if remove_downloads and not radarr['downloads']:
            return jsonify({'ok': False, 'error': 'No active Radarr downloads were found for this movie'}), 400
    if delete_plex and not _plex_deletion_available(title):
        return jsonify({'ok': False, 'error': 'Plex deletion is unavailable for this title'}), 400
    if trigger_cleanuparr and not _cleanuparr_configured():
        return jsonify({'ok': False, 'error': 'Cleanuparr is not configured'}), 400
    torrent = {'matches': []}
    if delete_torrent_matches:
        torrent = _torrent_matches_for_title(title, radarr)
        if torrent['error']:
            return jsonify({'ok': False, 'error': torrent['error']}), 502
        if not torrent['matches']:
            return jsonify({'ok': False, 'error': 'No matching torrent-client entries were found'}), 400

    completed = []
    radarr_url = _normalize_url(get_setting('radarr_url'))
    radarr_headers = {'X-Api-Key': get_setting('radarr_api_key')}

    try:
        if remove_downloads:
            for download in radarr['downloads']:
                log.info(
                    f"[REMOVE] Radarr queue delete start queue_id={download['id']} "
                    f"radarr_id={radarr['movie']['id']} removeFromClient=true"
                )
                response = requests.delete(
                    f"{radarr_url}/api/v3/queue/{download['id']}",
                    headers=radarr_headers,
                    params={
                        'removeFromClient': True,
                        'blocklist': False,
                        'skipRedownload': False,
                        'changeCategory': False,
                    },
                    timeout=8,
                )
                log.info(f"[REMOVE] Radarr queue delete HTTP {response.status_code} queue_id={download['id']}")
                if response.status_code >= 400:
                    log.warning(f"[REMOVE] Radarr queue delete failed HTTP {response.status_code}: {_response_excerpt(response)}")
                response.raise_for_status()
            count = len(radarr['downloads'])
            completed.append(f"Removed {count} active download{'s' if count != 1 else ''} from the download client")

        if delete_plex:
            plex_ip = get_setting('plex_ip', PLEX_IP)
            plex_port = get_setting('plex_port', PLEX_PORT)
            plex_token = get_setting('plex_token', PLEX_TOKEN)
            rating_key = str(title['plex_rating_key'])
            response = requests.delete(
                f'http://{plex_ip}:{plex_port}/library/metadata/{rating_key}',
                headers=_plex_headers(plex_token),
                timeout=8,
            )
            log.info(f"[REMOVE] Plex delete HTTP {response.status_code} rating_key={rating_key}")
            if response.status_code == 404:
                log.info(f"[REMOVE] Plex item already absent rating_key={rating_key}")
            elif response.status_code >= 400:
                log.warning(f"[REMOVE] Plex delete failed HTTP {response.status_code}: {_response_excerpt(response)}")
            if response.status_code == 404:
                completed.append('Removed stale Plex cache entry; Plex item was already absent')
            else:
                response.raise_for_status()
                completed.append('Deleted the movie from Plex and disk')
            _delete_local_title(title_id)

        if remove_radarr:
            log.info(f"[REMOVE] Radarr movie delete start radarr_id={radarr['movie']['id']} deleteFiles={str(delete_radarr_files).lower()}")
            response = requests.delete(
                f"{radarr_url}/api/v3/movie/{radarr['movie']['id']}",
                headers=radarr_headers,
                params={'deleteFiles': delete_radarr_files, 'addImportExclusion': False},
                timeout=8,
            )
            log.info(f"[REMOVE] Radarr movie delete HTTP {response.status_code} radarr_id={radarr['movie']['id']}")
            if response.status_code >= 400:
                log.warning(f"[REMOVE] Radarr movie delete failed HTTP {response.status_code}: {_response_excerpt(response)}")
            response.raise_for_status()
            completed.append('Removed the movie from Radarr and deleted Radarr-managed files' if delete_radarr_files else 'Removed the movie from Radarr')

        if delete_torrent_matches:
            match_ids = [match['id'] for match in torrent['matches']]
            _delete_torrent_client_entries(match_ids, delete_files=True)
            count = len(match_ids)
            completed.append(f"Deleted {count} matched torrent-client entr{'y' if count == 1 else 'ies'} with data")

        if trigger_cleanuparr:
            _trigger_cleanuparr_download_cleaner()
            completed.append('Triggered Cleanuparr Download Cleaner')
    except Exception as exc:
        log.warning(f"[REMOVE] Remote deletion failed: {type(exc).__name__}")
        return jsonify({
            'ok': False,
            'error': 'A remote deletion failed. Completed actions were not rolled back.',
            'completed': completed,
        }), 502

    return jsonify({'ok': True, 'completed': completed})

def _test_arr(url, key, app_name):
    """Test a Radarr or Sonarr connection. Tries v3 then v1 API."""
    url = _normalize_url(url)
    if not url:
        return {'ok': False, 'msg': f'No URL provided for {app_name}'}
    if not key:
        return {'ok': False, 'msg': f'No API key provided for {app_name}'}

    last_err = None
    for api_path in ['/api/v3/system/status', '/api/v1/system/status']:
        try:
            log.info(f"[TEST] {app_name} header → {url}{api_path}")
            r = requests.get(url + api_path, headers={'X-Api-Key': key}, timeout=8)
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
            break
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

        elif target == 'cleanuparr':
            url = _normalize_url(data.get('cleanuparr_url', '') or get_setting('cleanuparr_url'))
            key = str(data.get('cleanuparr_api_key', '') or '')
            if '••••' in key or not key:
                key = get_setting('cleanuparr_api_key')
            if not url:
                return jsonify({'ok': False, 'msg': 'No URL provided for Cleanuparr'})
            if not key:
                return jsonify({'ok': False, 'msg': 'No API key provided for Cleanuparr'})
            log.info(f"[TEST] Cleanuparr → {url}/api/stats")
            r = requests.get(url + '/api/stats', headers={'X-Api-Key': key}, timeout=8)
            if r.status_code in (401, 403):
                return jsonify({'ok': False, 'msg': f'HTTP {r.status_code} — API key rejected by Cleanuparr'})
            r.raise_for_status()
            return jsonify({'ok': True, 'msg': f'Cleanuparr connected at {url}'})

        elif target == 'torrent_client':
            client_type = _torrent_client_type(data.get('torrent_client_type', get_setting('torrent_client_type')))
            url = data.get('torrent_client_url', '') or get_setting('torrent_client_url')
            username = str(data.get('torrent_client_username', get_setting('torrent_client_username')) or '')
            password = str(data.get('torrent_client_password', '') or '')
            if '••••' in password or not password:
                password = get_setting('torrent_client_password')
            if not client_type:
                return jsonify({'ok': False, 'msg': 'Choose qBittorrent or Transmission'})
            if not url:
                return jsonify({'ok': False, 'msg': 'No URL provided for torrent client'})
            if client_type == 'qbittorrent':
                qbit_url, session = _qbittorrent_session(url, username, password)
                r = session.get(qbit_url + '/api/v2/app/version', timeout=8)
                r.raise_for_status()
                return jsonify({'ok': True, 'msg': f'qBittorrent {r.text.strip()} connected'})
            if client_type == 'transmission':
                _transmission_rpc('session-get', {}, url, username, password)
                return jsonify({'ok': True, 'msg': 'Transmission connected'})

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
    Probe common local/Docker addresses and ports to auto-detect integrations.
    Discovery never saves settings; it only returns candidate URLs for the UI.
    """
    data = _json_object()
    if data is None:
        return jsonify({'ok': False, 'error': 'Expected a JSON object'}), 400
    targets = data.get('targets', ['radarr', 'sonarr'])

    arr_ports = {
        'radarr': [7878, 7879],
        'sonarr': [8989, 8990],
    }
    cleanuparr_ports = [11011, 8080]
    torrent_clients = {
        'qbittorrent': {
            'ports': [8080, 8081, 8090, 8091],
            'hosts': ['qbittorrent', 'qbittorrentvpn', 'binhex-qbittorrentvpn', 'gluetun'],
        },
        'transmission': {
            'ports': [9091],
            'hosts': ['transmission', 'transmission-openvpn', 'transmissionvpn', 'gluetun'],
        },
    }

    # Hosts to try: Docker host gateways, loopback for non-Docker runs, service DNS names,
    # plus any custom hint from the UI.
    base_hosts = ['host.docker.internal', 'host.containers.internal', 'localhost', '127.0.0.1']
    custom_hint = str(data.get('hint') or '').strip()
    hint_host = ''
    hint_port = None
    if custom_hint:
        try:
            parsed_hint = urlsplit(custom_hint if '://' in custom_hint else f'http://{custom_hint}')
            hint_host = parsed_hint.hostname or ''
            hint_port = parsed_hint.port
        except ValueError:
            hint_host = custom_hint.replace('http://','').replace('https://','').split('/')[0].split(':')[0]
            hint_port = None
        if hint_host and hint_host not in base_hosts:
            base_hosts.insert(0, hint_host)

    found = {}
    found_priority = {}

    def ordered(items):
        return list(dict.fromkeys(item for item in items if item))

    def record_found(app_name, result, priority):
        if result and (app_name not in found_priority or priority < found_priority[app_name]):
            found[app_name] = result
            found_priority[app_name] = priority

    def probe_arr(app_name, host, port):
        url = f'http://{host}:{port}'
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

    def probe_cleanuparr(host, port):
        url = f'http://{host}:{port}'
        for path in ('/health', '/api/stats'):
            try:
                r = requests.get(url + path, timeout=3, allow_redirects=False)
                if r.status_code in (200, 204, 401, 403):
                    log.info(f"[DISCOVER] cleanuparr found at {url} via {path} (HTTP {r.status_code})")
                    return url
            except Exception:
                pass
        return None

    def probe_qbittorrent(host, port):
        url = f'http://{host}:{port}'
        try:
            r = requests.get(url + '/api/v2/app/version', timeout=3, allow_redirects=False)
            if r.status_code in (200, 401, 403):
                log.info(f"[DISCOVER] qBittorrent found at {url} (HTTP {r.status_code})")
                return {'type': 'qbittorrent', 'url': url}
        except Exception:
            pass
        return None

    def probe_transmission(host, port):
        url = f'http://{host}:{port}'
        try:
            r = requests.post(
                url + '/transmission/rpc',
                json={'method': 'session-get', 'arguments': {}},
                timeout=3,
                allow_redirects=False,
            )
            if r.status_code in (200, 409, 401, 403):
                log.info(f"[DISCOVER] Transmission found at {url} (HTTP {r.status_code})")
                return {'type': 'transmission', 'url': url}
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = {}
        for app_name in targets:
            if app_name in arr_ports:
                hosts = ordered(base_hosts + [app_name])
                ports = ordered(([hint_port] if hint_port else []) + arr_ports[app_name])
                for host_index, host in enumerate(hosts):
                    for port_index, port in enumerate(ports):
                        priority = host_index * 100 + port_index
                        f = pool.submit(probe_arr, app_name, host, port)
                        futures[f] = (app_name, priority)
            elif app_name == 'cleanuparr':
                hosts = ordered(base_hosts + ['cleanuparr'])
                ports = ordered(([hint_port] if hint_port else []) + cleanuparr_ports)
                for host_index, host in enumerate(hosts):
                    for port_index, port in enumerate(ports):
                        priority = host_index * 100 + port_index
                        f = pool.submit(probe_cleanuparr, host, port)
                        futures[f] = (app_name, priority)
            elif app_name in ('torrent_client', 'download_client'):
                for client_index, (client_type, meta) in enumerate(torrent_clients.items()):
                    hosts = ordered(base_hosts + meta['hosts'])
                    ports = ordered(([hint_port] if hint_port else []) + meta['ports'])
                    for host_index, host in enumerate(hosts):
                        for port_index, port in enumerate(ports):
                            priority = client_index * 10000 + host_index * 100 + port_index
                            probe = probe_qbittorrent if client_type == 'qbittorrent' else probe_transmission
                            f = pool.submit(probe, host, port)
                            futures[f] = ('torrent_client', priority)

        for future in as_completed(futures):
            app_name, priority = futures[future]
            result = future.result()
            record_found(app_name, result, priority)

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

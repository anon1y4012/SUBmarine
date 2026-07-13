import sqlite3

import app as app_module


def sync_row(app):
    db = app.get_db()
    row = db.execute('SELECT * FROM sync_status WHERE id=1').fetchone()
    db.close()
    return row


class TestInitDb:
    def test_idempotent(self, app):
        app.init_db()  # second run must not fail or duplicate rows
        db = app.get_db()
        assert db.execute('SELECT COUNT(*) FROM sync_status').fetchone()[0] == 1
        db.close()

    def test_seeds_default_settings(self, app):
        db = app.get_db()
        keys = {r['key'] for r in db.execute('SELECT key FROM settings')}
        db.close()
        assert {'plex_ip', 'plex_port', 'tmdb_api_key', 'sync_workers', 'log_level'} <= keys

    def test_resets_stuck_sync_flag(self, app):
        db = app.get_db()
        db.execute('UPDATE sync_status SET is_syncing=1 WHERE id=1')
        db.commit()
        db.close()
        app.init_db()
        assert sync_row(app)['is_syncing'] == 0

    def test_migrates_legacy_provider_links(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / 'legacy.db')
        legacy = sqlite3.connect(db_path)
        legacy.executescript('''
            CREATE TABLE titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plex_rating_key TEXT UNIQUE, title TEXT NOT NULL, year TEXT,
                media_type TEXT NOT NULL, thumb_url TEXT, tmdb_id TEXT,
                last_updated REAL DEFAULT 0
            );
            CREATE TABLE provider_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title_id INTEGER NOT NULL, provider_name TEXT NOT NULL,
                leaving_date TEXT, UNIQUE(title_id, provider_name)
            );
            INSERT INTO titles (plex_rating_key, title, media_type)
                VALUES ('1', 'Old Movie', 'movie');
            INSERT INTO provider_links (title_id, provider_name, leaving_date)
                VALUES (1, 'Netflix', '2024-01-01');
        ''')
        legacy.commit()
        legacy.close()

        monkeypatch.setattr(app_module, 'DB_PATH', db_path)
        app_module.init_db()

        db = app_module.get_db()
        cols = {row[1] for row in db.execute('PRAGMA table_info(provider_links)')}
        assert 'leaving_date' not in cols
        assert db.execute('SELECT provider_name FROM provider_links').fetchone()[0] == 'Netflix'
        title_cols = {row[1] for row in db.execute('PRAGMA table_info(titles)')}
        assert 'plex_signature' in title_cols
        db.close()


class TestClaimSync:
    def test_claims_once(self, app):
        assert app.claim_sync() is True
        assert app.claim_sync() is False

    def test_reclaim_after_release(self, app):
        assert app.claim_sync() is True
        app.set_sync_status('done', syncing=False)
        assert app.claim_sync() is True


class TestSetSyncStatus:
    def test_updates_message_and_progress(self, app):
        app.set_sync_status('halfway', syncing=True, synced=5, total=10)
        row = sync_row(app)
        assert row['sync_message'] == 'halfway'
        assert row['is_syncing'] == 1
        assert row['synced_count'] == 5
        assert row['total_count'] == 10


class TestRunSync:
    def test_failure_releases_sync_flag(self, app, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError('plex exploded')
        monkeypatch.setattr(app_module, 'fetch_plex_library', boom)
        app.run_sync()
        row = sync_row(app)
        assert row['is_syncing'] == 0
        assert 'ERROR' in row['sync_message']

    def test_empty_plex_sets_error(self, app, monkeypatch):
        monkeypatch.setattr(app_module, 'fetch_plex_library', lambda *a, **k: [])
        monkeypatch.setattr(app_module, 'fetch_plex_tv_seasons', lambda *a, **k: None)
        app.run_sync()
        row = sync_row(app)
        assert row['is_syncing'] == 0
        assert 'ERROR' in row['sync_message']

    def test_end_to_end_with_mocked_externals(self, app, monkeypatch):
        movies = [{'key': 'm1', 'title': 'Film A', 'year': '2020', 'thumb': ''}]
        shows = [{'key': 't1', 'title': 'Show B', 'year': '2021', 'thumb': ''}]

        def fake_fetch(library_id, media_tag):
            return movies if media_tag == 'Video' else shows

        monkeypatch.setattr(app_module, 'fetch_plex_library', fake_fetch)
        monkeypatch.setattr(app_module, 'fetch_plex_tv_seasons', lambda lib: {'t1': {1, 2}})
        monkeypatch.setattr(app_module, 'tmdb_search', lambda title, mtype: '77')
        monkeypatch.setattr(app_module, 'tmdb_providers', lambda tmdb_id, mtype: ['Netflix'])
        monkeypatch.setattr(app_module, 'tmdb_tv_season_providers',
                            lambda tmdb_id, season: ['Hulu'] if season == 1 else [])

        app.run_sync()

        row = sync_row(app)
        assert row['is_syncing'] == 0
        assert row['last_sync'] > 0

        client = app.app.test_client()
        titles = {t['title']: t for t in client.get('/api/titles').get_json()}
        assert titles['Film A']['providers'] == ['Netflix']
        assert titles['Show B']['partial_providers'] == [{'name': 'Hulu', 'seasons': [1]}]

    def test_resync_uses_cache_and_prunes_stale(self, app, monkeypatch):
        movies = [{'key': 'm1', 'title': 'Film A', 'year': '2020', 'thumb': ''}]
        calls = {'search': 0}

        def fake_search(title, mtype):
            calls['search'] += 1
            return '77'

        monkeypatch.setattr(app_module, 'fetch_plex_library',
                            lambda lib, tag: movies if tag == 'Video' else [])
        monkeypatch.setattr(app_module, 'fetch_plex_tv_seasons', lambda lib: None)
        monkeypatch.setattr(app_module, 'tmdb_search', fake_search)
        monkeypatch.setattr(app_module, 'tmdb_providers', lambda tmdb_id, mtype: ['Netflix'])

        app.run_sync()
        assert calls['search'] == 1
        app.run_sync()  # unchanged item: no new TMDB search
        assert calls['search'] == 1

        # Title disappears from Plex -> pruned on next sync
        movies.clear()
        movies.append({'key': 'm2', 'title': 'Film C', 'year': '2022', 'thumb': ''})
        app.run_sync()
        client = app.app.test_client()
        titles = [t['title'] for t in client.get('/api/titles').get_json()]
        assert titles == ['Film C']

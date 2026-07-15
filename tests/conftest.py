import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """A fresh app instance backed by a temporary database."""
    db_path = str(tmp_path / 'test.db')
    monkeypatch.setattr(app_module, 'DB_PATH', db_path)
    monkeypatch.delenv(app_module.AUTH_TOKEN_ENV, raising=False)
    app_module.init_db()
    app_module.app.config['TESTING'] = True
    return app_module


@pytest.fixture()
def client(app):
    return app.app.test_client()


def set_setting(app, key, value):
    db = app.get_db()
    db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    db.commit()
    db.close()


def insert_title(app, title='Example', year='2020', media_type='movie',
                 rating_key='1', thumb_url='', tmdb_id=None, providers=(),
                 partial_providers=()):
    db = app.get_db()
    cursor = db.execute(
        'INSERT INTO titles (plex_rating_key, title, year, media_type, thumb_url, tmdb_id) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (rating_key, title, year, media_type, thumb_url, tmdb_id),
    )
    title_id = cursor.lastrowid
    for provider in providers:
        db.execute('INSERT INTO provider_links (title_id, provider_name) VALUES (?, ?)',
                   (title_id, provider))
    for provider, seasons in partial_providers:
        db.execute('INSERT INTO partial_provider_links (title_id, provider_name, seasons) VALUES (?, ?, ?)',
                   (title_id, provider, seasons))
    db.commit()
    db.close()
    return title_id

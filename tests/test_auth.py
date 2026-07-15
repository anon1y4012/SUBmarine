from conftest import set_setting

import app as app_module


def complete_setup_with_token(app, token='test-token'):
    set_setting(app, 'setup_complete', '1')
    set_setting(app, app.AUTH_TOKEN_SETTING, app._hash_auth_token(token))
    return token


class TestAuthEnforcement:
    def test_open_before_setup(self, client):
        """Pre-setup, endpoints stay open so the wizard can run."""
        assert client.get('/api/settings').status_code == 200

    def test_open_without_configured_token(self, app, client):
        """Legacy installs without a token keep local-first behavior."""
        set_setting(app, 'setup_complete', '1')
        assert client.get('/api/settings').status_code == 200

    def test_protected_endpoints_require_token(self, app, client):
        complete_setup_with_token(app)
        for path in ('/api/settings', '/api/debug/plex'):
            r = client.get(path)
            assert r.status_code == 401, path
            assert r.get_json()['auth_required'] is True
        assert client.post('/api/sync').status_code == 401
        assert client.post('/api/remove/1', json={}).status_code == 401

    def test_bearer_token_accepted(self, app, client):
        token = complete_setup_with_token(app)
        r = client.get('/api/settings', headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 200

    def test_custom_header_accepted(self, app, client):
        token = complete_setup_with_token(app)
        r = client.get('/api/settings', headers={'X-Submarine-Token': token})
        assert r.status_code == 200

    def test_wrong_token_rejected(self, app, client):
        complete_setup_with_token(app)
        r = client.get('/api/settings', headers={'Authorization': 'Bearer wrong'})
        assert r.status_code == 401

    def test_public_endpoints_stay_open(self, app, client):
        complete_setup_with_token(app)
        for path in ('/api/titles', '/api/status', '/api/service_counts',
                     '/api/setup/status', '/api/health', '/'):
            assert client.get(path).status_code == 200, path

    def test_env_token_overrides_db(self, app, client, monkeypatch):
        complete_setup_with_token(app, 'db-token')
        monkeypatch.setenv(app_module.AUTH_TOKEN_ENV, 'env-token')
        assert client.get('/api/settings',
                          headers={'X-Submarine-Token': 'db-token'}).status_code == 401
        assert client.get('/api/settings',
                          headers={'X-Submarine-Token': 'env-token'}).status_code == 200

    def test_setup_save_locked_after_setup(self, app, client):
        """Re-running setup after completion requires the token."""
        complete_setup_with_token(app)
        r = client.post('/api/setup/save', json={
            'plex_ip': 'x', 'plex_token': 'x', 'tmdb_api_key': 'x',
        })
        assert r.status_code == 401


class TestCrossSiteWriteProtection:
    def test_cross_site_fetch_rejected(self, client):
        r = client.post('/api/sync', headers={'Sec-Fetch-Site': 'cross-site'})
        assert r.status_code == 403

    def test_cross_origin_rejected(self, client):
        r = client.post('/api/sync', headers={'Origin': 'http://evil.example'})
        assert r.status_code == 403

    def test_same_origin_allowed(self, client):
        r = client.post('/api/settings', json={}, headers={'Origin': 'http://localhost'})
        assert r.status_code == 200

    def test_get_requests_unaffected(self, client):
        r = client.get('/api/status', headers={'Sec-Fetch-Site': 'cross-site'})
        assert r.status_code == 200


class TestTokenHelpers:
    def test_hash_is_sha256_hex(self, app):
        digest = app._hash_auth_token('abc')
        assert len(digest) == 64
        assert digest == app._hash_auth_token('abc')

    def test_ensure_auth_token_mints_once(self, app):
        db = app.get_db()
        first = app._ensure_auth_token(db)
        second = app._ensure_auth_token(db)
        db.commit()
        db.close()
        assert first and len(first) >= 32
        assert second is None

    def test_ensure_auth_token_skipped_with_env(self, app, monkeypatch):
        monkeypatch.setenv(app_module.AUTH_TOKEN_ENV, 'env-token')
        db = app.get_db()
        assert app._ensure_auth_token(db) is None
        db.close()

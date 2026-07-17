from conftest import insert_title, set_setting


class TestHealth:
    def test_health_ok(self, client):
        r = client.get('/api/health')
        assert r.status_code == 200
        assert r.get_json()['ok'] is True


class TestIndex:
    def test_serves_page(self, client):
        r = client.get('/')
        assert r.status_code == 200
        assert b'SUBmarine' in r.data


class TestStatus:
    def test_empty_db_counts_are_zero(self, client):
        r = client.get('/api/status')
        data = r.get_json()
        assert data['total'] == 0
        assert data['movies'] == 0
        assert data['tv'] == 0
        assert data['is_syncing'] is False

    def test_counts_by_type(self, app, client):
        insert_title(app, rating_key='1', media_type='movie')
        insert_title(app, rating_key='2', media_type='tv')
        data = client.get('/api/status').get_json()
        assert data == {**data, 'total': 2, 'movies': 1, 'tv': 1}


class TestTitles:
    def test_lists_titles_with_providers(self, app, client):
        insert_title(app, title='Alpha', rating_key='1', providers=['Netflix', 'Hulu'])
        rows = client.get('/api/titles').get_json()
        assert len(rows) == 1
        assert rows[0]['title'] == 'Alpha'
        assert sorted(rows[0]['providers']) == ['Hulu', 'Netflix']

    def test_type_filter(self, app, client):
        insert_title(app, title='Film', rating_key='1', media_type='movie')
        insert_title(app, title='Show', rating_key='2', media_type='tv')
        rows = client.get('/api/titles?type=tv').get_json()
        assert [r['title'] for r in rows] == ['Show']

    def test_service_filter_includes_partial(self, app, client):
        insert_title(app, title='FullMatch', rating_key='1', providers=['Netflix'])
        insert_title(app, title='PartialMatch', rating_key='2', media_type='tv',
                     partial_providers=[('Netflix', '1,2')])
        insert_title(app, title='NoMatch', rating_key='3', providers=['Hulu'])
        rows = client.get('/api/titles?services=Netflix').get_json()
        assert sorted(r['title'] for r in rows) == ['FullMatch', 'PartialMatch']

    def test_partial_providers_shape(self, app, client):
        insert_title(app, title='Show', rating_key='1', media_type='tv',
                     partial_providers=[('Max', '1,3')])
        rows = client.get('/api/titles').get_json()
        assert rows[0]['partial_providers'] == [{'name': 'Max', 'seasons': [1, 3]}]

    def test_thumb_url_proxied(self, app, client):
        insert_title(app, rating_key='1', thumb_url='http://plex:32400/thumb/1')
        rows = client.get('/api/titles').get_json()
        assert rows[0]['thumb'].startswith('/api/thumb/')
        assert 'plex' not in rows[0]['thumb']


class TestServiceCounts:
    def test_counts_and_overlap(self, app, client):
        insert_title(app, rating_key='1', providers=['Netflix'])
        insert_title(app, rating_key='2', providers=['Netflix', 'Hulu'])
        insert_title(app, rating_key='3')  # not on any service
        data = client.get('/api/service_counts').get_json()
        assert data['service_counts'] == {'Netflix': 2, 'Hulu': 1}
        assert data['overlap'] == 2
        assert data['total'] == 3
        assert data['unavailable'] == 1

    def test_partial_counts_deduplicated(self, app, client):
        insert_title(app, rating_key='1', media_type='tv', providers=['Netflix'],
                     partial_providers=[('Netflix', '1')])
        data = client.get('/api/service_counts').get_json()
        assert data['service_counts'] == {'Netflix': 1}

    def test_media_type_scope(self, app, client):
        insert_title(app, rating_key='1', media_type='movie', providers=['Netflix'])
        insert_title(app, rating_key='2', media_type='tv', providers=['Netflix'])
        data = client.get('/api/service_counts?type=movie').get_json()
        assert data['service_counts'] == {'Netflix': 1}
        assert data['total'] == 1


class TestThumb:
    def test_missing_title_404(self, client):
        assert client.get('/api/thumb/999').status_code == 404

    def test_title_without_thumb_404(self, app, client):
        title_id = insert_title(app, rating_key='1', thumb_url='')
        assert client.get(f'/api/thumb/{title_id}').status_code == 404


class TestSetup:
    def test_status_incomplete_on_fresh_db(self, client):
        assert client.get('/api/setup/status').get_json() == {'complete': False}

    def test_save_requires_core_fields(self, client):
        r = client.post('/api/setup/save', json={'plex_ip': '1.2.3.4'})
        assert r.status_code == 400

    def test_save_rejects_non_object(self, client):
        r = client.post('/api/setup/save', json=['not', 'an', 'object'])
        assert r.status_code == 400

    def test_save_completes_setup_and_mints_token(self, app, client):
        r = client.post('/api/setup/save', json={
            'plex_ip': '1.2.3.4', 'plex_token': 'ptok', 'tmdb_api_key': 'tkey',
        })
        data = r.get_json()
        assert data['ok'] is True
        assert data['auth_token']  # plaintext token returned exactly once
        assert client.get('/api/setup/status').get_json() == {'complete': True}
        # Server keeps only the hash
        stored = app.get_setting(app.AUTH_TOKEN_SETTING)
        assert stored != data['auth_token']
        assert len(stored) == 64

    def test_save_ignores_unknown_keys(self, app, client):
        client.post('/api/setup/save', json={
            'plex_ip': '1.2.3.4', 'plex_token': 'p', 'tmdb_api_key': 't',
            'evil_key': 'value',
        })
        assert app.get_setting('evil_key') == ''


class TestSettings:
    def test_get_masks_secrets(self, app, client):
        set_setting(app, 'plex_token', 'super-secret')
        set_setting(app, 'tmdb_api_key', 'also-secret')
        data = client.get('/api/settings').get_json()
        assert data['plex_token'] == ''
        assert data['plex_token_set'] is True
        assert data['tmdb_api_key'] == ''
        assert data['tmdb_api_key_set'] is True
        assert 'super-secret' not in str(data)

    def test_post_updates_allowed_keys_only(self, app, client):
        r = client.post('/api/settings', json={'plex_ip': '5.6.7.8', 'nope': 'x'})
        assert r.get_json()['updated'] == ['plex_ip']
        assert app.get_setting('plex_ip') == '5.6.7.8'

    def test_post_skips_masked_placeholders(self, app, client):
        set_setting(app, 'plex_token', 'original')
        client.post('/api/settings', json={'plex_token': '••••••••'})
        assert app.get_setting('plex_token') == 'original'

    def test_post_rejects_bad_log_level(self, client):
        r = client.post('/api/settings', json={'log_level': 'VERBOSE'})
        assert r.status_code == 400

    def test_post_normalizes_torrent_client_type(self, app, client):
        client.post('/api/settings', json={'torrent_client_type': 'QBitTorrent'})
        assert app.get_setting('torrent_client_type') == 'qbittorrent'
        client.post('/api/settings', json={'torrent_client_type': 'utorrent'})
        assert app.get_setting('torrent_client_type') == ''


class TestRemoval:
    def test_preview_unknown_title(self, client):
        assert client.get('/api/remove/999/preview').status_code == 404

    def test_remove_requires_confirmation(self, app, client):
        title_id = insert_title(app, rating_key='1')
        r = client.post(f'/api/remove/{title_id}', json={})
        assert r.status_code == 400

    def test_remove_requires_an_action(self, app, client):
        title_id = insert_title(app, rating_key='1')
        r = client.post(f'/api/remove/{title_id}', json={'confirmed': True})
        assert r.status_code == 400

    def test_tv_removal_requires_sonarr_config(self, app, client):
        title_id = insert_title(app, rating_key='1', media_type='tv')
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'remove_sonarr': True})
        assert r.status_code == 400
        assert 'Sonarr' in r.get_json()['error']

from conftest import insert_title, set_setting

import app as app_module


def _configure_sonarr(app):
    set_setting(app, 'sonarr_url', 'http://sonarr:8989')
    set_setting(app, 'sonarr_api_key', 'key')


def _configure_cleanuparr(app):
    set_setting(app, 'cleanuparr_url', 'http://cleanuparr:11011')
    set_setting(app, 'cleanuparr_api_key', 'key')


def _seed_result(available=True, seconds=0, error=''):
    return {'available': available, 'min_seed_seconds': seconds, 'error': error}


class TestSeedRuleParsing:
    def test_collects_camel_and_snake_case_keys(self):
        found = []
        app_module._collect_seed_time_hours({
            'categories': [
                {'name': 'radarr', 'minSeedTime': 48},
                {'name': 'tv', 'min_seed_time': '12'},
            ],
        }, found)
        assert sorted(found) == [12.0, 48.0]

    def test_max_seed_time_counts_when_min_is_zero(self):
        # Cleanuparr cleans on max seed time alone (regardless of ratio), so a
        # rule with minSeedTime 0 but maxSeedTime 240 protects until 240h.
        found = []
        app_module._collect_seed_time_hours({
            'clients': [{'downloadClientName': 'qbit', 'seedingRules': [
                {'name': 'default', 'maxRatio': -1, 'minSeedTime': 0, 'maxSeedTime': 240},
            ]}],
        }, found)
        assert found == [240.0]

    def test_strictest_bound_wins_within_a_rule(self):
        found = []
        app_module._collect_seed_time_hours({'minSeedTime': 48, 'maxSeedTime': 24}, found)
        assert found == [48.0]

    def test_disabled_bounds_still_record_the_rule(self):
        found = []
        app_module._collect_seed_time_hours(
            {'minSeedTime': 'soon', 'nested': [{'minSeedTime': -1}, {'minSeedTime': 0, 'maxSeedTime': -1}]},
            found,
        )
        assert found == [0.0, 0.0]


class TestSeedProtectionLookup:
    def test_reads_modern_download_cleaner_shape(self, app, monkeypatch):
        _configure_cleanuparr(app)
        payload = {'enabled': True, 'clients': [{'downloadClientName': 'qbit', 'seedingRules': [
            {'name': 'default', 'maxRatio': -1, 'minSeedTime': 0, 'maxSeedTime': 240},
        ]}]}

        class FakeResponse:
            status_code = 200

            def json(self):
                return payload

        monkeypatch.setattr(app_module.requests, 'get', lambda *a, **k: FakeResponse())
        app_module._cleanuparr_seed_cache['result'] = None
        app_module._cleanuparr_seed_cache['expires'] = 0.0
        try:
            result = app_module._cleanuparr_seed_protection()
        finally:
            app_module._cleanuparr_seed_cache['result'] = None
            app_module._cleanuparr_seed_cache['expires'] = 0.0
        assert result == {'available': True, 'min_seed_seconds': 240 * 3600, 'error': ''}


class TestSplitSeedProtected:
    def test_partitions_by_seeding_time(self):
        matches = [
            {'id': 'a', 'seeding_seconds': 10},
            {'id': 'b', 'seeding_seconds': 7200},
            {'id': 'c'},  # unknown seed time counts as 0 → protected
        ]
        deletable, protected = app_module._split_seed_protected(matches, 3600)
        assert [m['id'] for m in deletable] == ['b']
        assert [m['id'] for m in protected] == ['a', 'c']

    def test_zero_minimum_protects_nothing(self):
        matches = [{'id': 'a', 'seeding_seconds': 0}]
        deletable, protected = app_module._split_seed_protected(matches, 0)
        assert deletable == matches
        assert protected == []


class TestQbittorrentSeedingSeconds:
    def test_prefers_reported_seeding_time(self):
        assert app_module._qbittorrent_seeding_seconds({'seeding_time': 500}) == 500

    def test_falls_back_to_completion_timestamp(self):
        import time
        completed = int(time.time()) - 900
        seconds = app_module._qbittorrent_seeding_seconds({'completion_on': completed})
        assert 890 <= seconds <= 910

    def test_unknown_is_zero(self):
        assert app_module._qbittorrent_seeding_seconds({}) == 0


class TestTvPreview:
    def test_tv_preview_uses_sonarr(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Show', rating_key='9', media_type='tv')
        monkeypatch.setattr(app_module, '_sonarr_context', lambda title: {
            'configured': True,
            'series': {'id': 4, 'title': 'Show', 'path': '/tv/Show'},
            'downloads': [],
            'error': '',
            'queue_error': '',
        })
        monkeypatch.setattr(app_module, '_torrent_matches_for_title',
                            lambda title, radarr=None, entries=None:
                            {'configured': False, 'matches': [], 'error': ''})
        data = client.get(f'/api/remove/{title_id}/preview').get_json()
        assert data['ok'] is True
        assert data['media_type'] == 'tv'
        assert data['arr_name'] == 'sonarr'
        assert data['arr_item']['id'] == 4

    def test_preview_marks_seed_protected_matches(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Example', rating_key='10', tmdb_id='55')
        monkeypatch.setattr(app_module, '_radarr_context', lambda tmdb_id: {
            'configured': False, 'movie': None, 'downloads': [],
            'error': '', 'queue_error': '',
        })
        monkeypatch.setattr(app_module, '_torrent_matches_for_title',
                            lambda title, radarr=None, entries=None: {
                                'configured': True,
                                'matches': [
                                    {'id': 'young', 'name': 'a', 'seeding_seconds': 60},
                                    {'id': 'old', 'name': 'b', 'seeding_seconds': 999999},
                                ],
                                'error': '',
                            })
        monkeypatch.setattr(app_module, '_cleanuparr_seed_protection',
                            lambda: _seed_result(True, 3600))
        data = client.get(f'/api/remove/{title_id}/preview').get_json()
        flags = {m['id']: m['seed_protected'] for m in data['torrent_matches']}
        assert flags == {'young': True, 'old': False}
        assert data['seed_protection']['min_seed_seconds'] == 3600


class TestTvRemoval:
    def test_sonarr_series_delete_called(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Show', rating_key='9', media_type='tv')
        _configure_sonarr(app)
        monkeypatch.setattr(app_module, '_sonarr_context', lambda title: {
            'configured': True,
            'series': {'id': 4, 'title': 'Show', 'path': '/tv/Show'},
            'downloads': [],
            'error': '',
            'queue_error': '',
        })
        calls = []

        class FakeResponse:
            status_code = 200
            text = ''

            def raise_for_status(self):
                pass

        def fake_delete(url, headers=None, params=None, timeout=None):
            calls.append({'url': url, 'params': params})
            return FakeResponse()

        monkeypatch.setattr(app_module.requests, 'delete', fake_delete)
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'remove_arr': True, 'delete_arr_files': True})
        data = r.get_json()
        assert r.status_code == 200, data
        assert calls[0]['url'].endswith('/api/v3/series/4')
        assert calls[0]['params']['deleteFiles'] is True
        assert calls[0]['params']['addImportListExclusion'] is False
        assert any('Sonarr' in step for step in data['completed'])

    def test_legacy_movie_payload_keys_still_work(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Example', rating_key='1', tmdb_id='55')
        set_setting(app, 'radarr_url', 'http://radarr:7878')
        set_setting(app, 'radarr_api_key', 'key')
        monkeypatch.setattr(app_module, '_radarr_context', lambda tmdb_id: {
            'configured': True,
            'movie': {'id': 7, 'title': 'Example', 'path': '/m/Example', 'movie_file_path': ''},
            'downloads': [],
            'error': '',
            'queue_error': '',
        })
        calls = []

        class FakeResponse:
            status_code = 200
            text = ''

            def raise_for_status(self):
                pass

        monkeypatch.setattr(app_module.requests, 'delete',
                            lambda url, **kw: calls.append(url) or FakeResponse())
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'remove_radarr': True})
        assert r.status_code == 200
        assert calls and calls[0].endswith('/api/v3/movie/7')


class TestSeedProtectedRemoval:
    def _movie_with_matches(self, app, monkeypatch, matches):
        title_id = insert_title(app, title='Example', rating_key='1', tmdb_id='55')
        monkeypatch.setattr(app_module, '_torrent_matches_for_title',
                            lambda title, radarr=None, entries=None:
                            {'configured': True, 'matches': matches, 'error': ''})
        return title_id

    def test_protected_torrents_are_kept(self, app, client, monkeypatch):
        _configure_cleanuparr(app)
        matches = [
            {'id': 'young', 'name': 'a', 'seeding_seconds': 60},
            {'id': 'old', 'name': 'b', 'seeding_seconds': 999999},
        ]
        title_id = self._movie_with_matches(app, monkeypatch, matches)
        monkeypatch.setattr(app_module, '_cleanuparr_seed_protection',
                            lambda: _seed_result(True, 3600))
        deleted = []
        monkeypatch.setattr(app_module, '_delete_torrent_client_entries',
                            lambda ids, delete_files=True: deleted.extend(ids))
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'delete_torrent_matches': True,
                              'protect_seeding': True})
        data = r.get_json()
        assert r.status_code == 200, data
        assert deleted == ['old']
        assert any('Kept 1 torrent' in step for step in data['completed'])

    def test_all_protected_deletes_nothing_but_succeeds(self, app, client, monkeypatch):
        _configure_cleanuparr(app)
        matches = [{'id': 'young', 'name': 'a', 'seeding_seconds': 60}]
        title_id = self._movie_with_matches(app, monkeypatch, matches)
        monkeypatch.setattr(app_module, '_cleanuparr_seed_protection',
                            lambda: _seed_result(True, 3600))
        monkeypatch.setattr(app_module, '_delete_torrent_client_entries',
                            lambda ids, delete_files=True: (_ for _ in ()).throw(
                                AssertionError('should not delete')))
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'delete_torrent_matches': True,
                              'protect_seeding': True})
        data = r.get_json()
        assert r.status_code == 200, data
        assert data['completed'] == ["Kept 1 torrent still under Cleanuparr's minimum seed time"]

    def test_unreadable_seed_rules_fail_safe(self, app, client, monkeypatch):
        _configure_cleanuparr(app)
        matches = [{'id': 'young', 'name': 'a', 'seeding_seconds': 60}]
        title_id = self._movie_with_matches(app, monkeypatch, matches)
        monkeypatch.setattr(app_module, '_cleanuparr_seed_protection',
                            lambda: _seed_result(False, 0, 'Could not read seed-time rules from Cleanuparr'))
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'delete_torrent_matches': True,
                              'protect_seeding': True})
        assert r.status_code == 400
        assert 'seed-time rules' in r.get_json()['error']

    def test_without_protection_all_matches_deleted(self, app, client, monkeypatch):
        matches = [{'id': 'young', 'name': 'a', 'seeding_seconds': 60}]
        title_id = self._movie_with_matches(app, monkeypatch, matches)
        deleted = []
        monkeypatch.setattr(app_module, '_delete_torrent_client_entries',
                            lambda ids, delete_files=True: deleted.extend(ids))
        r = client.post(f'/api/remove/{title_id}',
                        json={'confirmed': True, 'delete_torrent_matches': True})
        assert r.status_code == 200
        assert deleted == ['young']

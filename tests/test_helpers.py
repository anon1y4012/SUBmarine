import app as app_module


class TestNormalizeUrl:
    def test_adds_scheme(self):
        assert app_module._normalize_url('example.com:7878') == 'http://example.com:7878'

    def test_keeps_https(self):
        assert app_module._normalize_url('https://example.com/') == 'https://example.com'

    def test_rewrites_localhost_for_docker(self):
        assert app_module._normalize_url('http://localhost:7878') == 'http://host.docker.internal:7878'
        assert app_module._normalize_url('127.0.0.1:8080') == 'http://host.docker.internal:8080'

    def test_empty(self):
        assert app_module._normalize_url('') == ''
        assert app_module._normalize_url(None) == ''


class TestBoundedWorkers:
    def test_within_range(self):
        assert app_module._bounded_workers('8') == 8

    def test_clamps(self):
        assert app_module._bounded_workers('999') == 32
        assert app_module._bounded_workers('0') == 1
        assert app_module._bounded_workers('-3') == 1

    def test_fallback_on_garbage(self):
        assert app_module._bounded_workers('lots', fallback=5) == 5
        assert app_module._bounded_workers(None, fallback=5) == 5


class TestLogLevel:
    def test_normalizes_case(self):
        assert app_module._normalize_log_level('debug') == 'DEBUG'

    def test_fallback(self):
        assert app_module._normalize_log_level('nonsense', 'INFO') == 'INFO'
        assert app_module._normalize_log_level(None, 'WARNING') == 'WARNING'


class TestPlexTokenStripping:
    def test_strips_token_from_query(self):
        url = 'http://plex:32400/library/thumb/1?X-Plex-Token=secret&w=100'
        cleaned = app_module._without_plex_token(url)
        assert 'secret' not in cleaned
        assert 'w=100' in cleaned

    def test_leaves_other_params(self):
        url = 'http://plex:32400/thumb?a=1&b=2'
        assert app_module._without_plex_token(url) == url


class TestProviderNormalization:
    def _payload(self, *names):
        return {'results': {'US': {'flatrate': [{'provider_name': n} for n in names]}}}

    def test_alias_mapping(self):
        matched = app_module._matched_tmdb_providers(self._payload('HBO Max', 'Paramount+'))
        assert matched == ['Max', 'Paramount Plus']

    def test_unknown_providers_dropped(self):
        matched = app_module._matched_tmdb_providers(self._payload('Netflix', 'Totally Fake TV'))
        assert matched == ['Netflix']

    def test_empty_payload(self):
        assert app_module._matched_tmdb_providers({}) == []


class TestTorrentMatching:
    def test_match_text_normalizes(self):
        assert app_module._match_text('The.Movie:2020!') == 'the movie 2020'

    def test_entry_matches_title_and_year(self):
        terms = app_module._torrent_match_terms('The Example', '2020')
        entry = {'name': 'The.Example.2020.1080p.WEB', 'save_path': '', 'content_path': '',
                 'category': '', 'tags': ''}
        assert app_module._torrent_entry_matches(entry, terms, '2020')

    def test_entry_rejects_other_title(self):
        terms = app_module._torrent_match_terms('The Example', '2020')
        entry = {'name': 'Different.Film.2020.1080p', 'save_path': '', 'content_path': '',
                 'category': '', 'tags': ''}
        assert not app_module._torrent_entry_matches(entry, terms, '2020')

    def test_entry_requires_year_for_short_titles(self):
        terms = app_module._torrent_match_terms('It', '2017')
        entry = {'name': 'It.1990.miniseries', 'save_path': '', 'content_path': '',
                 'category': '', 'tags': ''}
        assert not app_module._torrent_entry_matches(entry, terms, '2017')


class TestTransmissionRpcUrl:
    def test_appends_rpc_path(self):
        assert app_module._transmission_rpc_url('http://example.com:9091').endswith('/transmission/rpc')

    def test_keeps_existing_rpc_path(self):
        url = app_module._transmission_rpc_url('http://example.com:9091/transmission/rpc')
        assert url.count('/rpc') == 1


class TestThumbProxyUrl:
    def test_stable_version_hash(self):
        a = app_module._thumb_proxy_url(1, 'http://plex:32400/thumb/1')
        b = app_module._thumb_proxy_url(1, 'http://plex:32400/thumb/1')
        assert a == b
        assert a.startswith('/api/thumb/1?v=')

    def test_version_ignores_plex_token(self):
        a = app_module._thumb_proxy_url(1, 'http://plex:32400/thumb/1?X-Plex-Token=abc')
        b = app_module._thumb_proxy_url(1, 'http://plex:32400/thumb/1?X-Plex-Token=xyz')
        assert a == b

    def test_version_changes_with_source(self):
        a = app_module._thumb_proxy_url(1, 'http://plex:32400/thumb/1')
        b = app_module._thumb_proxy_url(1, 'http://plex:32400/thumb/2')
        assert a != b


class TestPlexItemSignature:
    def test_signature_stable(self):
        item = {'title': 'A', 'year': '2020', 'thumb': 'http://p/t/1', 'plex_seasons': {2, 1}}
        assert app_module._plex_item_signature(item, 'tv') == app_module._plex_item_signature(item, 'tv')

    def test_signature_changes_on_seasons(self):
        base = {'title': 'A', 'year': '2020', 'thumb': ''}
        one = app_module._plex_item_signature({**base, 'plex_seasons': {1}}, 'tv')
        two = app_module._plex_item_signature({**base, 'plex_seasons': {1, 2}}, 'tv')
        assert one != two


class TestTvCoverage:
    def test_full_and_partial_split(self, monkeypatch):
        by_season = {1: ['Netflix', 'Hulu'], 2: ['Netflix']}
        monkeypatch.setattr(app_module, 'tmdb_tv_season_providers',
                            lambda tmdb_id, season: by_season[season])
        full, partial = app_module.tmdb_tv_coverage('42', {1, 2})
        assert full == ['Netflix']
        assert partial == {'Hulu': [1]}

    def test_falls_back_on_season_error(self, monkeypatch):
        monkeypatch.setattr(app_module, 'tmdb_tv_season_providers', lambda tmdb_id, season: None)
        monkeypatch.setattr(app_module, 'tmdb_providers', lambda tmdb_id, mtype: ['Netflix'])
        full, partial = app_module.tmdb_tv_coverage('42', {1, 2})
        assert full == ['Netflix']
        assert partial == {}

    def test_no_seasons_uses_title_level(self, monkeypatch):
        monkeypatch.setattr(app_module, 'tmdb_providers', lambda tmdb_id, mtype: ['Max'])
        full, partial = app_module.tmdb_tv_coverage('42', set())
        assert full == ['Max']
        assert partial == {}


class TestProcessTitle:
    def test_uses_existing_tmdb_id(self, monkeypatch):
        monkeypatch.setattr(app_module, 'tmdb_providers', lambda tmdb_id, mtype: ['Netflix'])
        result = app_module.process_title({'key': 'k1', 'title': 'X'}, 'movie', '99')
        assert result == {'key': 'k1', 'tmdb_id': '99', 'providers': ['Netflix'],
                          'partial_providers': {}}

    def test_search_miss_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app_module, 'tmdb_search', lambda title, mtype: None)
        result = app_module.process_title({'key': 'k1', 'title': 'X'}, 'movie', None)
        assert result['tmdb_id'] is None
        assert result['providers'] == []

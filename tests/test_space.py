from conftest import insert_title

import app as app_module


class TestIntOrZero:
    def test_parses_int(self):
        assert app_module._int_or_zero('42') == 42
        assert app_module._int_or_zero(7) == 7

    def test_garbage_and_negative(self):
        assert app_module._int_or_zero('lots') == 0
        assert app_module._int_or_zero(None) == 0
        assert app_module._int_or_zero(-5) == 0


class TestGroupFileRecords:
    def test_hardlinks_counted_once(self):
        """Same byte size across sources = one physical file (hardlink/copy)."""
        files, total = app_module._group_file_records([
            {'path': '/movies/Example (2020)/Example.mkv', 'bytes': 1000, 'source': 'radarr'},
            {'path': '/downloads/Example.2020.1080p/Example.mkv', 'bytes': 1000, 'source': 'torrent'},
            {'path': '/movies/Example (2020)/Example.mkv', 'bytes': 1000, 'source': 'plex'},
        ])
        assert total == 1000
        assert len(files) == 1
        paths = {p['path']: p['sources'] for p in files[0]['paths']}
        assert paths['/movies/Example (2020)/Example.mkv'] == ['plex', 'radarr']
        assert paths['/downloads/Example.2020.1080p/Example.mkv'] == ['torrent']

    def test_distinct_copies_counted_separately(self):
        """A 1080p and a 4K copy differ in size and both count."""
        files, total = app_module._group_file_records([
            {'path': '/movies/a/Example.1080p.mkv', 'bytes': 1000, 'source': 'plex'},
            {'path': '/movies/a/Example.2160p.mkv', 'bytes': 5000, 'source': 'plex'},
        ])
        assert total == 6000
        assert [f['bytes'] for f in files] == [5000, 1000]  # largest first

    def test_unknown_sizes_grouped_by_path(self):
        files, total = app_module._group_file_records([
            {'path': '/x/a.mkv', 'bytes': 0, 'source': 'plex'},
            {'path': '/X/A.mkv', 'bytes': 0, 'source': 'radarr'},
            {'path': '/x/b.mkv', 'bytes': 0, 'source': 'plex'},
        ])
        assert total == 0
        assert len(files) == 2

    def test_empty_records_skipped(self):
        files, total = app_module._group_file_records([
            {'path': '', 'bytes': 0, 'source': 'plex'},
        ])
        assert files == []
        assert total == 0


class TestTorrentPathMatch:
    def test_content_path_inside_library_folder(self):
        entry = {'content_path': '/movies/Example (2020)/Example.2020.mkv'}
        assert app_module._torrent_path_match(entry, ('/movies/Example (2020)',))

    def test_library_file_inside_torrent_folder(self):
        entry = {'content_path': '/data/Example.2020.1080p'}
        assert app_module._torrent_path_match(
            entry, ('/data/Example.2020.1080p/Example.mkv',))

    def test_unrelated_paths(self):
        entry = {'content_path': '/downloads/Other.Film.2019'}
        assert not app_module._torrent_path_match(entry, ('/movies/Example (2020)',))

    def test_no_content_path(self):
        assert not app_module._torrent_path_match({'content_path': ''}, ('/movies/x',))


class TestTorrentMatchingSafety:
    def _entry(self, name):
        return {'name': name, 'save_path': '', 'content_path': '', 'category': '', 'tags': ''}

    def test_rejects_remake_with_conflicting_year(self):
        """A remake that names a different year must never match, even for long titles."""
        terms = app_module._torrent_match_terms('The Thing About Harry Situations', '1982')
        entry = self._entry('The.Thing.About.Harry.Situations.2011.1080p.BluRay')
        assert not app_module._torrent_entry_matches(entry, terms, '1982')

    def test_accepts_matching_year(self):
        terms = app_module._torrent_match_terms('The Example', '2020')
        entry = self._entry('The.Example.2020.2160p.WEB')
        assert app_module._torrent_entry_matches(entry, terms, '2020')

    def test_resolution_not_mistaken_for_year(self):
        """2160p must not count as a year token when checking for conflicts."""
        terms = app_module._torrent_match_terms('The Example', '2020')
        entry = self._entry('The.Example.2020.2160p')
        assert app_module._torrent_entry_matches(entry, terms, '2020')

    def test_path_verified_match_overrides_name(self):
        entry = {'name': 'abc123-obfuscated', 'save_path': '/downloads',
                 'content_path': '/movies/Example (2020)/Example.mkv',
                 'category': '', 'tags': ''}
        terms = app_module._torrent_match_terms('The Example', '2020')
        assert app_module._torrent_entry_matches(
            entry, terms, '2020', arr_paths=('/movies/Example (2020)',))


class TestSpaceEndpoint:
    def _stub_sources(self, monkeypatch, plex=None, radarr=None, sonarr=None, torrent=None):
        monkeypatch.setattr(app_module, '_plex_file_records',
                            lambda title: plex or {'configured': False, 'records': [], 'error': ''})
        monkeypatch.setattr(app_module, '_radarr_file_records',
                            lambda title: radarr or {'configured': False, 'records': [], 'movie': None, 'error': ''})
        monkeypatch.setattr(app_module, '_sonarr_file_records',
                            lambda title: sonarr or {'configured': False, 'records': [], 'series': None, 'error': ''})
        monkeypatch.setattr(app_module, '_torrent_file_records',
                            lambda title, radarr_movie=None: torrent or {'configured': False, 'records': [], 'matches': [], 'error': ''})

    def test_unknown_title_404(self, client):
        assert client.get('/api/space/999').status_code == 404

    def test_movie_space_dedupes_hardlinks(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Example', rating_key='10', tmdb_id='55')
        self._stub_sources(
            monkeypatch,
            plex={'configured': True, 'records': [
                {'path': '/movies/Example/Example.mkv', 'bytes': 4000, 'source': 'plex'},
            ], 'error': ''},
            radarr={'configured': True, 'records': [
                {'path': '/movies/Example/Example.mkv', 'bytes': 4000, 'source': 'radarr'},
            ], 'movie': {'id': 1, 'title': 'Example', 'path': '/movies/Example', 'movie_file_path': ''}, 'error': ''},
            torrent={'configured': True, 'records': [
                {'path': '/downloads/Example.2020/Example.mkv', 'bytes': 4000, 'source': 'torrent'},
                {'path': '/downloads/Example.2020.2160p/Example.mkv', 'bytes': 9000, 'source': 'torrent'},
            ], 'matches': [{'id': 'h1', 'name': 'Example.2020', 'client': 'qBittorrent'}], 'error': ''},
        )
        data = client.get(f'/api/space/{title_id}').get_json()
        assert data['ok'] is True
        assert data['total_bytes'] == 13000
        assert len(data['files']) == 2
        assert 'radarr' in data['sources']
        assert 'sonarr' not in data['sources']
        assert data['torrent_matches'][0]['name'] == 'Example.2020'

    def test_tv_space_uses_sonarr(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Show', rating_key='11',
                                media_type='tv', tmdb_id='77')
        self._stub_sources(
            monkeypatch,
            sonarr={'configured': True, 'records': [
                {'path': '/tv/Show/S01E01.mkv', 'bytes': 700, 'source': 'sonarr'},
                {'path': '/tv/Show/S01E02.mkv', 'bytes': 800, 'source': 'sonarr'},
            ], 'series': {'id': 3, 'title': 'Show', 'path': '/tv/Show'}, 'error': ''},
        )
        data = client.get(f'/api/space/{title_id}').get_json()
        assert data['total_bytes'] == 1500
        assert 'sonarr' in data['sources']
        assert 'radarr' not in data['sources']

    def test_source_errors_reported(self, app, client, monkeypatch):
        title_id = insert_title(app, title='Example', rating_key='12', tmdb_id='55')
        self._stub_sources(
            monkeypatch,
            radarr={'configured': True, 'records': [], 'movie': None,
                    'error': 'Not tracked in Radarr'},
        )
        data = client.get(f'/api/space/{title_id}').get_json()
        assert data['ok'] is True
        assert data['total_bytes'] == 0
        assert data['sources']['radarr']['error'] == 'Not tracked in Radarr'


class TestTorrentListCache:
    def test_cache_hits_within_ttl(self, monkeypatch):
        app_module._invalidate_torrent_cache()
        calls = []
        monkeypatch.setattr(app_module, '_list_torrent_client_entries',
                            lambda: calls.append(1) or [{'id': 'a'}])
        first = app_module._cached_torrent_entries()
        second = app_module._cached_torrent_entries()
        assert first == second == [{'id': 'a'}]
        assert len(calls) == 1

    def test_invalidated_on_delete(self, monkeypatch):
        app_module._invalidate_torrent_cache()
        calls = []
        monkeypatch.setattr(app_module, '_list_torrent_client_entries',
                            lambda: calls.append(1) or [])
        app_module._cached_torrent_entries()
        monkeypatch.setattr(app_module, '_torrent_client_type', lambda value=None: 'qbittorrent')
        monkeypatch.setattr(app_module, '_qbittorrent_delete_torrents', lambda ids, delete_files: None)
        app_module._delete_torrent_client_entries(['a'])
        app_module._cached_torrent_entries()
        assert len(calls) == 2

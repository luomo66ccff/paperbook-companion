from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Event

import pytest

import story_tts_server as server


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    monkeypatch.setattr(server, 'TARGET_DIR', str(root))
    monkeypatch.setattr(server, 'BOOKSHELF_FILE', str(root / 'bookshelf.json'))
    monkeypatch.setattr(server.app_config, 'HOST', '127.0.0.1')
    monkeypatch.setattr(server.app_config, 'API_TOKEN', '')
    monkeypatch.setattr(server.app_config, 'CORS_ALLOW_ORIGINS', ())
    return server.app.test_client(), root


@pytest.mark.parametrize('headers,allowed_origin', [
    ({}, None),
    ({'Referer': 'http://127.0.0.1:5100/tts-story-book/tts_v2.html'}, None),
    ({'Origin': 'http://127.0.0.1:5100'}, 'http://127.0.0.1:5100'),
    ({'Origin': 'http://localhost:5100'}, 'http://localhost:5100'),
    ({'Origin': 'http://[::1]:5100'}, 'http://[::1]:5100'),
    ({'Origin': 'https://reader.example.com'}, 'https://reader.example.com'),
])
def test_allowed_origins_and_originless_clients(client, monkeypatch, headers, allowed_origin):
    http, root = client
    monkeypatch.setattr(server.app_config, 'CORS_ALLOW_ORIGINS', ('https://reader.example.com',))
    (root / 'output_dir' / 'session').mkdir(parents=True)
    response = http.post('/tts-story-book/api/bookshelf', base_url='http://127.0.0.1:5100',
                         headers=headers, json={'book_name': 'Book', 'session_dir': 'output_dir/session'})
    assert response.status_code == 200
    assert response.headers.get('Access-Control-Allow-Origin') == allowed_origin
    assert 'Origin' in response.vary


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://unlisted.example'},
    {'Origin': 'http://localhost:5101'},
    {'Origin': 'null', 'Referer': 'http://127.0.0.1:5100/tts_v2.html'},
    {'Origin': 'http://localhost:5100/path'},
    {'Referer': 'https://unlisted.example/page'},
])
def test_unlisted_origin_writes_are_blocked(client, headers):
    http, root = client
    response = http.post('/api/bookshelf', base_url='http://127.0.0.1:5100',
                         headers=headers, json={'book_name': 'Blocked', 'session_dir': 'session'})
    assert response.status_code == 403
    assert response.json['success'] is False
    assert 'Access-Control-Allow-Origin' not in response.headers
    assert not (root / 'bookshelf.json').exists()
    read = http.get('/api/bookshelf', base_url='http://127.0.0.1:5100', headers=headers)
    assert 'Access-Control-Allow-Origin' not in read.headers


def test_tokenless_api_rejects_untrusted_host(client, monkeypatch):
    http, _ = client
    origin = 'http://reader.example.com:5100'
    url = '/tts-story-book/api/bookshelf'
    assert http.get(url, base_url=origin).status_code == 403
    assert http.get(url, base_url=origin, headers={'Origin': origin}).status_code == 403
    monkeypatch.setattr(server.app_config, 'CORS_ALLOW_ORIGINS', (origin,))
    assert http.get(url, base_url=origin).status_code == 200


@pytest.mark.parametrize('prefix', ['', '/tts-story-book'])
@pytest.mark.parametrize('method', ['GET', 'POST', 'OPTIONS'])
def test_tokenless_api_rejects_non_loopback_peer(client, monkeypatch, prefix, method):
    http, root = client
    origin = 'http://localhost'
    monkeypatch.setattr(server.app_config, 'CORS_ALLOW_ORIGINS', (origin,))
    response = http.open(prefix + '/api/bookshelf', method=method, base_url=origin,
                         environ_overrides={'REMOTE_ADDR': '192.0.2.20'},
                         headers={'Origin': origin, 'X-Forwarded-For': '127.0.0.1',
                                  'Forwarded': 'for=127.0.0.1', 'Access-Control-Request-Method': 'POST'})
    assert response.status_code == 403
    assert '回环' in response.json['error']
    assert not (root / 'bookshelf.json').exists()


@pytest.mark.parametrize('peer,status', [
    ('127.0.0.2', 200), ('::1', 200), ('::ffff:127.0.0.1', 200),
    ('::ffff:192.0.2.20', 403), (None, 403), ('localhost', 403),
])
def test_tokenless_loopback_uses_only_peer_address(client, peer, status):
    http, _ = client
    response = http.get('/api/bookshelf', environ_overrides={'REMOTE_ADDR': peer},
                        headers={'X-Forwarded-For': '192.0.2.20'})
    assert response.status_code == status


@pytest.mark.parametrize('args', [[], ['--open-quick']])
def test_non_loopback_without_token_cannot_start(tmp_path, args):
    env = dict(os.environ, HOST='0.0.0.0', API_TOKEN=' ', TARGET_DIR=str(tmp_path), PYTHONUTF8='1')
    script = Path(server.__file__).resolve()
    result = subprocess.run([sys.executable, str(script), *args], env=env,
                            capture_output=True, encoding='utf-8', timeout=20)
    assert result.returncode != 0
    assert '拒绝启动' in result.stderr and 'API_TOKEN' in result.stderr
    assert '纸书智伴工作台' not in result.stdout


@pytest.mark.parametrize('prefix', ['', '/tts-story-book'])
def test_token_protects_api_and_allows_cors_preflight(client, monkeypatch, prefix):
    http, _ = client
    monkeypatch.setattr(server.app_config, 'HOST', '0.0.0.0')
    monkeypatch.setattr(server.app_config, 'API_TOKEN', 'test-only-token')
    monkeypatch.setattr(server.app_config, 'CORS_ALLOW_ORIGINS', ('https://reader.example.com',))
    url = prefix + '/api/bookshelf'
    assert http.get(url).status_code == 401
    assert http.get(url, headers={'Authorization': 'Bearer incorrect'}).status_code == 401
    assert http.get(url, headers={'Authorization': 'Bearer test-only-token'}).status_code == 200
    assert http.get(url, headers={'X-API-Token': 'test-only-token'}).status_code == 200
    assert http.get(url, headers={'Authorization': 'Bearer test-only-token'},
                    environ_overrides={'REMOTE_ADDR': '192.0.2.20'}).status_code == 200
    assert http.get(prefix + '/health').status_code == 200
    assert http.get(prefix + '/tts_v2.html').status_code == 200
    assert http.options(url).status_code == 401
    headers = {'Origin': 'https://reader.example.com', 'Access-Control-Request-Method': 'POST',
               'Access-Control-Request-Headers': 'authorization, content-type'}
    preflight = http.options(url, headers=headers)
    assert preflight.status_code == 200
    assert preflight.headers['Access-Control-Allow-Origin'] == headers['Origin']
    assert preflight.headers['Access-Control-Allow-Methods'] == 'POST'
    assert 'Authorization' in preflight.headers['Access-Control-Allow-Headers']
    assert http.post(url, headers={'Origin': headers['Origin']}, json={}).status_code == 401
    assert http.options(url, headers=dict(headers, Origin='https://unlisted.example')).status_code == 401


def test_cors_preflight_is_independent_of_endpoint_capabilities(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(server, 'get_capabilities', lambda: {'vision': False})
    url = '/api/analyze-image'
    headers = {'Origin': 'http://localhost'}
    preflight = http.options(url, headers={**headers, 'Access-Control-Request-Method': 'POST'})
    assert preflight.status_code == 200
    assert preflight.headers['Access-Control-Allow-Methods'] == 'POST'
    response = http.post(url, headers=headers, json={})
    assert response.status_code == 503
    assert response.headers['Access-Control-Allow-Origin'] == headers['Origin']


@pytest.mark.parametrize('route,relative_path', [
    ('', r'..\data_evil\book'),
    ('', None),
    ('/load', r'bookshelf\..\bookshelf_evil\book'),
    ('/load', 'bookshelf_evil/book'),
    ('/load', None),
    ('/remove', r'bookshelf\..\bookshelf_evil\book'),
    ('/remove', 'bookshelf_evil/book'),
    ('/remove', 'bookshelf'),
    ('/remove', None),
])
def test_bookshelf_rejects_paths_outside_its_root(client, monkeypatch, route, relative_path):
    http, root = client
    sibling = root.with_name('data_evil') / 'book'
    for directory in (sibling, root / 'bookshelf' / 'valid', root / 'bookshelf_evil' / 'book'):
        directory.mkdir(parents=True)
        (directory / 'keep.txt').write_text('keep', encoding='utf-8')

    supplied = relative_path if relative_path is not None else str(sibling)
    # Unknown invalid paths are rejected. Registered legacy values use the
    # index-only removal contract tested below.
    shelf = [{'session_dir': 'bookshelf/valid' if route == '/remove' else supplied, 'name': 'Existing'}]
    index = root / 'bookshelf.json'
    index.write_text(json.dumps(shelf), encoding='utf-8')
    resolve = Path.resolve

    def resolve_inside_data(path, *args, **kwargs):
        assert path.is_relative_to(root), 'Outside paths must be rejected before filesystem resolution'
        return resolve(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, 'resolve', resolve_inside_data)
        response = http.post('/tts-story-book/api/bookshelf' + route,
                             json={'book_name': 'Blocked', 'session_dir': supplied})
    assert response.status_code in {400, 403}
    assert response.json['success'] is False
    assert json.loads(index.read_text(encoding='utf-8')) == shelf
    assert sorted(p.name for p in (root / 'bookshelf').iterdir()) == ['valid']
    for directory in (sibling, root / 'bookshelf' / 'valid', root / 'bookshelf_evil' / 'book'):
        assert (directory / 'keep.txt').read_text(encoding='utf-8') == 'keep'


def test_add_load_and_remove_preserve_windows_path_compatibility(client):
    http, root = client
    source = root / 'output_dir' / 'session' / 'story'
    source.mkdir(parents=True)
    (source / 'story.txt').write_text('A short story.', encoding='utf-8')
    added = http.post('/api/bookshelf', json={'book_name': 'Book', 'session_dir': r'output_dir\session'})
    assert added.status_code == 200
    book = added.json['book']
    assert book['session_dir'] == 'bookshelf/Book'

    # Old Windows records could store an absolute path with mixed separators.
    book['session_dir'] = str(root) + '/bookshelf\\Book'
    (root / 'bookshelf.json').write_text(json.dumps([book]), encoding='utf-8')
    loaded = http.post('/api/bookshelf/load', json={'session_dir': book['session_dir']})
    assert loaded.status_code == 200
    assert loaded.json['story_text'] == 'A short story.'

    removed = http.post('/api/bookshelf/remove', json={'session_dir': r'bookshelf\Book'})
    assert removed.status_code == 200
    assert http.get('/api/bookshelf').json['bookshelf'] == []
    assert not (root / 'bookshelf' / 'Book').exists()
    assert (source / 'story.txt').is_file()


def test_bookshelf_remove_does_not_delete_linked_record_files(client):
    http, root = client
    real_book = root / 'bookshelf' / 'Real'
    real_book.mkdir(parents=True)
    linked_book = real_book.with_name('Linked')
    try:
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(real_book), str(linked_book))
        else:
            linked_book.symlink_to(real_book, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Cannot create a directory link in this environment: {exc}')

    shelf = [{'session_dir': 'bookshelf/Linked'}, {'session_dir': 'bookshelf/Real'}]
    (root / 'bookshelf.json').write_text(json.dumps(shelf), encoding='utf-8')
    removed = http.post('/api/bookshelf/remove', json={'session_dir': 'bookshelf/Linked'})
    assert removed.status_code == 200
    assert removed.json['success'] is True
    assert removed.json['warning']
    assert linked_book.is_dir()
    assert real_book.is_dir()
    assert http.get('/api/bookshelf').json['bookshelf'] == shelf[1:]


@pytest.mark.parametrize('delete_files', [False, True])
@pytest.mark.parametrize('kind', ['outside', 'missing', 'legacy_separator'])
def test_remove_stale_legacy_record_without_deleting_files(client, monkeypatch, delete_files, kind):
    http, root = client
    preserved = root / 'output_dir' / 'old_session'
    preserved.mkdir(parents=True)
    marker = preserved / 'keep.txt'
    marker.write_text('keep', encoding='utf-8')
    stored = {
        'outside': str(preserved),
        'missing': 'bookshelf/missing',
        'legacy_separator': r'bookshelf\..\output_dir\old_session',
    }[kind]
    keep = {'session_dir': 'bookshelf/other'}
    index = root / 'bookshelf.json'
    index.write_text(json.dumps([{'session_dir': stored}, keep]), encoding='utf-8')

    if not delete_files:
        def reject_path_access(*args, **kwargs):
            pytest.fail('Index-only removal must skip filesystem path validation')
        monkeypatch.setattr(server, '_bookshelf_path', reject_path_access)

    removed = http.post('/api/bookshelf/remove', json={'session_dir': stored, 'delete_files': delete_files})
    assert removed.status_code == 200
    assert removed.json['success'] is True
    assert bool(removed.json.get('warning')) is delete_files
    assert json.loads(index.read_text(encoding='utf-8')) == [keep]
    assert marker.read_text(encoding='utf-8') == 'keep'


def test_concurrent_bookshelf_adds_keep_both_records(client, monkeypatch):
    http, root = client
    (root / 'output_dir' / 'session').mkdir(parents=True)
    pending_save = Event()
    second_started = Event()
    second_read = Event()
    finish_save = Event()
    original_load = server.load_bookshelf
    original_save = server.save_bookshelf

    def observe_read():
        if server.request.get_json()['book_name'] == 'Second':
            second_read.set()
        return original_load()

    def pause_first_save(bookshelf):
        if bookshelf[-1]['name'] == 'First':
            pending_save.set()
            assert finish_save.wait(timeout=5)
        original_save(bookshelf)

    def add(name):
        if name == 'Second':
            second_started.set()
        with server.app.test_client() as worker:
            return worker.post('/api/bookshelf', json={'book_name': name, 'session_dir': 'output_dir/session'})

    with monkeypatch.context() as patch:
        patch.setattr(server, 'load_bookshelf', observe_read)
        patch.setattr(server, 'save_bookshelf', pause_first_save)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(add, 'First')
            try:
                assert pending_save.wait(timeout=5)
                second = pool.submit(add, 'Second')
                assert second_started.wait(timeout=5)
                assert not second_read.wait(timeout=0.2)
            finally:
                finish_save.set()
            assert first.result(timeout=5).status_code == 200
            assert second.result(timeout=5).status_code == 200
    assert [book['name'] for book in http.get('/api/bookshelf').json['bookshelf']] == ['First', 'Second']
    assert {book.name for book in (root / 'bookshelf').iterdir()} == {'First', 'Second'}


@pytest.fixture
def existing_book(client):
    http, root = client
    directory = root / 'bookshelf' / 'Book'
    directory.mkdir(parents=True)
    (directory / 'keep.txt').write_text('keep', encoding='utf-8')
    index = root / 'bookshelf.json'
    shelf = [{'session_dir': 'bookshelf/Book'}]
    index.write_text(json.dumps(shelf), encoding='utf-8')
    return http, directory, index, shelf


def test_remove_preserves_files_on_index_replace_failure(existing_book, monkeypatch):
    http, directory, index, shelf = existing_book

    def fail_replace(temporary, destination):
        assert temporary.parent == destination.parent == index.parent
        assert temporary != destination == index
        assert json.loads(temporary.read_text(encoding='utf-8')) == []
        assert json.loads(index.read_text(encoding='utf-8')) == shelf
        raise OSError('test index replace failure')

    monkeypatch.setattr(server.os, 'replace', fail_replace)
    removed = http.post('/api/bookshelf/remove', json=shelf[0])
    assert removed.status_code == 500
    assert removed.json['success'] is False
    assert json.loads(index.read_text(encoding='utf-8')) == shelf
    assert (directory / 'keep.txt').read_text(encoding='utf-8') == 'keep'
    assert list(index.parent.glob('.bookshelf.json.*.tmp')) == []


def test_remove_reports_file_error_after_committing_index(existing_book, monkeypatch):
    http, directory, index, shelf = existing_book

    def fail_delete(path):
        assert path == directory.resolve()
        assert json.loads(index.read_text(encoding='utf-8')) == []
        raise OSError('test directory deletion failure')

    monkeypatch.setattr(server.shutil, 'rmtree', fail_delete)
    removed = http.post('/api/bookshelf/remove', json=shelf[0])
    assert removed.status_code == 500
    assert removed.json['success'] is False
    assert removed.json['index_removed'] is True
    assert '文件删除失败' in removed.json['error']
    assert json.loads(index.read_text(encoding='utf-8')) == []
    assert (directory / 'keep.txt').exists()


@pytest.mark.parametrize('failure', ['copy', 'save', 'corrupt_index'])
def test_add_does_not_leave_an_unregistered_copy(existing_book, monkeypatch, failure):
    http, directory, index, _ = existing_book
    source = index.parent / 'output_dir' / 'session'
    source.mkdir(parents=True)

    def fail_copy(source, destination, **kwargs):
        destination.mkdir()
        (destination / 'partial.txt').touch()
        raise OSError('test copy interruption')

    def fail_save(bookshelf):
        raise OSError('test index save failure')

    if failure == 'copy':
        monkeypatch.setattr(server.shutil, 'copytree', fail_copy)
    elif failure == 'save':
        monkeypatch.setattr(server, 'save_bookshelf', fail_save)
    else:
        index.write_text('[{', encoding='utf-8')
    original_index = index.read_bytes()
    added = http.post('/api/bookshelf', json={'book_name': 'New book', 'session_dir': 'output_dir/session'})
    assert added.status_code == 500
    assert index.read_bytes() == original_index
    assert list(directory.parent.iterdir()) == [directory]


@pytest.mark.parametrize('route', ['/api/bookshelf', '/api/bookshelf/load'])
def test_bookshelf_rechecks_file_resolution_at_use(existing_book, monkeypatch, route):
    http, directory, index, shelf = existing_book
    story = directory / 'story' / 'story.txt'
    story.parent.mkdir()
    story.write_text('Expected local story.', encoding='utf-8')
    check_contents = server._check_bookshelf_contents
    realpath = server.os.path.realpath

    def changed_resolution(path, *args, **kwargs):
        if Path(path) == story:
            return str(directory / 'unexpected.txt')
        return realpath(path, *args, **kwargs)

    def observe_path_change(path):
        check_contents(path)
        monkeypatch.setattr(server.os.path, 'realpath', changed_resolution)

    monkeypatch.setattr(server, '_check_bookshelf_contents', observe_path_change)
    response = http.post(route, json={**shelf[0], 'book_name': 'New book'})
    assert response.status_code == 400
    assert response.json['success'] is False
    assert json.loads(index.read_text(encoding='utf-8')) == shelf
    assert list(directory.parent.iterdir()) == [directory]


@pytest.mark.parametrize('legacy', [False, True])
def test_removing_stale_record_cannot_remove_replacement(client, monkeypatch, legacy):
    http, root = client
    source = root / 'output_dir' / 'session'
    source.mkdir(parents=True)
    (source / 'keep.txt').write_text('new book', encoding='utf-8')
    index = root / 'bookshelf.json'
    old = {'session_dir': r'bookshelf\Book', 'name': 'Book'}
    if not legacy:
        old['id'] = 1
    index.write_text(json.dumps([old]), encoding='utf-8')

    added = http.post('/api/bookshelf', json={'book_name': 'Book', 'session_dir': 'output_dir/session'})
    assert added.status_code == 200
    new = added.json['book']
    assert new['id'] and new['id'] != old.get('id')
    assert new['session_dir'] != 'bookshelf/Book'

    # Recreate two records produced by the previous implementation before the fix.
    directory = root / 'bookshelf' / 'Book'
    (root / new['session_dir']).rename(directory)
    new['session_dir'] = directory.as_posix()
    index.write_text(json.dumps([old, new]), encoding='utf-8')
    monkeypatch.setattr(server.shutil, 'rmtree', lambda path: pytest.fail(f'Unexpected delete: {path}'))
    ambiguous = http.post('/api/bookshelf/remove', json={'session_dir': 'bookshelf/Book'})
    assert ambiguous.status_code == 409
    assert http.get('/api/bookshelf').json['bookshelf'] == [old, new]
    stale_request = http.post('/api/bookshelf/remove', json={**old, 'id': -1})
    assert stale_request.status_code == 404

    removed = http.post('/api/bookshelf/remove', json=old)
    assert removed.status_code == 200 and removed.json['warning']
    assert http.get('/api/bookshelf').json['bookshelf'] == [new]
    assert (directory / 'keep.txt').read_text(encoding='utf-8') == 'new book'


def test_remove_respects_legacy_name_and_timestamp(client):
    http, root = client
    directory = root / 'bookshelf' / 'Book'
    directory.mkdir(parents=True)
    old = {'session_dir': 'bookshelf/Book', 'name': 'Book'}
    dated = {**old, 'created_at': '2026-11-24 10:00:00'}
    other = {**old, 'name': 'Other'}
    index = root / 'bookshelf.json'
    index.write_text(json.dumps([old, dated, other]), encoding='utf-8')

    assert http.post('/api/bookshelf/remove', json=old).json['warning']
    assert http.get('/api/bookshelf').json['bookshelf'] == [dated, other]
    assert http.post('/api/bookshelf/remove', json=dated).json['warning']
    assert http.get('/api/bookshelf').json['bookshelf'] == [other]
    assert directory.is_dir()


def test_bookshelf_lock_coordinates_separate_processes(client, monkeypatch, tmp_path):
    http, root = client
    (root / 'output_dir' / 'session').mkdir(parents=True)
    ready, release = tmp_path / 'first-saving', tmp_path / 'release'
    code = """
from pathlib import Path
import sys
import time
import story_tts_server as server

server.app_config.HOST = '127.0.0.1'
server.app_config.API_TOKEN = ''
ready, release = map(Path, sys.argv[1:])
save = server.save_bookshelf
def pause_save(bookshelf):
    ready.touch()
    deadline = time.monotonic() + 15
    while not release.exists():
        assert time.monotonic() < deadline
        time.sleep(.02)
    save(bookshelf)
server.save_bookshelf = pause_save
with server.app.test_client() as client:
    result = client.post('/api/bookshelf', json={'book_name': 'First', 'session_dir': 'output_dir/session'})
assert result.status_code == 200, result.get_json()
"""
    env = dict(os.environ, TARGET_DIR=str(root), PYTHONUTF8='1')
    child = subprocess.Popen([sys.executable, '-c', code, str(ready), str(release)],
                             cwd=Path(server.__file__).resolve().parent, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    payload = {'book_name': 'Second', 'session_dir': 'output_dir/session'}
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        with monkeypatch.context() as patch:
            patch.setattr(server, '_BOOKSHELF_LOCK_TIMEOUT', .15)
            assert http.get('/api/bookshelf').status_code == 503
            busy = http.post('/api/bookshelf', json=payload)
            assert busy.status_code == 503 and '书架文件锁超时' in busy.json['error']
            assert http.post('/api/bookshelf/remove', json={'session_dir': 'bookshelf/First'}).status_code == 503
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = Event()

            def add_second():
                with server.app.test_client() as worker:
                    started.set()
                    return worker.post('/api/bookshelf', json=payload)

            pending = pool.submit(add_second)
            assert started.wait(5)
            release.touch()
            assert pending.result(timeout=10).status_code == 200
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stdout + stderr
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()
    assert [book['name'] for book in http.get('/api/bookshelf').json['bookshelf']] == ['First', 'Second']


@pytest.mark.parametrize('running', [False, True])
@pytest.mark.parametrize('replacement', [False, True])
def test_deleted_book_cancels_queued_and_running_video(existing_book, monkeypatch, running, replacement):
    http, directory, index, shelf = existing_book
    video = directory / 'videos' / 'video_0.mp4'
    image = directory / 'page.png'
    image.write_bytes(b'image')
    entered = Event()
    proceed = Event()
    tasks = []

    class CapturedExecutor:
        def submit(self, fn, *args):
            tasks.append((fn, args))

    def generate(**kwargs):
        assert running, 'A deleted book started remote generation'
        output = Path(kwargs['output_path'])
        assert not output.is_relative_to(directory)
        entered.set()
        assert proceed.wait(5)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'x' * 2048)
        return output

    monkeypatch.setattr(server, 'video_executor', CapturedExecutor())
    monkeypatch.setattr(server, '_deleted_video_sessions', set())
    monkeypatch.setattr(server, '_video_inflight', set())
    monkeypatch.setattr(server, '_video_status', {})
    monkeypatch.setattr(server.video_generator, 'generate', generate)
    monkeypatch.setattr(server, 'probe_media', lambda path: {'has_video': True})
    assert server.enqueue_video_task('page', str(image), str(directory), 0) == 'queued'
    task, args = tasks.pop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(task, *args) if running else None
        try:
            if running:
                assert entered.wait(5)
            assert http.post('/api/bookshelf/remove', json=shelf[0]).status_code == 200
            assert not directory.exists()
            assert server._deleted_video_sessions
            if replacement:
                source = index.parent / 'output_dir' / 'session'
                source.mkdir(parents=True)
                (source / 'keep.txt').write_text('replacement', encoding='utf-8')
                new = http.post('/api/bookshelf', json={'book_name': 'Book', 'session_dir': 'output_dir/session'})
                assert new.status_code == 200
                assert new.json['book']['session_dir'] == 'bookshelf/Book'
                # A different service process would have no local tombstone.
                server._deleted_video_sessions.clear()
        finally:
            proceed.set()
        assert (pending.result(timeout=5) if running else task(*args)) is None
    assert directory.exists() is replacement
    assert not (directory / 'videos').exists()
    assert video not in server._video_status
    assert not server._video_inflight
    assert not list(index.parent.glob('.video-*'))
    if replacement:
        assert (directory / 'keep.txt').read_text(encoding='utf-8') == 'replacement'

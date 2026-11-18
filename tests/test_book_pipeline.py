import io
import json
from pathlib import Path
import shutil
import subprocess
import wave

import fitz
from flask import Flask
from PIL import Image
import pytest

from app.book_pipeline import BookPipelineService, register_book_routes
from app.pdf_book_reader import PDFBookReader


def pdf_bytes():
    document = fitz.open()
    document.new_page(width=300, height=100).insert_text((10, 30), 'Rabbit shares apples. Fox likes apples!', fontsize=8)
    document.new_page(width=150, height=100).draw_circle(fitz.Point(40, 40), 10)
    data = document.tobytes()
    document.close()
    return data


def wav(path, duration=.12):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\x01\x00' * round(24000 * duration))
    return str(path)


class LocalReader(PDFBookReader):
    seen = []

    def __init__(self):
        super().__init__(dpi=72)

    def ocr_page(self, path):
        return '[无文字页面]'

    def verify_ocr(self, path, text):
        return text, True

    def read_book(self, *args, **kwargs):
        self.seen.append((kwargs['mode'], kwargs['verify_ocr']))
        return super().read_book(*args, **kwargs)


@pytest.fixture
def pipeline(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('real FFmpeg required')
    clip = tmp_path / 'controlled.mp4'
    subprocess.run([shutil.which('ffmpeg'), '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=red:s=160x120:r=24:d=0.25', '-an', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(clip)], check=True, capture_output=True)
    calls = {'tts': 0, 'video': 0, 'answer': [], 'fail_tts': False, 'force': []}

    def synthesize(text, voice, output_path, language):
        calls['tts'] += 1
        if calls['fail_tts']:
            raise RuntimeError('controlled failure')
        return wav(output_path)

    def generate(image_path, prompt, output_path, options):
        calls['video'] += 1
        calls['force'].append(options['force'])
        shutil.copyfile(clip, output_path)
        return output_path

    def answer(question, context, history):
        calls['answer'].append((question, context, history))
        return 'The rabbit shares apples [p0001].'

    config = {'data_dir': str(tmp_path / 'books'), 'max_pages': 10, 'max_upload_bytes': 1000000,
              'default_video': {'width': 320, 'height': 180, 'fps': 24, 'blank_duration': .2},
              'cache_identity': {'tts_model': 'controlled-v1', 'video_model': 'controlled-v1'}}
    adapters = {'reader_factory': LocalReader, 'synthesize': synthesize, 'video_generate': generate,
                'answer': answer, 'capabilities': lambda: {'ocr': False, 'vision': False,
                                                          'tts': True, 'video': True, 'answer': True}}
    app = Flask(__name__)
    service = register_book_routes(app, config, adapters)
    yield app.test_client(), service, calls, config, adapters
    service.close()


def imported(client, service, mode='auto', verify='true'):
    response = client.post('/api/books/import', data={'file': (io.BytesIO(pdf_bytes()), 'example.pdf'),
                                                      'mode': mode, 'verify_ocr': verify})
    assert response.status_code == 202
    result = response.get_json()
    job = service.wait_for_job(result['job_id'])
    assert job['status'] == 'succeeded', job
    return result['book_id']


def media(client, service, book_id, **payload):
    response = client.post(f'/api/books/{book_id}/media', json={'voice': 'test', **payload})
    assert response.status_code == 202
    return service.wait_for_job(response.get_json()['job_id'])


def test_import_validates_actual_pdf_before_creating_a_job(tmp_path):
    app = Flask(__name__)
    service = register_book_routes(app, {'data_dir': str(tmp_path / 'books'), 'max_pages': 2},
                                   {'reader_factory': LocalReader})
    client = app.test_client()
    pdf = pdf_bytes()
    with fitz.open(stream=pdf) as document:
        locked_pdf = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='reader', owner_pw='owner')
    with io.BytesIO() as image:
        Image.new('RGB', (10, 10)).save(image, 'PNG')
        png = image.getvalue()

    def upload(data):
        return client.post('/api/books/import', data={'file': (io.BytesIO(data), 'example.pdf'), 'mode': 'fast'})

    try:
        for invalid, message in ((b'', '文件为空'), (b'%PDF-1.7\ncorrupt', '完整'),
                                 (png, '有效的 PDF'), (locked_pdf, '密码保护')):
            response = upload(invalid)
            assert response.status_code == 400
            assert message in response.get_json()['error']
        assert list(service.root.iterdir()) == [service.job_dir]
        assert not list(service.job_dir.iterdir())

        # MuPDF can recover a readable file with leading data beyond the
        # previous fixed-size header scan.
        response = upload(b'X' * 2048 + b'\n' + pdf)
        assert response.status_code == 202
        payload = response.get_json()
        assert service.wait_for_job(payload['job_id'])['status'] == 'succeeded'
        assert service.manifest(payload['book_id'])['page_count'] == 2
    finally:
        service.close()


def test_import_envelopes_modes_assets_and_stable_ids(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    manifest = client.get(f'/api/books/{book}').get_json()['book']
    assert manifest['schema_version'] == 1 and manifest['source']['kind'] == 'pdf'
    assert len(manifest['source']['pdf_sha256']) == 64
    assert len(manifest['pages']) == 2
    assert LocalReader.seen[-1] == ('auto', True)
    assert manifest['pages'][1]['page_id'] == 'p0002'
    assert manifest['pages'][1]['text_status'] == 'empty'
    assert client.get(manifest['pages'][0]['image_url']).status_code == 200
    assert client.get('/api/books').get_json()['books'][0]['book_id'] == book
    assert str(service.root) not in json.dumps(manifest)
    assert client.get(f'/api/books/{book}/assets/../manifest.json').status_code in (400, 404)
    assert client.get(f'/api/books/{book}/assets/source.pdf').status_code == 400
    assert client.get('/api/books/not-there').status_code == 404


def test_media_measures_sentences_cache_force_and_model_identity(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    assert media(client, service, book)['status'] == 'succeeded'
    first = service.manifest(book)
    tts = first['pages'][0]['media']['tts']
    assert len(tts['sentences']) == 2
    assert tts['duration'] == pytest.approx(.24)
    assert tts['sentences'][1]['start'] == pytest.approx(.12)
    assert first['pages'][1]['media']['tts']['status'] == 'silent'
    assert calls['tts'] == 2 and calls['video'] == 2
    assert media(client, service, book)['status'] == 'succeeded'
    assert calls['tts'] == 2 and calls['video'] == 2
    assert media(client, service, book, force=True)['status'] == 'succeeded'
    assert calls['tts'] == 4 and calls['video'] == 4 and calls['force'][-1] is True
    service.config['cache_identity']['video_model'] = 'controlled-v2'
    assert media(client, service, book)['status'] == 'succeeded'
    assert calls['video'] == 6


def test_failed_job_retry_and_restart_interruption_are_durable(pipeline):
    client, service, calls, config, adapters = pipeline
    book = imported(client, service)
    calls['fail_tts'] = True
    failed = media(client, service, book)
    assert failed['status'] == 'failed' and failed['attempts'] == 1
    assert failed['error'] and failed['started_at'] and failed['finished_at']
    assert service.manifest(book)['pages'][0]['media']['tts']['status'] == 'failed'
    service._update_job(failed['job_id'], status='running')
    restarted = BookPipelineService(config, adapters)
    try:
        assert restarted.job(failed['job_id'])['status'] == 'interrupted'
        calls['fail_tts'] = False
        restarted.retry(failed['job_id'])
        retried = restarted.wait_for_job(failed['job_id'])
        assert retried['status'] == 'succeeded' and retried['attempts'] == 2
        assert not list(Path(config['data_dir']).rglob('*.tmp'))
    finally:
        restarted.close()


def test_missing_adapters_unread_pages_and_bad_files_do_not_succeed(pipeline):
    client, service, calls, *_ = pipeline
    fast = imported(client, service, mode='fast')
    assert media(client, service, fast)['status'] == 'failed'
    book = imported(client, service)
    service.adapters.pop('video_generate')
    job = media(client, service, book)
    assert job['status'] == 'failed'
    assert service.manifest(book)['pages'][0]['media']['video']['status'] == 'failed'
    response = client.post('/api/books/import', data={'file': (io.BytesIO(b'not a PDF'), 'fake.pdf')})
    assert response.status_code == 400
    service.max_upload = 5
    response = client.post('/api/books/import', data={'file': (io.BytesIO(pdf_bytes()), 'big.pdf')})
    assert response.status_code == 413
    assert media(client, service, book, page_ids=['p9999'])['status'] == 'failed'


def test_grounded_qa_history_attachments_and_no_evidence(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    image = Image.new('RGB', (10, 10), 'red')
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    response = client.post(f'/api/books/{book}/attachments', data={'kind': 'image', 'file': (io.BytesIO(buffer.getvalue()), 'image.png')})
    assert response.status_code == 200
    ref = response.get_json()['ref']
    result = client.post(f'/api/books/{book}/qa', json={'question': 'What does the rabbit share?', 'page_id': 'p0001', 'image_ref': ref}).get_json()
    assert result['success'] and result['citations'][0]['page_id'] == 'p0001'
    assert result['turn']['image_ref'] == ref
    assert calls['answer'][0][2] == []
    assert isinstance(calls['answer'][0][1], list) and 'excerpt' in calls['answer'][0][1][0]
    follow = client.post(f'/api/books/{book}/qa', json={'question': 'Rabbit?', 'session_id': result['session_id']}).get_json()
    assert follow['success'] and len(calls['answer'][-1][2]) == 1
    saved = client.get(f"/api/books/{book}/sessions/{result['session_id']}").get_json()['session']
    assert len(saved['turns']) == 2 and saved['turns'][0]['image_ref'] == ref
    before = len(calls['answer'])
    unknown = client.post(f'/api/books/{book}/qa', json={'question': '火星距离多少公里'}).get_json()
    assert unknown['answer_source'] == 'no_evidence' and len(calls['answer']) == before
    for question in ('火星是什么', '这页为什么出现火星'):
        unrelated = client.post(f'/api/books/{book}/qa', json={'question': question, 'page_id': 'p0001'}).get_json()
        assert unrelated['answer_source'] == 'no_evidence' and len(calls['answer']) == before
        unrelated_with_history = client.post(f'/api/books/{book}/qa', json={
            'question': question, 'page_id': 'p0001', 'session_id': result['session_id']}).get_json()
        assert unrelated_with_history['answer_source'] == 'no_evidence' and len(calls['answer']) == before
    summary = client.post(f'/api/books/{book}/qa', json={'question': '这页讲了什么', 'page_id': 'p0001'}).get_json()
    assert summary['citations'][0]['page_id'] == 'p0001'
    service.adapters.pop('answer')
    excerpt = client.post(f'/api/books/{book}/qa', json={'question': 'Rabbit apples'}).get_json()
    assert excerpt['answer_source'] == 'extractive' and '未调用' in excerpt['answer']
    for invalid in ('../source.pdf', '/etc/passwd', 'attachments/not-there.png', f'../other/{ref}'):
        assert client.post(f'/api/books/{book}/qa', json={'question': 'rabbit', 'image_ref': invalid}).status_code in (400, 404)


def test_transcribe_ref_saved_and_provider_failures_are_explicit(pipeline, tmp_path):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    audio = Path(wav(tmp_path / 'voice.wav')).read_bytes()
    response = client.post(f'/api/books/{book}/transcribe', data={'file': (io.BytesIO(audio), 'voice.wav')})
    assert response.status_code == 503
    service.adapters['transcribe'] = lambda path, language: 'Rabbit apples'
    response = client.post(f'/api/books/{book}/transcribe', data={'file': (io.BytesIO(audio), 'voice.wav')})
    assert response.status_code == 200
    result = response.get_json()
    assert result['text'] == 'Rabbit apples' and client.get(result['audio_url']).status_code == 200
    qa = client.post(f'/api/books/{book}/qa', json={'question': result['text'], 'audio_ref': result['audio_ref']}).get_json()
    assert qa['turn']['audio_ref'] == result['audio_ref']
    service.adapters['answer'] = lambda *a: (_ for _ in ()).throw(RuntimeError('provider failure'))
    response = client.post(f'/api/books/{book}/qa', json={'question': 'Rabbit apples'})
    assert response.status_code == 502 and response.get_json()['success'] is False


def test_model_citations_are_validated_and_unconfigured_models_use_explicit_extracts(pipeline):
    client, service, *_ = pipeline
    book = imported(client, service)
    endpoint = f'/api/books/{book}/qa'
    for invalid in ('A made-up fact [p9999].', 'An answer with no explicit reference.'):
        service.adapters['answer'] = lambda *args, value=invalid: value
        response = client.post(endpoint, json={'question': 'Rabbit apples'})
        assert response.status_code == 502 and response.get_json()['success'] is False
    service.adapters['answer'] = lambda *args: 'Apples [p0001].'
    result = client.post(endpoint, json={'question': 'Rabbit apples'}).get_json()
    assert result['answer_source'] == 'model_with_validated_citations'
    assert [c['page_id'] for c in result['citations']] == ['p0001']
    assert result['turn']['grounding_status'] == 'citations_validated_not_fact_checked'
    service.adapters['answer'] = lambda *args: '我不知道，但我猜火星上也有苹果。'
    unknown = client.post(endpoint, json={'question': 'Rabbit apples'}).get_json()
    assert unknown['answer_source'] == 'model_abstained' and unknown['citations'] == []
    assert '火星' not in unknown['answer']
    service.adapters['capabilities'] = lambda: {'answer': False}
    service.adapters['answer'] = lambda *args: pytest.fail('unconfigured model must not be called')
    assert client.post(endpoint, json={'question': 'Rabbit apples'}).get_json()['answer_source'] == 'extractive'


def test_real_pipeline_export_and_temporal_citations(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    assert media(client, service, book)['status'] == 'succeeded'
    response = client.post(f'/api/books/{book}/export', json={})
    job = service.wait_for_job(response.get_json()['job_id'])
    assert job['status'] == 'succeeded', job
    export = job['result']
    assert export['probe']['has_audio'] and export['probe']['has_video']
    assert client.get(export['video_url']).status_code == 200
    qa = client.post(f'/api/books/{book}/qa', json={'question': 'Rabbit apples', 'book_time': .1, 'frame_time': .05}).get_json()
    assert qa['turn']['page_id'] == 'p0001' and qa['turn']['frame_time'] == .05
    assert qa['citations'][0]['start'] == 0 and qa['citations'][0]['end'] == pytest.approx(.24)
    assert client.post(f'/api/books/{book}/qa', json={'question': 'rabbit', 'book_time': 100}).status_code == 400


def test_export_is_invalidated_before_failed_regeneration(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    assert media(client, service, book)['status'] == 'succeeded'
    response = client.post(f'/api/books/{book}/export', json={})
    assert service.wait_for_job(response.get_json()['job_id'])['status'] == 'succeeded'
    assert service.manifest(book)['exports']
    calls['fail_tts'] = True
    assert media(client, service, book, force=True)['status'] == 'failed'
    assert service.manifest(book)['exports'] is None


def test_concurrent_qa_preserves_all_turns_and_chinese_keyword_retrieval(pipeline):
    from concurrent.futures import ThreadPoolExecutor
    client, service, calls, *_ = pipeline
    document = fitz.open()
    document.new_page(width=300, height=150).insert_text((10, 30), '小兔子把红苹果送给小松鼠。', fontname='china-s', fontsize=10)
    data = document.tobytes()
    document.close()
    response = client.post('/api/books/import', data={'file': (io.BytesIO(data), 'chinese.pdf'), 'mode': 'fast'})
    book = response.get_json()['book_id']
    assert service.wait_for_job(response.get_json()['job_id'])['status'] == 'succeeded'
    first = service.qa(book, {'question': '红苹果送给谁'})
    assert first['citations'][0]['page_id'] == 'p0001'
    assert '小松鼠' in first['citations'][0]['excerpt']
    assert first['citations'][0]['start'] is None
    session_id = first['session_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.qa(book, {'question': '为什么送苹果', 'session_id': session_id}), range(8)))
    assert len(results) == 8
    saved = service.session(book, session_id)
    assert len(saved['turns']) == 9
    assert len({turn['turn_id'] for turn in saved['turns']}) == 9


def test_moment_search_is_sentence_level_and_gains_film_clock_after_export(pipeline):
    client, service, *_ = pipeline
    book = imported(client, service)
    assert media(client, service, book)['status'] == 'succeeded'
    before = client.get(f'/api/books/{book}/search', query_string={'q': 'fox'}).get_json()
    assert before['success'] and before['film'] is False and isinstance(before['timing_ms'], float)
    fox = before['moments'][0]
    assert fox['kind'] == 'speech' and fox['page_id'] == 'p0001' and 'Fox' in fox['text']
    assert fox['start'] == pytest.approx(.12) and fox['book_start'] is None
    job = service.wait_for_job(client.post(f'/api/books/{book}/export', json={}).get_json()['job_id'])
    assert job['status'] == 'succeeded', job
    after = client.get(f'/api/books/{book}/search', query_string={'q': 'fox likes'}).get_json()
    assert after['film'] is True
    assert after['moments'][0]['book_start'] == pytest.approx(.12) and after['moments'][0]['book_end'] == pytest.approx(.24)
    assert client.get(f'/api/books/{book}/search', query_string={'q': ''}).status_code == 400
    assert client.get(f'/api/books/{book}/search', query_string={'q': 'fox', 'page_id': 'p9999'}).status_code == 400


def test_picture_vocabulary_finds_wordless_pages_but_is_marked_inferred(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    manifest = service.manifest(book)
    manifest['pages'][1]['objects']['detections'] = [{'label': '小松鼠<squirrel>', 'box': [1, 1, 20, 20]}]
    manifest['pages'][1]['scene_analysis'] = {'status': 'ready', 'inferred': True, 'data': {
        'summary': 'A squirrel holds a nut', 'scene': 'forest', 'characters': ['squirrel'], 'key_objects': ['nut']}}
    service._save_manifest(book, manifest)
    found = service.search(book, '小松鼠在哪')
    assert found['moments'][0]['kind'] == 'visual' and found['moments'][0]['page_id'] == 'p0002'
    assert found['moments'][0]['inferred'] is True and '小松鼠' in found['moments'][0]['text']
    answer = client.post(f"/api/books/{book}/qa", json={"question": "rabbit squirrel nut"}).get_json()
    cited = {cite['page_id']: cite for cite in calls['answer'][-1][1]}
    assert 'p0002' in cited and cited['p0002']['visual']['inferred'] is True and cited['p0002']['excerpt'] == ''
    assert answer['turn']['moments'] == answer['moments']
    # Text evidence still outranks picture vocabulary.
    ranked = service.qa(book, {'question': 'Rabbit apples squirrel'})
    assert ranked['citations'][0]['page_id'] == 'p0001'


def test_sentences_keep_closing_quotes_and_narration_skips_layout_furniture():
    from app.book_pipeline import _sentences, _narration_text
    text = ('夏天热，夏天长，夏天菜园菜成行。\n“绿色的蔬菜，红色的蔬菜，\n黄色的蔬菜，我都爱！”\n“快看，有个番茄在动！”\n'
            '汉字源流\n甲骨文\n楷书\n夏\n金文\n小篆\n隶书\n阅读指导\n猜一猜，番茄为什么会动？')
    narration = _narration_text(text)
    assert '汉字源流' not in narration and '阅读指导' not in narration and '猜一猜' not in narration
    sentences = _sentences(narration)
    assert sentences == ['夏天热，夏天长，夏天菜园菜成行。', '“绿色的蔬菜，红色的蔬菜，', '黄色的蔬菜，我都爱！”', '“快看，有个番茄在动！”']
    assert all(any(ch.isalnum() for ch in s) for s in sentences)
    assert _narration_text('仅供第二届北京市大学生“人工智能+”创新大赛使用，请勿外传\nThe fox ran.') == 'The fox ran.'
    assert _sentences('Run! "Stop," he said. Then he left.') == ['Run!', '"Stop," he said.', 'Then he left.']


def test_one_failing_page_does_not_abandon_the_book_and_retry_drops_force(pipeline):
    client, service, calls, *_ = pipeline
    book = imported(client, service)
    real = service.adapters['synthesize']
    def flaky(text, voice, output_path, language):
        if 'Fox' in text:
            raise RuntimeError('NoAudioReceived')
        return real(text, voice, output_path, language)
    service.adapters['synthesize'] = flaky
    job = media(client, service, book, force=True)
    assert job['status'] == 'failed' and '第1页朗读' in job['error'] and '1 / 2' in job['error']
    pages = service.manifest(book)['pages']
    assert pages[0]['media']['tts']['status'] == 'failed' and pages[1]['media']['video']['status'] == 'ready'
    service.adapters['synthesize'] = real
    videos = calls['video']
    client.post(f"/api/jobs/{job['job_id']}/retry")
    retried = service.wait_for_job(job['job_id'])
    assert retried['status'] == 'succeeded' and retried['request']['force'] is False
    assert calls['video'] == videos + 1  # only the failed page made a new (paid) video call

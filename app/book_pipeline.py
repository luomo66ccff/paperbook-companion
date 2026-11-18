"""Durable PDF -> book media -> grounded conversation service.

No provider is called unless an injected adapter or configured reader is used.
Paths in manifests and API results are always relative to their owning book.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import uuid
from urllib.parse import quote

from flask import jsonify, request, send_file
from PIL import Image

try:
    from .pdf_book_reader import PDFBookReader, PDFInputError, open_pdf_document
    from .book_video import (MediaError, concatenate_wav, export_book, probe_media,
                             wav_duration, write_silence)
except ImportError:
    from pdf_book_reader import PDFBookReader, PDFInputError, open_pdf_document
    from book_video import (MediaError, concatenate_wav, export_book, probe_media,
                            wav_duration, write_silence)


class PipelineError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode('utf-8')).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _read_json(path):
    return json.loads(Path(path).read_text('utf-8'))


def _identifier(value, kind='标识'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise PipelineError(f'{kind}无效')
    return value


_CLOSERS = '”’"\'』」）)》】'
_SPEAKABLE = re.compile(r'[0-9A-Za-z㐀-鿿]')


def _sentences(text):
    """Split into speakable sentences. Closing quotes/brackets stay with their sentence,
    and punctuation-only fragments (e.g. a lone ”) are merged back instead of being sent
    to TTS, which returns no audio for them."""
    parts = re.split(r'(?<=[。！？!?…])(?![' + re.escape(_CLOSERS) + r'。！？!?…])\s*'
                     r'|(?<=[。！？!?…][' + re.escape(_CLOSERS) + r'])\s*'
                     r'|(?<=\.)\s+|\n+', text)
    result = []
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if not _SPEAKABLE.search(part):
            if result:
                result[-1] += part
            continue
        result.append(part)
    return result


# Layout furniture of the sample series (character-origin sidebar, reading-guide box) and
# competition watermarks. Page text keeps them for QA/search; narration skips them.
_SIDEBAR_LABELS = {'汉字源流', '甲骨文', '金文', '小篆', '隶书', '楷书'}
_WATERMARK = re.compile(r'请勿外传|仅供.{0,40}(?:比赛|大赛)|创新大赛|纸书智伴')


def _narration_text(text):
    lines, skip_guide = [], False
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if _WATERMARK.search(line) or line in _SIDEBAR_LABELS:
            continue
        if re.fullmatch(r'[㐀-鿿]', line):
            continue  # the single character shown in the character-origin sidebar
        if line == '阅读指导':
            skip_guide = True
            continue
        if skip_guide:
            skip_guide = False
            continue  # the one guide question is addressed to the grown-up, not narrated
        lines.append(line)
    return '\n'.join(lines).strip()


def _tokens(text):
    lowered = text.lower()
    result = set(re.findall(r'[a-z0-9]+', lowered))
    for run in re.findall(r'[\u4e00-\u9fff]+', lowered):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            result.add(run)
    return result


# Question scaffolding bigrams carry no story content; they only add noise to moment search.
_QUERY_STOP = {'什么', '怎么', '为什', '么时', '时候', '哪里', '哪儿', '这个', '那个', '是谁', '在哪',
               '了吗', '是什', '么呢', '一下', '告诉', '诉我', '我们', '你们', '有没', '的是', '找找',
               '一找', '找一', '画面', '视频', '影片', '出现', '现在', '里面', '哪一', '一页', '哪页'}


def _query_tokens(text):
    return _tokens(text) - _QUERY_STOP


def _clean_label(value):
    return re.sub(r'<[^>]*>', '', str(value or '')).strip()


def _visual_terms(page):
    """Model-inferred picture vocabulary for a page: scene summary, characters, objects, boxes."""
    terms = []
    scene = page.get('scene_analysis') or {}
    data = scene.get('data') if scene.get('status') == 'ready' else None
    if isinstance(data, dict):
        for key in ('summary', 'scene'):
            if isinstance(data.get(key), str):
                terms.append(data[key])
        for key in ('characters', 'key_objects'):
            for item in data.get(key) or []:
                if isinstance(item, str):
                    terms.append(item)
                elif isinstance(item, dict):
                    terms.extend(str(v) for v in item.values() if isinstance(v, str))
    for detection in (page.get('objects') or {}).get('detections') or []:
        if isinstance(detection, dict):
            terms.append(_clean_label(detection.get('label')))
    seen, result = set(), []
    for term in (t.strip() for t in terms):
        if term and term not in seen:
            seen.add(term)
            result.append(term[:200])
    return result


def search_moments(manifest, timeline, queries, page_id=None, limit=5):
    """Rank sentence-level speech moments and page-level picture moments.

    Speech rows carry page-local start/end from measured sentence WAVs and, once
    the whole film is exported, book_start/book_end on the film clock. Visual
    rows are model-inferred and always marked as such.
    """
    timing = {row['page_id']: row for row in timeline or []}
    rows = []
    for page in manifest.get('pages', []):
        pid, row = page['page_id'], timing.get(page['page_id'])
        boost = .5 if pid == page_id else 0
        tts = (page.get('media') or {}).get('tts') or {}
        spoken = (row or {}).get('sentences') or (tts.get('sentences') if tts.get('status') == 'ready' else None) or []
        base = {'page_id': pid, 'page_num': page['page_num']}
        if spoken:
            for sentence in spoken:
                score = len(queries & _tokens(sentence.get('text', '')))
                if score:
                    rows.append((score + boost, {**base, 'kind': 'speech', 'text': sentence['text'],
                                                 'start': sentence.get('start'), 'end': sentence.get('end'),
                                                 'book_start': sentence.get('book_start'), 'book_end': sentence.get('book_end'),
                                                 'inferred': False}))
        else:
            for sentence in _sentences(page.get('text', '')):
                score = len(queries & _tokens(sentence))
                if score:
                    rows.append((score + boost, {**base, 'kind': 'text', 'text': sentence, 'start': None, 'end': None,
                                                 'book_start': None, 'book_end': None, 'inferred': False}))
        hits = [term for term in _visual_terms(page) if queries & _tokens(term)]
        if hits:
            score = len(queries & _tokens(' '.join(hits))) - .25
            rows.append((score + boost, {**base, 'kind': 'visual', 'text': '画面里有：' + '、'.join(hits[:4]),
                                         'start': 0.0, 'end': tts.get('duration'),
                                         'book_start': row.get('video_start', row['book_start']) if row else None,
                                         'book_end': row.get('video_end', row['book_end']) if row else None,
                                         'inferred': True}))
    rows.sort(key=lambda item: (-item[0], item[1]['page_num'], item[1]['start'] or 0))
    return [dict(item, score=round(score, 3)) for score, item in rows[:limit]]


class BookPipelineService:
    def __init__(self, config, adapters):
        self.config, self.adapters = dict(config), dict(adapters or {})
        self.root = Path(self.config['data_dir']).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.job_dir = self.root / '.jobs'
        self.job_dir.mkdir(exist_ok=True)
        self.max_upload = int(self.config.get('max_upload_bytes', 128 * 1024 * 1024))
        self.max_pages = int(self.config.get('max_pages', 100))
        self.executor = ThreadPoolExecutor(max_workers=int(self.config.get('max_workers', 2)), thread_name_prefix='book-job')
        self._guard = threading.RLock()
        self._book_locks, self._futures = {}, {}
        # Recovery never silently repeats an external model call.
        for path in self.job_dir.glob('*.json'):
            job = _read_json(path)
            if job.get('status') in ('queued', 'running'):
                job.update(status='interrupted', updated_at=_now(), finished_at=_now(),
                           error='服务重启中断任务，可显式重试')
                _atomic_json(path, job)
                try:
                    manifest = self.manifest(job['book_id'])
                    if job.get('kind') == 'import':
                        manifest['status'] = 'interrupted'
                    for page in manifest.get('pages', []):
                        for media in page.get('media', {}).values():
                            if media.get('status') == 'running':
                                media['status'] = 'interrupted'
                    self._save_manifest(job['book_id'], manifest)
                except (PipelineError, KeyError):
                    pass

    def close(self, wait=True):
        self.executor.shutdown(wait=wait)

    def _lock(self, book_id):
        with self._guard:
            return self._book_locks.setdefault(book_id, threading.RLock())

    def book_dir(self, book_id):
        return self.root / _identifier(book_id, '书本标识')

    def manifest(self, book_id):
        file = self.book_dir(book_id) / 'manifest.json'
        if not file.is_file():
            raise PipelineError('书本不存在', 404)
        return _read_json(file)

    def _save_manifest(self, book_id, manifest):
        manifest['updated_at'] = _now()
        _atomic_json(self.book_dir(book_id) / 'manifest.json', manifest)

    def asset_url(self, book_id, relative):
        return '/api/books/' + quote(book_id, safe='') + '/assets/' + quote(relative, safe='/')

    def asset(self, book_id, relative, allow_source=False):
        root = self.book_dir(book_id).resolve()
        if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative or '\x00' in relative:
            raise PipelineError('资源引用必须为本书相对路径')
        posix = PurePosixPath(relative)
        if posix.is_absolute() or '..' in posix.parts or '.' in posix.parts:
            raise PipelineError('资源路径越界')
        allowed = {'images', 'media', 'exports', 'attachments'}
        if (posix.parts[0] not in allowed) and not (allow_source and relative == 'source.pdf'):
            raise PipelineError('资源目录不允许访问')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise PipelineError('资源不存在', 404)
        return path

    def _ref(self, book_id, relative, kind):
        if relative is None:
            return None
        path = self.asset(book_id, relative)
        extensions = ({'.wav', '.webm', '.mp3', '.m4a', '.ogg', '.flac'} if kind == 'audio'
                      else {'.png', '.jpg', '.jpeg', '.webp'})
        if path.suffix.lower() not in extensions:
            raise PipelineError('媒体引用类型不匹配')
        return relative

    def _job_path(self, job_id):
        return self.job_dir / (_identifier(job_id, '任务标识') + '.json')

    def job(self, job_id):
        path = self._job_path(job_id)
        if not path.is_file():
            raise PipelineError('任务不存在', 404)
        return _read_json(path)

    def _update_job(self, job_id, **changes):
        with self._guard:
            job = self.job(job_id)
            job.update(changes, updated_at=_now())
            _atomic_json(self._job_path(job_id), job)
            return job

    def submit(self, kind, book_id, payload=None):
        self.manifest(book_id)
        job_id = uuid.uuid4().hex
        stamp = _now()
        job = {'job_id': job_id, 'kind': kind, 'book_id': book_id, 'status': 'queued',
               'attempts': 0, 'progress': {'current': 0, 'total': 0}, 'error': None,
               'created_at': stamp, 'updated_at': stamp, 'started_at': None, 'finished_at': None,
               'request': payload or {}, 'result': None}
        _atomic_json(self._job_path(job_id), job)
        with self._guard:
            self._futures[job_id] = self.executor.submit(self._run_job, job_id)
        return job_id

    def retry(self, job_id):
        with self._guard:
            job = self.job(job_id)
            if job['status'] not in ('failed', 'interrupted'):
                raise PipelineError('仅失败或中断的任务可重试', 409)
            changes = {}
            if job.get('kind') == 'media' and job.get('request', {}).get('force'):
                # A retry finishes what is missing; pages already regenerated stay cached (no repeat paid calls).
                changes['request'] = {**job['request'], 'force': False}
            self._update_job(job_id, status='queued', error=None, result=None, finished_at=None, **changes)
            self._futures[job_id] = self.executor.submit(self._run_job, job_id)
        return job_id

    def wait_for_job(self, job_id, timeout=60):
        with self._guard:
            future = self._futures.get(job_id)
        if future:
            future.result(timeout=timeout)
        return self.job(job_id)

    def _run_job(self, job_id):
        job = self.job(job_id)
        book_id, kind = job['book_id'], job['kind']
        with self._lock(book_id):
            self._update_job(job_id, status='running', attempts=job['attempts'] + 1,
                             started_at=_now(), finished_at=None, error=None)
            try:
                operation = {'import': self._import, 'media': self._media, 'export': self._export}.get(kind)
                if operation is None:
                    raise PipelineError('未知任务类型')
                result = operation(book_id, job['request'], job_id)
                self._update_job(job_id, status='succeeded', result=result, finished_at=_now())
            except Exception as exc:
                # Unexpected adapters must not leak absolute paths/provider secrets.
                error = str(exc) if isinstance(exc, (PipelineError, MediaError)) else f'任务执行失败（{type(exc).__name__}），请检查接口配置或稍后重试'
                self._update_job(job_id, status='failed', error=error, finished_at=_now())
                manifest = self.manifest(book_id)
                manifest['last_error'] = {'kind': kind, 'message': error, 'job_id': job_id}
                if kind == 'import':
                    manifest['status'] = 'failed'
                self._save_manifest(book_id, manifest)

    def import_pdf(self, upload, mode='auto', verify_ocr=False, title=None):
        if mode not in ('fast', 'auto', 'full'):
            raise PipelineError('mode 必须为 fast、auto 或 full')
        book_id = uuid.uuid4().hex
        root = self.book_dir(book_id)
        root.mkdir()
        source = root / 'source.pdf'
        digest, size = hashlib.sha256(), 0
        try:
            with source.open('wb') as target:
                while chunk := upload.stream.read(min(1024 * 1024, self.max_upload + 1)):
                    size += len(chunk)
                    if size > self.max_upload:
                        raise PipelineError('PDF 超过上传大小限制', 413)
                    digest.update(chunk)
                    target.write(chunk)
            try:
                with open_pdf_document(source, max_pages=self.max_pages):
                    pass
            except PDFInputError as exc:
                raise PipelineError(str(exc)) from exc
            name = title or Path(upload.filename or '绘本.pdf').stem
            name = str(name).strip()[:200] or '绘本'
            manifest = {'schema_version': 1, 'book_id': book_id, 'title': name, 'status': 'queued',
                        'source': {'kind': 'pdf', 'pdf_sha256': digest.hexdigest()}, 'pages': [],
                        'created_at': _now(), 'updated_at': _now(), 'exports': None}
            self._save_manifest(book_id, manifest)
            job_id = self.submit('import', book_id, {'mode': mode, 'verify_ocr': bool(verify_ocr)})
            return book_id, job_id
        except Exception:
            # root is a just-created, validated book UUID beneath self.root.
            if root.resolve().parent == self.root and root.is_dir():
                shutil.rmtree(root)
            raise

    def _import(self, book_id, payload, job_id):
        manifest = self.manifest(book_id)
        manifest['status'] = 'running'
        manifest['exports'] = None
        self._save_manifest(book_id, manifest)
        factory = self.adapters.get('reader_factory')
        reader = factory() if factory else PDFBookReader(**self.config.get('reader_options', {}))
        if hasattr(reader, 'set_progress_callback'):
            reader.set_progress_callback(lambda stage, current, total, message='':
                                         self._update_job(job_id, progress={'stage': stage, 'current': current,
                                                                           'total': total, 'message': message}))
        parsed = reader.read_book(str(self.book_dir(book_id) / 'source.pdf'), str(self.book_dir(book_id)),
                                  mode=payload.get('mode', 'auto'), verify_ocr=bool(payload.get('verify_ocr', False)),
                                  max_pages=self.max_pages)
        pages = parsed.get('pages')
        if not isinstance(pages, list) or not pages or len(pages) > self.max_pages:
            raise PipelineError('解析器没有返回有效页数据')
        for index, page in enumerate(pages):
            if page.get('page_id') != f'p{index + 1:04d}' or page.get('page_num') != index + 1:
                raise PipelineError('解析器必须保留全部原页和稳定页号')
            self.asset(book_id, page['image_path'])
            page['image_url'] = self.asset_url(book_id, page['image_path'])
            page.setdefault('media', {'tts': {'status': 'not_requested'}, 'video': {'status': 'not_requested'}})
        manifest.update(pages=pages, page_count=len(pages), status=parsed.get('status', 'degraded'),
                        mode=payload.get('mode', 'auto'), verify_ocr=bool(payload.get('verify_ocr', False)),
                        language=parsed.get('language', 'zh'), warnings=parsed.get('warnings', []), exports=None)
        manifest.pop('last_error', None)
        self._save_manifest(book_id, manifest)
        return {'book_id': book_id, 'status': manifest['status'], 'page_count': len(pages)}

    def _media(self, book_id, payload, job_id):
        manifest = self.manifest(book_id)
        requested = payload.get('page_ids')
        if requested is not None and (not isinstance(requested, list) or not all(isinstance(p, str) for p in requested)):
            raise PipelineError('page_ids 必须是页标识数组')
        selected = set(requested) if requested is not None else {p['page_id'] for p in manifest['pages']}
        if not selected or selected - {p['page_id'] for p in manifest['pages']}:
            raise PipelineError('包含不存在的页或没有选择页')
        voice = payload.get('voice', 'xiaoyuan')
        if not isinstance(voice, str) or not voice or len(voice) > 100:
            raise PipelineError('音色无效')
        options = {**self.config.get('default_video', {}), **(payload.get('video_options') or {})}
        # Tool paths belong to trusted server config, never user JSON.
        if any(k in options for k in ('ffmpeg', 'ffprobe')):
            raise PipelineError('视频请求不能指定可执行文件')
        force, completed, failures = bool(payload.get('force', False)), [], []
        caps = self.adapters.get('capabilities', lambda: {})()
        for page in manifest['pages']:
            if page['page_id'] not in selected:
                continue
            stage = 'tts'
            try:
                if page.get('text_status') in ('needs_ocr', 'ocr_failed'):
                    raise PipelineError('此页文字尚未可靠读取，请先重新分析，不能以静音代替')
                image = self.asset(book_id, page['image_path'])
                text = page.get('text', '').strip()
                # Narration skips layout furniture/watermarks; the page text itself is unchanged.
                narration = _narration_text(text)
                base_key = {'version': 1, 'text': narration, 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                            'voice': voice, 'language': manifest.get('language', 'zh'), 'options': options,
                            'models': {**self.config.get('cache_identity', {}),
                                       'reader_vl_model': self.config.get('reader_options', {}).get('vl_model'),
                                       'capabilities': caps}, 'prompt_version': 'book-page-v2'}
                tts_key = _hash({**base_key, 'stage': 'tts'})
                target_rel = f"media/{page['page_id']}/{tts_key[:24]}"  # short dir: Windows MAX_PATH; full key stays in the manifest
                media_dir = self.book_dir(book_id) / target_rel
                media_dir.mkdir(parents=True, exist_ok=True)
                old = page['media'].get('tts', {})
                cached = not force and old.get('cache_key') == tts_key and old.get('status') in ('ready', 'silent')
                if cached:
                    try:
                        wav_duration(self.asset(book_id, old['path']))
                        for sentence in old.get('sentences', []):
                            self.asset(book_id, sentence['audio_path'])
                    except (PipelineError, MediaError, KeyError):
                        cached = False
                if not cached:
                    manifest['exports'] = None
                    page['media']['tts'] = {'status': 'running', 'cache_key': tts_key}
                    self._save_manifest(book_id, manifest)
                    sentences, files, elapsed = [], [], 0.0
                    if narration:
                        synthesize = self.adapters.get('synthesize')
                        if synthesize is None:
                            raise PipelineError('未配置配音适配器', 503)
                        for index, sentence in enumerate(_sentences(narration)):
                            path = media_dir / f'sentence_{index + 1:04d}.wav'
                            returned = synthesize(sentence, voice, str(path), manifest.get('language', 'zh'))
                            if not returned or not Path(returned).is_file():
                                raise MediaError('配音适配器未返回真实 WAV')
                            if Path(returned).resolve() != path.resolve():
                                shutil.copyfile(returned, path)
                            duration = wav_duration(path)
                            rel = path.relative_to(self.book_dir(book_id)).as_posix()
                            sentences.append({'text': sentence, 'start': round(elapsed, 9), 'end': round(elapsed + duration, 9),
                                              'duration': duration, 'audio_path': rel, 'audio_url': self.asset_url(book_id, rel)})
                            files.append(path)
                            elapsed += duration
                        duration = concatenate_wav(files, media_dir / 'narration.wav')
                        status = 'ready'
                    else:
                        duration = write_silence(media_dir / 'narration.wav', options.get('blank_duration', 3))
                        status = 'silent'
                    rel = target_rel + '/narration.wav'
                    page['media']['tts'] = {'status': status, 'cache_key': tts_key, 'path': rel,
                                            'url': self.asset_url(book_id, rel), 'duration': duration,
                                            'sentences': sentences, 'voice': voice, 'cached': False,
                                            'silent_reason': None if narration else ('confirmed_empty_page' if not text else 'no_narration_text')}
                else:
                    page['media']['tts']['cached'] = True
                stage = 'video'
                prompt = '保持原页画风、角色和构图，轻微自然运动，不添加文字。页面转录：' + narration
                video_key = _hash({**base_key, 'stage': 'video', 'prompt': prompt})
                video_rel = f"media/{page['page_id']}/{video_key[:24]}/clip.mp4"
                video_path = self.book_dir(book_id) / video_rel
                old = page['media'].get('video', {})
                cached = not force and old.get('cache_key') == video_key and old.get('status') == 'ready'
                if cached:
                    try:
                        info = probe_media(self.asset(book_id, old['path']))
                        cached = info['has_video'] and info['duration'] > 0
                    except (PipelineError, MediaError, KeyError):
                        cached = False
                if not cached:
                    manifest['exports'] = None
                    page['media']['video'] = {'status': 'running', 'cache_key': video_key}
                    self._save_manifest(book_id, manifest)
                    generate = self.adapters.get('video_generate')
                    if generate is None:
                        raise PipelineError('未配置视频适配器', 503)
                    video_path.parent.mkdir(parents=True, exist_ok=True)
                    returned = generate(str(image), prompt, str(video_path), {**options, 'force': force})
                    if not returned or not Path(returned).is_file():
                        raise MediaError('视频适配器未返回真实 MP4')
                    if Path(returned).resolve() != video_path.resolve():
                        shutil.copyfile(returned, video_path)
                    info = probe_media(video_path)
                    if not info['has_video'] or info['duration'] <= 0:
                        raise MediaError('视频适配器返回文件缺少视频流')
                    page['media']['video'] = {'status': 'ready', 'cache_key': video_key, 'path': video_rel,
                                              'url': self.asset_url(book_id, video_rel), 'duration': info['duration'],
                                              'options': options, 'cached': False}
                else:
                    page['media']['video']['cached'] = True
                completed.append(page['page_id'])
                manifest['exports'] = None
                self._save_manifest(book_id, manifest)
                self._update_job(job_id, progress={'current': len(completed), 'total': len(selected), 'page_id': page['page_id']})
            except Exception as exc:
                # One page failing must not abandon the rest of the book (or waste finished paid calls).
                reason = str(exc) if isinstance(exc, PipelineError) else type(exc).__name__
                page['media'].setdefault(stage, {})['status'] = 'failed'
                page['media'][stage]['error'] = ('朗读' if stage == 'tts' else '动画') + '生成未完成（' + reason[:120] + '），可重试'
                self._save_manifest(book_id, manifest)
                failures.append((page['page_num'], stage, reason))
                if isinstance(exc, PipelineError) and exc.status == 503:
                    break  # the adapter is not configured at all; other pages would fail the same way
        if failures:
            listed = '、'.join(f"第{num}页{'朗读' if stage == 'tts' else '动画'}（{reason[:60]}）" for num, stage, reason in failures[:6])
            raise PipelineError(f'已完成 {len(completed)} / {len(selected)} 页；未完成：{listed}' + ('…' if len(failures) > 6 else '') + '。可点“重试”只补做未完成的页', 502)
        return {'book_id': book_id, 'page_ids': completed}

    def _export(self, book_id, payload, job_id):
        manifest = self.manifest(book_id)
        options = {**self.config.get('default_video', {}), **(payload.get('video_options') or {})}
        if any(k in options for k in ('ffmpeg', 'ffprobe')):
            raise PipelineError('导出请求不能指定可执行文件')
        result = export_book(self.book_dir(book_id), manifest, options, force=bool(payload.get('force', False)))
        for prefix in ('video', 'srt', 'timeline'):
            result[prefix + '_url'] = self.asset_url(book_id, result[prefix + '_path'])
        manifest['exports'] = result
        self._save_manifest(book_id, manifest)
        self._update_job(job_id, progress={'current': 1, 'total': 1})
        return result

    def list_books(self):
        rows = []
        for file in self.root.glob('*/manifest.json'):
            manifest = _read_json(file)
            rows.append({key: manifest.get(key) for key in ('book_id', 'title', 'status', 'page_count', 'created_at', 'updated_at')})
        return sorted(rows, key=lambda row: row.get('created_at', ''), reverse=True)

    def attachment(self, book_id, upload, kind):
        self.manifest(book_id)
        if kind not in ('audio', 'image'):
            raise PipelineError('kind 必须为 audio 或 image')
        data = upload.stream.read(min(self.max_upload, 64 * 1024 * 1024) + 1)
        if len(data) > min(self.max_upload, 64 * 1024 * 1024):
            raise PipelineError('附件超过大小限制', 413)
        extension = Path(upload.filename or '').suffix.lower()
        allowed = ({'.wav', '.webm', '.mp3', '.m4a', '.ogg', '.flac'} if kind == 'audio'
                   else {'.png', '.jpg', '.jpeg', '.webp'})
        if extension not in allowed:
            raise PipelineError('不支持的附件格式')
        if kind == 'image':
            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
            except Exception as exc:
                raise PipelineError('图片附件无法解码') from exc
        rel = 'attachments/' + uuid.uuid4().hex + extension
        path = self.book_dir(book_id) / rel
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        if kind == 'audio':
            try:
                info = probe_media(path)
                if not info['has_audio']:
                    raise MediaError('附件没有音频流')
            except Exception as exc:
                path.unlink(missing_ok=True)
                raise PipelineError('音频附件无法解码') from exc
        return {'ref': rel, 'url': self.asset_url(book_id, rel)}

    def session(self, book_id, session_id):
        self.manifest(book_id)
        file = self.book_dir(book_id) / 'sessions' / (_identifier(session_id, '会话标识') + '.json')
        if not file.is_file():
            raise PipelineError('会话不存在', 404)
        return _read_json(file)

    def qa(self, book_id, payload):
        question = payload.get('question')
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise PipelineError('问题不能为空且不得超过4000字')
        with self._lock(book_id):
            manifest = self.manifest(book_id)
            page_id = payload.get('page_id')
            pages = manifest.get('pages', [])
            if page_id is not None and page_id not in {p['page_id'] for p in pages}:
                raise PipelineError('页标识不存在')
            times = {}
            for key in ('book_time', 'frame_time'):
                value = payload.get(key)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise PipelineError('时间必须是非负有限秒数')
                    times[key] = value
            audio_ref = self._ref(book_id, payload.get('audio_ref'), 'audio')
            image_ref = self._ref(book_id, payload.get('image_ref'), 'image')
            session_id = payload.get('session_id') or uuid.uuid4().hex
            _identifier(session_id, '会话标识')
            session_path = self.book_dir(book_id) / 'sessions' / (session_id + '.json')
            session = _read_json(session_path) if session_path.is_file() else {'schema_version': 1, 'session_id': session_id, 'book_id': book_id, 'turns': [], 'created_at': _now()}
            timeline = []
            if manifest.get('exports'):
                timeline = _read_json(self.asset(book_id, manifest['exports']['timeline_path']))['pages']
            if 'book_time' in times and timeline:
                if times['book_time'] > timeline[-1].get('video_end', timeline[-1]['book_end']):
                    raise PipelineError('book_time 超出整片时间轴')
                if page_id is None:
                    page_id = next((p['page_id'] for p in timeline
                                    if p.get('video_start', p['book_start']) <= times['book_time'] < p.get('video_end', p['book_end'])), timeline[-1]['page_id'])
            queries = _tokens(question)
            prior = session['turns'][-6:]
            # Follow-ups can use prior user terms, but still retrieve only real book text.
            refers_back = (re.search(r'它|他|她|\b(?:it|he|she|they|them)\b', question, re.I)
                           or re.fullmatch(r'(?:为什么|然后呢|还有呢|继续|那后来呢|接下来呢|why|and then|what next)[？?。!！]*', question.strip(), re.I))
            if prior and refers_back:
                queries |= _tokens(' '.join(t['question'] for t in prior[-2:]))
            ranked = []
            page_summary = bool(re.fullmatch(
                r'(?:这页|这一页|当前页)(?:讲了什么|说了什么|写了什么|原文|内容是什么|讲什么|内容)[？?。!！]*'
                r'|what (?:does|is on) (?:this|the current) page(?: say)?[?]?', question.strip(), re.I))
            visual_by_page = {}
            for page in pages:
                text = page.get('text', '').strip()
                visual = _visual_terms(page)
                visual_hits = len((queries - _QUERY_STOP) & _tokens(' '.join(visual))) if visual else 0
                if not text and not visual_hits:
                    continue
                score = len(queries & _tokens(text)) if text else 0
                if page['page_id'] == page_id and text and (score > 0 or page_summary):
                    score += 3
                # Picture vocabulary is model-inferred: it can surface a page, never outrank its text.
                score += .5 * visual_hits
                if score:
                    ranked.append((score, page))
                    visual_by_page[page['page_id']] = visual
            ranked.sort(key=lambda row: (-row[0], row[1]['page_num']))
            citations = []
            for _, page in ranked[:3]:
                timing = next((p for p in timeline if p['page_id'] == page['page_id']), None)
                cite = {'page_id': page['page_id'], 'page_num': page['page_num'], 'excerpt': page.get('text', '')[:1200],
                        'start': None, 'end': None, 'time_status': 'unavailable_until_export'}
                if visual_by_page.get(page['page_id']):
                    cite['visual'] = {'inferred': True, 'terms': visual_by_page[page['page_id']][:8]}
                if timing:
                    cite.update(time_status='export_timeline', start=timing['book_start'], end=timing['book_end'],
                                book_start=timing['book_start'], book_end=timing['book_end'],
                                video_start=timing.get('video_start', timing['book_start']),
                                video_end=timing.get('video_end', timing['book_end']))
                citations.append(cite)
            if not citations:
                answer, source = '我不知道；当前绘本原文中没有找到足够依据。', 'no_evidence'
            elif self.adapters.get('answer') and self.adapters.get('capabilities', lambda: {'answer': True})().get('answer', False):
                try:
                    answer = self.adapters['answer'](question, citations, prior)
                except Exception as exc:
                    raise PipelineError('问答服务失败，请检查配置或受控服务', 502) from exc
                if not isinstance(answer, str) or not answer.strip():
                    raise PipelineError('问答服务未返回有效回答', 502)
                cited_ids = set(re.findall(r'\[(p\d+)\]', answer))
                allowed_ids = {cite['page_id'] for cite in citations}
                if not cited_ids:
                    if re.search(r'不知道|无法确定|没有足够依据|not (?:know|enough evidence)', answer, re.I):
                        answer, source, citations = '原文没有足够依据，我暂时不知道。', 'model_abstained', []
                    else:
                        raise PipelineError('模型回答缺少明确页引用，不能自动附上检索结果作为依据', 502)
                elif not cited_ids.issubset(allowed_ids):
                    raise PipelineError('模型回答引用了检索证据以外的页', 502)
                else:
                    citations = [cite for cite in citations if cite['page_id'] in cited_ids]
                    source = 'model_with_validated_citations'
            else:
                answer = '原文摘录（未调用问答模型）：\n' + '\n'.join(
                    f"第{c['page_num']}页：{c['excerpt'] or '（无文字）画面推断：' + '、'.join(c.get('visual', {}).get('terms', [])[:4])}"
                    for c in citations)
                source = 'extractive'
            moments = search_moments(manifest, timeline, (queries - _QUERY_STOP) or queries, page_id, 3) if citations else []
            turn = {'turn_id': uuid.uuid4().hex, 'question': question, 'answer': answer,
                    'answer_source': source, 'citations': citations, 'moments': moments, 'page_id': page_id,
                    'grounding_status': 'citations_validated_not_fact_checked' if source == 'model_with_validated_citations' else source,
                    **times, 'audio_ref': audio_ref, 'image_ref': image_ref, 'created_at': _now()}
            for kind, ref in (('audio', audio_ref), ('image', image_ref)):
                if ref:
                    turn[kind + '_url'] = self.asset_url(book_id, ref)
            session['turns'].append(turn)
            session['updated_at'] = _now()
            _atomic_json(session_path, session)
            return {'session_id': session_id, 'answer': answer, 'answer_source': source,
                    'citations': citations, 'moments': moments, 'turn': turn}

    def timeline(self, book_id, manifest=None):
        manifest = manifest or self.manifest(book_id)
        exports = manifest.get('exports')
        if not exports:
            return []
        return _read_json(self.asset(book_id, exports['timeline_path'])).get('pages', [])

    def search(self, book_id, query, page_id=None, limit=5):
        """Model-free, sub-second lookup of where something is said or seen in the book/film."""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise PipelineError('检索内容需在 1 至 200 字内')
        manifest = self.manifest(book_id)
        if page_id is not None and page_id not in {p['page_id'] for p in manifest.get('pages', [])}:
            raise PipelineError('页标识不存在')
        tokens = _query_tokens(query) or _tokens(query)
        return {'query': query, 'moments': search_moments(manifest, self.timeline(book_id, manifest), tokens, page_id, limit),
                'film': bool(manifest.get('exports'))}


def register_book_routes(app, config, adapters):
    service = BookPipelineService(config, adapters)

    @app.errorhandler(PipelineError)
    def book_error(exc):
        return jsonify(success=False, error=str(exc)), exc.status

    def body():
        value = request.get_json(silent=True)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise PipelineError('JSON 请求必须是对象')
        return value

    def queued(book_id, job_id):
        return jsonify(success=True, book_id=book_id, job_id=job_id), 202

    @app.post('/api/books/import')
    def books_import():
        upload = request.files.get('file')
        if upload is None:
            raise PipelineError('缺少 PDF 文件 file')
        verify = request.form.get('verify_ocr', 'false').lower() in ('true', '1', 'yes')
        book_id, job_id = service.import_pdf(upload, request.form.get('mode', 'auto'), verify, request.form.get('title'))
        return queued(book_id, job_id)

    @app.get('/api/books')
    def books_list():
        return jsonify(success=True, books=service.list_books())

    @app.get('/api/books/<book_id>')
    def books_get(book_id):
        return jsonify(success=True, book=service.manifest(book_id))

    @app.get('/api/books/<book_id>/assets/<path:relative>')
    def books_asset(book_id, relative):
        return send_file(service.asset(book_id, relative), conditional=True)

    @app.post('/api/books/<book_id>/analyze')
    def books_analyze(book_id):
        value = body()
        if value.get('mode', 'auto') not in ('fast', 'auto', 'full'):
            raise PipelineError('mode 必须为 fast、auto 或 full')
        return queued(book_id, service.submit('import', book_id, value))

    @app.post('/api/books/<book_id>/media')
    def books_media(book_id):
        return queued(book_id, service.submit('media', book_id, body()))

    @app.get('/api/jobs/<job_id>')
    def books_job(job_id):
        return jsonify(success=True, job=service.job(job_id))

    @app.post('/api/jobs/<job_id>/retry')
    def books_job_retry(job_id):
        service.retry(job_id)
        return queued(service.job(job_id)['book_id'], job_id)

    @app.post('/api/books/<book_id>/export')
    def books_export(book_id):
        return queued(book_id, service.submit('export', book_id, body()))

    @app.post('/api/books/<book_id>/qa')
    def books_qa(book_id):
        started = time.perf_counter()
        result = service.qa(book_id, body())
        return jsonify(success=True, **result, timing_ms=round((time.perf_counter() - started) * 1000, 1))

    @app.get('/api/books/<book_id>/search')
    def books_search(book_id):
        started = time.perf_counter()
        limit = min(max(request.args.get('limit', 5, type=int) or 5, 1), 10)
        result = service.search(book_id, request.args.get('q', ''), request.args.get('page_id') or None, limit)
        return jsonify(success=True, **result, timing_ms=round((time.perf_counter() - started) * 1000, 1))

    @app.get('/api/books/<book_id>/sessions/<session_id>')
    def books_session(book_id, session_id):
        return jsonify(success=True, session=service.session(book_id, session_id))

    @app.post('/api/books/<book_id>/attachments')
    def books_attachment(book_id):
        upload = request.files.get('file')
        if upload is None:
            raise PipelineError('缺少附件 file')
        return jsonify(success=True, **service.attachment(book_id, upload, request.form.get('kind')))

    @app.post('/api/books/<book_id>/transcribe')
    def books_transcribe(book_id):
        transcribe = service.adapters.get('transcribe')
        if transcribe is None:
            raise PipelineError('未配置语音识别适配器', 503)
        upload = request.files.get('file') or request.files.get('audio')
        if upload is None:
            raise PipelineError('缺少音频附件 file')
        attachment = service.attachment(book_id, upload, 'audio')
        try:
            text = transcribe(str(service.asset(book_id, attachment['ref'])), request.form.get('language', 'zh'))
        except Exception as exc:
            raise PipelineError('语音识别失败，请检查模型与受控服务', 502) from exc
        if not isinstance(text, str) or not text.strip():
            raise PipelineError('未识别到有效语音', 422)
        return jsonify(success=True, text=text.strip(), audio_ref=attachment['ref'], audio_url=attachment['url'])

    return service

from flask import Flask, request, jsonify, send_file, send_from_directory
import requests
import base64
import binascii
import errno
import io
import threading
import time
import os
import shutil
import json
import sys
import webbrowser
import numpy as np

import asyncio
import websockets
from PIL import Image as PILImage
from flask import Response
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pydantic import BaseModel
from typing import Optional, List, Dict
from urllib.parse import unquote, urlsplit
# 关闭Flask/Werkzeug的HTTP请求日志
import logging
import hashlib
from hmac import compare_digest
from ipaddress import ip_address
import math
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
import uuid

from ai_story_generate import GeminiAIStroyWriter, parse_output, Gemini3ProAgent, split_sfx_text, parse_list, parse_story_pages
from prompt_zh import chapter_writer_system, en_chapter_writer_system
from text import OpenAIStoryWriter as SimpleStoryWriter, parse_output as parse_output_simple
from generate_audio import AudioGenerator
from baike_kw_generate import GeminiAIBaikeGene
from exam_paper_generate import GeminiAIPaperGene
from volcano_video import VolcanoVideoGenerator, VolcanoVideoError
from generate_music import MusicGenerator
from generator_tts import TTSGenerator
# 直接使用 gemini_analyze.py（使用本地服务）
from gemini_analyze import ImageObjectAnalyzer
# 使用 ImageCapture 截取帧
from ImageCapture import ImageCapture
from generate_image import OpenAIImageGenerator
from nlu import QAAssistant
from config import *
import config as app_config
import local_tts
import local_asr
import cloud_asr
import volc_asr

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


def _remote_asr(audio_path, language=None):
    """OpenAI 兼容的音频转写接口（/audio/transcriptions），未配置时返回 None。"""
    url = os.environ.get('ASR_API_URL', '').strip()
    if not url:
        return None
    key = os.environ.get('ASR_API_KEY', '').strip()
    model = os.environ.get('ASR_MODEL', 'whisper-1').strip() or 'whisper-1'
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    data = {'model': model}
    if language:
        data['language'] = language if language != 'zh' else 'zh'
    with open(audio_path, 'rb') as audio:
        response = requests.post(url, headers=headers, data=data, timeout=90,
                                 files={'file': (os.path.basename(audio_path), audio, 'audio/wav')})
    if response.status_code != 200:
        raise RuntimeError(f'语音识别接口返回 {response.status_code}')
    body = response.json()
    text = body.get('text') or (body.get('data') or {}).get('text') or ''
    return str(text).strip()


def transcribe_audio(audio_path, language=None):
    """语音识别：优先火山大模型 ASR，其次 AI 接口、云端流式，最后回退本地。"""
    if volc_asr.configured():
        try:
            text = volc_asr.transcribe(audio_path)
            if text:
                return local_asr._to_simplified(text)
        except Exception as e:
            print(f"火山语音识别失败，尝试其他通道: {e}")
    try:
        text = _remote_asr(audio_path, language)
        if text:
            return local_asr._to_simplified(text)
    except Exception as e:
        print(f"AI 语音识别失败，尝试其他通道: {e}")
    if cloud_asr.cloud_asr_configured():
        try:
            text = cloud_asr.transcribe_file(audio_path)
            if text:
                return local_asr._to_simplified(text)
        except Exception as e:
            print(f"云端 ASR 失败，尝试本地识别: {e}")
    if not local_asr.available():
        raise RuntimeError('语音识别服务未配置，且本地识别模型未安装')
    return local_asr.transcribe(audio_path, language=language)

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = BOOK_MAX_UPLOAD_BYTES

if app_config.HOST.lower() not in app_config.LOOPBACK_HOSTS and not app_config.API_TOKEN:
    raise SystemExit('拒绝启动：HOST 监听非回环地址时必须设置 API_TOKEN。')

_WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
_CORS_METHODS = _WRITE_METHODS | {'GET', 'HEAD', 'OPTIONS'}


def _http_origin(url, *, referer=False):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        if not referer and (parsed.path or parsed.query or parsed.fragment):
            return None
        default_port = 443 if parsed.scheme == 'https' else 80
        return parsed.scheme, parsed.hostname, parsed.port if parsed.port is not None else default_port
    except ValueError:
        return None


def _configured_origin(origin):
    return origin is not None and any(origin == _http_origin(value) for value in app_config.CORS_ALLOW_ORIGINS)


def _trusted_request_host(origin):
    return origin is not None and (origin[1] in app_config.LOOPBACK_HOSTS
                                  or app_config.API_TOKEN or _configured_origin(origin))


def _is_loopback_peer(address):
    try:
        peer = ip_address(address)
    except ValueError:
        return False
    return (getattr(peer, 'ipv4_mapped', None) or peer).is_loopback


def _allowed_origin(origin):
    if origin is None:
        return False
    current = _http_origin(request.host_url, referer=True)
    return ((origin == current and _trusted_request_host(current))
            or (current is not None and origin[0] == 'http'
                and origin[1] in app_config.LOOPBACK_HOSTS and origin[2] == current[2])
            or _configured_origin(origin))


def _cors_preflight_method():
    if request.method == 'OPTIONS' and request.url_rule and request.url_rule.provide_automatic_options:
        method = request.headers.get('Access-Control-Request-Method')
        if method in _CORS_METHODS and method in app.create_url_adapter(request).allowed_methods():
            return method
    return None


@app.before_request
def protect_requests():
    is_api = request.path.startswith('/api/')
    token = app_config.API_TOKEN
    # Use WSGI's connection peer, never forwarding headers. A public reverse proxy
    # connecting over loopback must be deployed with API_TOKEN.
    if is_api and not token and not _is_loopback_peer(request.remote_addr):
        return jsonify(success=False, error='无令牌模式仅允许回环连接访问 API'), 403
    # Keep the Host allowlist as a separate browser/DNS rebinding safeguard.
    if is_api and not token and not _trusted_request_host(_http_origin(request.host_url, referer=True)):
        return jsonify(success=False, error='API 请求主机不在白名单内'), 403

    origin = request.headers.get('Origin')
    if request.method in _WRITE_METHODS:
        referer = request.headers.get('Referer')
        if origin is not None:
            allowed = _allowed_origin(_http_origin(origin))
        else:
            allowed = referer is None or _allowed_origin(_http_origin(referer, referer=True))
        if not allowed:
            return jsonify(success=False, error='请求来源不在白名单内'), 403

    # Browsers send preflights without credentials. Endpoint capability checks apply to the actual request.
    if _cors_preflight_method() and origin and _allowed_origin(_http_origin(origin)):
        return app.make_default_options_response()

    if token and is_api:
        scheme, _, bearer = request.headers.get('Authorization', '').partition(' ')
        candidates = [request.headers.get('X-API-Token', '')]
        if scheme.lower() == 'bearer':
            candidates.append(bearer.strip())
        if not any(compare_digest(token.encode('utf-8'), value.encode('utf-8')) for value in candidates):
            return jsonify(success=False, error='API token 缺失或无效'), 401, {'WWW-Authenticate': 'Bearer'}


@app.after_request
def add_cors_headers(response):
    response.vary.add('Origin')
    origin = request.headers.get('Origin')
    if origin and _allowed_origin(_http_origin(origin)):
        response.headers['Access-Control-Allow-Origin'] = origin
        method = _cors_preflight_method()
        if method and response.status_code < 300:
            response.headers['Access-Control-Allow-Methods'] = method
            response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-API-Token'
            response.vary.add('Access-Control-Request-Method')
    return response


@app.before_request
def require_legacy_capability():
    required = {
        '/api/generate-story': 'story', '/api/generate-story-stream': 'story',
        '/api/generate-image': 'image', '/api/generate-music': 'music',
        '/api/generate-audio': 'tts', '/api/generate-sfx': 'sfx',
        '/api/analyze-image': 'vision', '/api/analyze-video-frame': 'vision',
        '/api/nlu': 'answer', '/api/asr': 'asr', '/api/tts': 'tts',
        '/api/generate-video': 'video',
    }.get(request.path)
    if request.path.startswith('/api/books/') and request.path.endswith('/transcribe'):
        required = 'asr'
    if required and not get_capabilities().get(required, False):
        return jsonify({'success': False, 'error': f'{required} 能力未配置或本机依赖未就绪',
                        'code': 'capability_not_configured', 'capability': required}), 503


@app.errorhandler(413)
def upload_too_large(error):
    return jsonify({'success': False, 'error': '上传文件超过本机配置的大小限制'}), 413


class _PrefixStripMiddleware:
    """兼容前端使用的 /tts-story-book/api/... 前缀，内部仍匹配 /api/... 路由。"""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip('/')

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.prefix or path.startswith(self.prefix + '/'):
            environ['PATH_INFO'] = path[len(self.prefix):] or '/'
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _PrefixStripMiddleware(app.wsgi_app, '/tts-story-book')

# HTML/CSS/JS and book assets are served by explicit routes only.
# Never expose the source/configuration directory as a generic static folder.



# ========== NLU 问答 API ==========
video_executor = ThreadPoolExecutor(max_workers=5)
## nlu 问答 API生成器实例
# qa_assistant = QAAssistant(base_url=mlops_nlu_url, model=mlops_nlu_model)
# analyzer = ImageObjectAnalyzer(base_url=mlops_image_analysis_url, model=mlops_image_analysis_model)
qa_assistant = QAAssistant(base_url=nlu_url, api_key=nlu_api_key, model=nlu_model)
analyzer = ImageObjectAnalyzer(base_url=image_analysis_url, api_key=image_api_key, model=image_model)

# 视频生成器实例（全局）
video_generator = VolcanoVideoGenerator(base_url=image2video_url, api_key=image2video_api_key, model=image2video_model, verbose=True)
# 全局视频生成线程池（避免每次调用都创建新的线程池）

# 页级视频按原图生成，再由整片导出按配音时长对齐。
VIDEO_DURATION = 5
VIDEO_RESOLUTION = '720p'
VIDEO_RATIO = '16:9'

# 保持绘本风格、避免角色变形的统一提示词后缀
VIDEO_STYLE_SUFFIX = (
    "保持原绘本的绘画风格、角色造型和配色完全一致；角色不要变形，"
    "动效轻微自然，镜头运动缓慢克制；不要添加文字，不要改变画面构图。"
)

# 正在生成中的视频路径，避免同一页被重复提交
_video_inflight = set()
_video_inflight_lock = threading.Lock()
_video_status = {}
_deleted_video_sessions = set()


def _abs_session_dir(session_dir):
    """session_dir 统一转绝对路径。"""
    if not session_dir:
        return session_dir
    base = Path(TARGET_DIR).resolve()
    target = Path(session_dir) if os.path.isabs(session_dir) else base / session_dir
    target = target.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError('会话目录不在配置的数据目录内') from exc
    return str(target)


def _bookshelf_lexical_path(session_dir):
    if not isinstance(session_dir, str) or not session_dir.strip() or '\x00' in session_dir:
        raise ValueError('无效的 session_dir')
    windows_path = PureWindowsPath(session_dir)
    path = Path(session_dir.replace('\\', '/'))
    if '..' in path.parts or ((windows_path.drive or path.anchor) and not path.is_absolute()):
        raise ValueError('会话路径包含不允许的路径形式')
    return Path(TARGET_DIR).absolute() / path


def _checked_bookshelf_entry(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError('会话路径不能经过符号链接或目录联接')
    resolved = Path(os.path.realpath(path))
    if resolved != path:
        raise ValueError('会话路径解析结果与预期不符')
    return resolved


def _bookshelf_path(session_dir, *, shelf_only=False):
    """Accept legacy Windows separators and keep resolved paths within their root."""
    path = _bookshelf_lexical_path(session_dir)
    try:
        configured_base = Path(TARGET_DIR).absolute()
        base = configured_base.resolve()
        shelf_part = ('bookshelf',) if shelf_only else ()
        root = base.joinpath(*shelf_part)
        configured_root = configured_base.joinpath(*shelf_part)
        # Check components before resolve() can access an external drive or UNC share.
        lexical_root = configured_root if path.is_relative_to(configured_root) else root
        relative = path.relative_to(lexical_root)
        if not relative.parts:
            raise ValueError('会话目录必须位于指定目录之下')
        if os.name == 'nt' and any(
            part.endswith(('.', ' ')) or ':' in part or PureWindowsPath(part).is_reserved()
            for part in relative.parts
        ):
            raise ValueError('会话目录包含无效的 Windows 路径组件')

        candidate = base
        for part in shelf_part + relative.parts:
            candidate = _checked_bookshelf_entry(candidate / part)
        return candidate
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError('会话目录不在允许的数据目录内') from exc


def _check_bookshelf_contents(directory):
    # 尽力而为：校验与使用同一解析路径仍是分开的系统调用，无法阻止外部
    # 进程在两者之间替换目录项。读取和复制还会在访问各路径时重新检查。
    directory = _checked_bookshelf_entry(directory)
    def raise_walk_error(error):
        raise error

    for parent, dirs, files in directory.walk(on_error=raise_walk_error):
        for name in dirs + files:
            _checked_bookshelf_entry(parent / name)


def _check_bookshelf_copy_entries(directory, names):
    directory = _checked_bookshelf_entry(Path(directory))
    for name in names:
        _checked_bookshelf_entry(directory / name)
    return []


def _copy_bookshelf_file(source, destination):
    source = _checked_bookshelf_entry(Path(source))
    shutil.copy2(source, destination, follow_symlinks=False)


class VideoSessionRemoved(Exception):
    pass


def _uses_bookshelf_session(directory):
    shelf = Path(TARGET_DIR).resolve() / 'bookshelf'
    return directory != shelf and directory.is_relative_to(shelf)


def _bookshelf_video_identity(directory):
    stat = directory.stat()
    shelf = Path(TARGET_DIR).resolve() / 'bookshelf'
    book = shelf / directory.relative_to(shelf).parts[0]
    try:
        with (book / '.bookshelf-id').open('rb') as marker:
            generation = marker.read(32)
    except FileNotFoundError:
        generation = None  # Older books have no marker.
    return stat.st_dev, stat.st_ino, generation


@contextmanager
def _video_session_guard(directory, expected_identity=None):
    if not _uses_bookshelf_session(directory):
        if expected_identity is not None:
            raise VideoSessionRemoved('书架绘本已删除或替换')
        yield None
        return

    # Use the deletion lock across both the identity check and the write.
    with _bookshelf_transaction():
        try:
            identity = _bookshelf_video_identity(directory)
        except FileNotFoundError as exc:
            raise VideoSessionRemoved('书架绘本已删除') from exc
        if (directory, identity) in _deleted_video_sessions or (
            expected_identity is not None and identity != expected_identity
        ):
            raise VideoSessionRemoved('书架绘本已删除或替换')
        yield identity


def build_page_video_prompt(page_text):
    """按每页文字生成提示词，统一追加风格/不变形约束。"""
    import re as _re
    text = _re.sub(r'<SFX>.*?</SFX>', '', page_text or '').strip()
    text = _re.sub(r'\s+', ' ', text).rstrip('。！？!?')
    if text:
        return f"以这张绘本插图为第一帧，表现这一页的内容：{text[:100]}。{VIDEO_STYLE_SUFFIX}"
    return f"以这张绘本插图为第一帧，让画面轻微、自然地动起来。{VIDEO_STYLE_SUFFIX}"


def _video_task_runner(prompt, image_path, session_dir, paragraph_index, force, video_path, identity):
    """线程池包装：任务结束后清理 in-flight 标记。"""
    try:
        with _video_session_guard(session_dir, identity):
            _video_status[video_path] = {'status': 'running', 'error': None, 'identity': identity}
        result = generate_video_task(prompt, image_path, session_dir, paragraph_index,
                                     force=force, session_identity=identity)
        with _video_session_guard(session_dir, identity):
            _video_status[video_path] = {'status': 'succeeded', 'error': None, 'identity': identity}
        return result
    except VideoSessionRemoved:
        return None
    except BookshelfLockUnavailable:
        raise
    except Exception as exc:
        try:
            with _video_session_guard(session_dir, identity):
                _video_status[video_path] = {'status': 'failed', 'error': str(exc), 'identity': identity}
        except VideoSessionRemoved:
            return None
        raise
    finally:
        with _video_inflight_lock:
            _video_inflight.discard((video_path, identity))


def enqueue_video_task(prompt, image_path, session_dir, paragraph_index, force=False):
    """
    提交单页视频任务：已存在则跳过（缓存），生成中则去重，否则入队。

    Returns: 'cached' / 'running' / 'queued'
    """
    session_dir = Path(_abs_session_dir(session_dir))
    video_path = session_dir / 'videos' / f'video_{paragraph_index}.mp4'
    with _video_session_guard(session_dir) as identity:
        if not force and _legacy_video_cached(prompt, image_path, str(video_path)):
            return 'cached'
        task_key = (video_path, identity)
        with _video_inflight_lock:
            if task_key in _video_inflight:
                return 'running'
            _video_inflight.add(task_key)
        previous_status = _video_status.get(video_path)
        _video_status[video_path] = {'status': 'queued', 'error': None, 'identity': identity}
        try:
            video_executor.submit(_video_task_runner, prompt, image_path, session_dir,
                                  paragraph_index, force, video_path, identity)
        except Exception:
            with _video_inflight_lock:
                _video_inflight.discard(task_key)
            if previous_status is None:
                _video_status.pop(video_path, None)
            else:
                _video_status[video_path] = previous_status
            raise
        return 'queued'


def discover_session_pages(session_dir):
    """扫描绘本目录：images/pageN.(png|jpg) 按页码排序，提示词取 story.txt 对应段落。"""
    import re as _re
    abs_dir = _abs_session_dir(session_dir)
    images_dir = os.path.join(abs_dir, 'images')
    if not os.path.isdir(images_dir):
        return []

    candidates = {}
    for name in os.listdir(images_dir):
        lower = name.lower()
        if not lower.endswith(('.png', '.jpg', '.jpeg')):
            continue
        match = _re.search(r'(\d+)', name)
        if not match:
            continue
        page_no = int(match.group(1))
        # 同一页同时存在 png/jpg 时优先 png
        if page_no in candidates and lower.endswith('.png'):
            candidates[page_no] = name
        elif page_no not in candidates:
            candidates[page_no] = name

    texts = []
    story_file = os.path.join(abs_dir, 'story', 'story.txt')
    if os.path.isfile(story_file):
        with open(story_file, 'r', encoding='utf-8') as f:
            texts = [p.strip() for p in f.read().split('\n\n') if p.strip()]

    pages = []
    for order, page_no in enumerate(sorted(candidates)):
        image_path = os.path.join(images_dir, candidates[page_no])
        pages.append({
            'index': order,
            'image_path': image_path,
            'prompt': texts[order] if order < len(texts) else '',
        })
    return pages


def submit_videos_for_session(session_dir, pages=None, force=False):
    """按页循环提交视频任务（默认扫描整个绘本），返回提交/跳过明细。"""
    abs_dir = _abs_session_dir(session_dir)
    if not os.path.isdir(abs_dir):
        raise VolcanoVideoError(f'绘本目录不存在：{session_dir}')

    if pages is None:
        pages = discover_session_pages(abs_dir)

    result = {'queued': [], 'cached': [], 'running': [], 'errors': []}
    for i, page in enumerate(pages):
        try:
            index = int(page.get('index', i))
            image_path = page.get('image_path') or page.get('imagePath')
            if not image_path:
                result['errors'].append({'index': index, 'error': '缺少 image_path'})
                continue
            if not os.path.isabs(image_path):
                image_path = os.path.join(TARGET_DIR, image_path)
            if not os.path.isfile(image_path):
                result['errors'].append({'index': index, 'error': f'图片不存在: {image_path}'})
                continue
            prompt = page.get('prompt') or page.get('text') or ''
            status = enqueue_video_task(prompt, image_path, abs_dir, index, force=force)
            result[status].append(index)
        except BookshelfLockUnavailable:
            raise
        except Exception as e:
            result['errors'].append({'index': i, 'error': str(e)})

    return result

# 音效生成器实例
sfx_generator = AudioGenerator(base_url=text2audio_url, api_key=text2audio_api_key, model=text2audio_model, verbose=False)
music_generator = MusicGenerator(base_url=text2music_url, api_key=text2music_api_key, model=text2music_model, verbose=False)
image_generator = OpenAIImageGenerator(base_url=text2image_url, api_key=text2image_api_key, model=text2image_model)

simple_story_writer = SimpleStoryWriter(base_url=simple_story_writer_url, api_key=simple_story_writer_api_key, model=simple_story_writer_model)

# 初始化试卷生成器
exam_generator = GeminiAIPaperGene(base_url=simple_story_writer_url, api_key=simple_story_writer_api_key, model=simple_story_writer_model)
# 百科问答生成器实例
baike_generator = GeminiAIBaikeGene(base_url=simple_story_writer_url, api_key=simple_story_writer_api_key, model=simple_story_writer_model)

deep_story_writer = GeminiAIStroyWriter(base_url=simple_story_writer_url, api_key=simple_story_writer_api_key, model=simple_story_writer_model)
                
cn_chapter_writer = Gemini3ProAgent(base_url=simple_story_writer_url, api_key=simple_story_writer_api_key, model=simple_story_writer_model, system_prompt=chapter_writer_system, track_history=False)
en_chapter_writer = Gemini3ProAgent(base_url=simple_story_writer_url, api_key=simple_story_writer_api_key, model=simple_story_writer_model, system_prompt=en_chapter_writer_system, track_history=False)

# 输出目录
OUTPUT_DIR = f"{TARGET_DIR}/output_dir"
BOOKSHELF_FILE = f"{TARGET_DIR}/bookshelf.json"
PROMPT_DIR = os.path.join(TARGET_DIR, "prompt_dir")
os.makedirs(PROMPT_DIR, exist_ok=True)
VOICE_CLONE_TTS_API_URL = globals().get('voice_clone_tts_api_url', tts_api_url)

class SpeakInfo(BaseModel):
    voice_type: str = "xiaosi"
    emotion: str = "neutral"
    lang: str = "cn"
    audio_format: str = "wav"
    rate: str = "1"
    pitch: str = "1"
    volume: str = "100"
    tts_model: str = "v2_llm"
    voice_clone: str = "false"
    text_prompt: Optional[str] = None
    audio_prompt: Optional[List[int]] = None


# 音色映射
VOICE_MAPPING = {
    'xiaosi': 'xiaosi',
    'xiaoyuan': 'tianmeixiaoyuan',
    'M001': 'M001',
    'F001': 'F001',
    'niudun': 'niudun',
    'oula': 'oula',
    # 英文音色
    'en_thinkie': 'thinkie',
    'en_F001': 'F001',
    'en_F002': 'F002',
}


def get_lang_by_voice(voice: str) -> str:
    """根据选择的音色决定语言参数"""
    return "en" if voice and voice.startswith("en") else "cn"


def resolve_voice_type(voice: str) -> str:
    """解析音色：优先映射内置音色，其次尝试自定义音色"""
    if voice in VOICE_MAPPING:
        return VOICE_MAPPING[voice]
    if voice:
        custom_path = os.path.join(PROMPT_DIR, f"{voice}.json")
        if os.path.exists(custom_path):
            return voice
    return VOICE_MAPPING.get('xiaosi', 'xiaosi')


def is_custom_voice(voice: str) -> bool:
    if not voice:
        return False
    custom_path = os.path.join(PROMPT_DIR, f"{voice}.json")
    return os.path.exists(custom_path)


def load_custom_voice_profile(voice: str) -> Optional[dict]:
    if not voice:
        return None
    profile_path = os.path.join(PROMPT_DIR, f"{voice}.json")
    if not os.path.exists(profile_path):
        return None
    try:
        with open(profile_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        text_prompt = data.get('text', '')
        token = data.get('token', [])
        audio_prompt = [int(x) for x in token]
        return {
            'text_prompt': text_prompt,
            'audio_prompt': audio_prompt
        }
    except Exception as e:
        print(f"加载自定义音色失败: {e}")
        return None


def generate_custom_audio(text: str, voice: str, output_path: str, lang: str = "cn") -> Optional[str]:
    profile = load_custom_voice_profile(voice)
    if not profile:
        return None
    if not output_path:
        fallback_dir = os.path.join(TARGET_DIR, "history_res")
        os.makedirs(fallback_dir, exist_ok=True)
        output_path = os.path.join(fallback_dir, f"custom_{int(time.time())}_{voice}.wav")
    generator = TTSGenerator(
        api_url=VOICE_CLONE_TTS_API_URL,
        text_prompt=profile['text_prompt'],
        audio_prompt=profile['audio_prompt'],
        trace_id=f"custom_{int(time.time())}_{voice}"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    return generator.infer_text(text, output_file=output_path, lang=lang)



def get_session_dir():
    """创建以时间命名的会话目录"""
    
    # timestamp = datetime.now().strftime("%Y%m%d%H%M")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]  # 取前3位毫秒
    session_dir = os.path.join(OUTPUT_DIR, timestamp)

    tts_session_dir = os.path.join(session_dir, "tts")
    music_session_dir = os.path.join(session_dir, "music")
    images_session_dir = os.path.join(session_dir, "images")
    story_session_dir = os.path.join(session_dir, "story")
    combined_session_dir = os.path.join(session_dir, "combined")
    baike_session_dir = os.path.join(session_dir, "baike")
    overlay_session_dir = os.path.join(session_dir, "overlay")
    sfx_session_dir = os.path.join(session_dir, "sfx")
    exam_session_dir = os.path.join(session_dir, "sfx")
    # 创建子目录
    os.makedirs(tts_session_dir, exist_ok=True)
    os.makedirs(music_session_dir, exist_ok=True)
    os.makedirs(images_session_dir, exist_ok=True)
    os.makedirs(story_session_dir, exist_ok=True)
    os.makedirs(combined_session_dir, exist_ok=True)
    os.makedirs(baike_session_dir, exist_ok=True)
    os.makedirs(overlay_session_dir, exist_ok=True)
    os.makedirs(sfx_session_dir, exist_ok=True)
    os.makedirs(exam_session_dir, exist_ok=True)
    
    return session_dir, tts_session_dir, music_session_dir, images_session_dir, story_session_dir, combined_session_dir, baike_session_dir, overlay_session_dir, sfx_session_dir, exam_session_dir

@app.route('/tts_v2.html')
@app.route('/tts-story-book/tts_v2.html')
def serve_tts_html():
    return send_from_directory('.', 'tts_v2.html')


@app.route('/voice_register.html')
@app.route('/tts-story-book/voice_register.html')
def serve_voice_register_html():
    return send_from_directory('.', 'voice_register.html')


@app.route('/favicon.ico')
def favicon():
    """处理favicon请求（不带前缀）"""
    return '', 204


@app.route('/tts-story-book/favicon.ico')
def favicon_with_prefix():
    """处理带部署前缀的favicon请求"""
    return '', 204


@app.route('/api/enroll', methods=['POST'])
def enroll_voice():
    """自定义音色注册"""
    try:
        audio_file = request.files.get('audio')
        prompt_text = request.form.get('text')
        spkname = request.form.get('spkname', 'judy')

        if not audio_file or not prompt_text:
            return jsonify({"code": 400, "message": "缺少音频或文本"}), 400

        audio_bytes = audio_file.read()
        audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')

        request_data = {
            'prompt_audio': audio_base64,
            'prompt_text': prompt_text,
            'request_id': f'web_{int(time.time())}',
            'mode': 'enroll',
            'config': {
                'min_mos_score': 2.7,
                'max_wer': 0.15,
                'min_speed': 2.0,
                'max_speed': 8.0,
            }
        }

        response = requests.post(f"{ENROLL_SERVER_URL}/enroll", json=request_data, timeout=60)

        logger.info('音色注册上游状态 %s，响应字节数 %s', response.status_code, len(response.content))
        if response.status_code == 200:
            result = response.json()
            # 保存 token
            token = result.get('token')
            save_path = ""
            if token:
                save_path = f"{PROMPT_DIR}/{spkname}.json"
                payload = {"text": prompt_text, "token": [str(int(x)) for x in token]}
                with open(save_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                result['save_status'] = f"已保存到 {save_path}"
            else:
                logger.warning('音色注册上游响应缺少 token 字段')
                result['save_status'] = "未保存：返回结果中缺少 token 字段"
            
            return jsonify(result)
        else:
            return jsonify({"code": response.status_code, "message": response.text}), response.status_code


        return jsonify({"code": response.status_code, "message": response.text}), response.status_code
    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


@app.route('/api/custom-voices', methods=['GET'])
def list_custom_voices():
    """获取自定义音色列表"""
    try:
        if not os.path.exists(PROMPT_DIR):
            return jsonify({'success': True, 'voices': []})
        voices = []
        for filename in os.listdir(PROMPT_DIR):
            if filename.endswith('.json'):
                voices.append(os.path.splitext(filename)[0])
        voices.sort()
        return jsonify({'success': True, 'voices': voices})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

_bookshelf_lock = threading.RLock()
_BOOKSHELF_LOCK_TIMEOUT = 5.0


class BookshelfLockUnavailable(Exception):
    pass


@contextmanager
def _bookshelf_transaction():
    """Serialize access to the index across service processes on one host."""
    try:
        lock = (Path(TARGET_DIR) / '.bookshelf.lock').open('a+b')
    except OSError as exc:
        raise BookshelfLockUnavailable('无法打开书架锁文件') from exc
    with lock:
        if os.name == 'nt':
            import msvcrt

            # Windows allows a byte-range lock beyond the end of an empty file.
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + _BOOKSHELF_LOCK_TIMEOUT
        while True:
            try:
                lock.seek(0)
                acquire()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK}:
                    raise BookshelfLockUnavailable('无法获取书架文件锁') from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BookshelfLockUnavailable('等待书架文件锁超时，请稍后重试') from exc
                time.sleep(min(0.05, remaining))
        try:
            with _bookshelf_lock:
                yield
        finally:
            lock.seek(0)
            release()


@app.errorhandler(BookshelfLockUnavailable)
def _bookshelf_busy(error):
    return jsonify(success=False, error=str(error)), 503


def load_bookshelf():
    """只有索引缺失时才视为空书架。"""
    with _bookshelf_lock:
        try:
            with open(BOOKSHELF_FILE, 'r', encoding='utf-8') as f:
                bookshelf = json.load(f)
        except FileNotFoundError:
            return []
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OSError('书架索引损坏') from exc
        if not isinstance(bookshelf, list) or any(not isinstance(book, dict) for book in bookshelf):
            raise OSError('书架索引格式无效')
        return bookshelf


def save_bookshelf(bookshelf):
    """在同一目录写完并关闭临时文件，然后原子替换索引。"""
    index = Path(BOOKSHELF_FILE)
    temporary_path = None
    with _bookshelf_lock:
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=index.parent,
                                    prefix=f'.{index.name}.', suffix='.tmp', delete=False) as f:
                temporary_path = Path(f.name)
                json.dump(bookshelf, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, index)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)



def infer_text(tts_api_url, text, voice_params=None, trace_id="test", max_retries=2, save_path=None):
    """调用TTS接口生成语音（带重试机制）"""
    if voice_params is None:
        voice_params = {
            "voice_type": "xiaosi",
            "emotion": "neutral",
            "lang": "cn",
            "audio_format": "wav",
            "rate": "1",
            "pitch": "1",
            "volume": "100",
        }
    
    # 如果voice_params是SpeakInfo对象，转换为字典
    if hasattr(voice_params, 'model_dump'):
        voice_params = voice_params.model_dump()

    # 构建请求数据
    request_data = {"text": text, "voice_params": voice_params, "trace_id": trace_id}

    # 重试机制
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                print(f"==>> TTS 重试第 {attempt} 次...")
            
            # 发送 POST 请求到服务器
            start_response = time.time()
            response = requests.post(tts_api_url, json=request_data, timeout=30)  # 120秒超时
            
            end_response = time.time()
            print(f"==>> TTS response time: {end_response - start_response:.2f}秒")

            # 解析响应数据
            response_data = response.json()

            base64_data = response_data.get("data")
            if base64_data is None:
                raise Exception("No audio data in response")

            data = base64.b64decode(base64_data)

            # 保存音频文件（如果传入 save_path 就直接落到目标目录）
            target_path = save_path or f"{TARGET_DIR}/output_{int(time.time())}_{voice_params['emotion']}.{voice_params['audio_format']}"
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "wb") as f:
                print(f"==>> save_path: {target_path}")
                f.write(data)

            end = time.time()
            print(f"Total TTS time: {end - start_response:.2f}秒")

            return target_path, response_data
        
        except requests.exceptions.Timeout as e:
            print(f"TTS 请求超时 (尝试 {attempt + 1}/{max_retries + 1}): {str(e)}")
            if attempt >= max_retries:
                raise Exception(f"TTS 请求超时，已重试 {max_retries} 次")
            time.sleep(0.5)  # 等待2秒后重试
        
        except Exception as e:
            print(f"Error in infer_text (尝试 {attempt + 1}/{max_retries + 1}): {str(e)}")
            if attempt >= max_retries:
                raise
            time.sleep(0.5)  # 等待2秒后重试


@app.route('/api/generate-story-stream', methods=['POST'])
def generate_story_stream():
    """流式生成故事接口 - 每生成一个段落就返回"""
    
    data = request.json
    story_topic = data.get('story_topic', '')
    main_role = data.get('main_role', '')
    scene = data.get('scene', '')
    deep_think = data.get('deep_think', False)
    lang = data.get('lang', 'cn')
    knowledge_points = data.get('knowledge_points', '')

    language_dict = {"cn": "中文", "en":"英文"}
    language = language_dict.get(lang, "cn")

    
    
    if not story_topic or not main_role or not scene:
        return jsonify({'error': '请填写完整的故事参数'}), 400
    
    def generate():
        try:
            if deep_think:
                # 深度思考模式：逐章节生成
                print("🧠 [流式] 启用深度思考模式...")
                
                if language == "中文":
                    chapter_writer = cn_chapter_writer
                else:
                    chapter_writer = en_chapter_writer

                # 先生成大纲
                params = {
                    "story_title": story_topic,
                    "main_role": main_role,
                    "scene": scene,
                    "knowledge_points": knowledge_points,  # 新增：知识点参数
                    "language": language
                }
                outline = deep_story_writer.generate_outline(params)
                total_chapters = len(outline.get("story_outline", []))
                
                # 发送大纲信息
                yield f"data: {json.dumps({'type': 'outline', 'total': total_chapters})}\n\n"
                
                # 逐章节生成并立即返回
                
                all_pages = []
                for idx, chapter in enumerate(outline["story_outline"]):
                    print(f"   生成第 {idx + 1}/{total_chapters} 章...")

                    chapter_detail, success = chapter_writer.call(
                        json.dumps({
                            "completed_story": all_pages,
                            "current_chapter": chapter
                        }, ensure_ascii=False),
                        success_check_fn=parse_list
                    )
                    
                    if not success:
                        raise RuntimeError(f'第 {idx + 1} 章生成失败，重试预算已用尽')
                    if success:
                        pages = parse_story_pages(chapter_detail)
                        page_content = "".join(pages)
                        all_pages.append(page_content)
                        
                        # 解析这一章的内容
                        parsed_items = split_sfx_text(page_content)
                        pure_text = "".join([item[0] for item in parsed_items if item[1] == 0])
                        
                        # 立即返回这一章
                        yield f"data: {json.dumps({'type': 'chapter', 'index': idx, 'content': page_content, 'pure_text': pure_text, 'parsed_items': parsed_items}, ensure_ascii=False)}\n\n"
                
                # 发送完成信号
                yield f"data: {json.dumps({'type': 'complete', 'total': len(all_pages)})}\n\n"
                
            else:
                # 简易模式：一次性生成（无法流式）
                print("📝 [流式] 简易模式生成故事...")
                
                result = simple_story_writer.call({
                    "story_topic": story_topic,
                    "main_role": main_role,
                    "scene": scene,
                    "knowledge_points": knowledge_points,  # 新增：知识点参数
                    "language": language
                })
                
                # 逐段返回
                parsed_pages = parse_output_simple(result['pages'])
                total = len(parsed_pages)
                
                yield f"data: {json.dumps({'type': 'outline', 'total': total})}\n\n"
                
                for page_num in sorted(parsed_pages.keys(), key=lambda x: int(x)):
                    page_items = parsed_pages[page_num]
                    pure_text = "".join([item[0] for item in page_items if item[1] == 0])
                    
                    yield f"data: {json.dumps({'type': 'chapter', 'index': int(page_num) - 1, 'pure_text': pure_text, 'parsed_items': page_items}, ensure_ascii=False)}\n\n"
                
                yield f"data: {json.dumps({'type': 'complete', 'total': total})}\n\n"
                
        except Exception as e:
            print(f"流式生成错误: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/generate-story', methods=['POST'])
def generate_story():
    """生成故事接口"""
    try:
        data = request.json
        story_topic = data.get('story_topic', '')
        main_role = data.get('main_role', '')
        scene = data.get('scene', '')
        deep_think = data.get('deep_think', False)
        lang = data.get('lang', 'cn')
        knowledge_points = data.get('knowledge_points', '')
        
        language_dict = {"cn": "中文", "en":"英文"}
        language = language_dict.get(lang, "中文")
        
        if not story_topic or not main_role or not scene:
            return jsonify({'error': '请填写完整的故事参数'}), 400
        
        if deep_think:
            # 深度思考模式：使用 GeminiAIStroyWriter
            print("🧠 启用深度思考模式...")
            
            # 生成故事
            pages_raw = deep_story_writer.call({
                "story_title": story_topic,
                "main_role": main_role,
                "scene": scene,
                "knowledge_points": knowledge_points,  # 新增：知识点参数
                "language": language
            })
            
            # 解析输出，获取带有音效标记的格式
            # 格式: {'1': [(text, 0/1), ...], '2': [...], ...}
            parsed_pages = parse_output(pages_raw)
            
            # 提取纯文本内容（type=0的文本），用于生成图片和背景音乐
            pure_text_pages = []
            for page_num in sorted(parsed_pages.keys(), key=lambda x: int(x)):
                page_items = parsed_pages[page_num]
                # 只取type=0的文本（普通文本）
                text_parts = [item[0] for item in page_items if item[1] == 0]
                pure_text_pages.append("".join(text_parts))
            
            # 合并为完整故事内容（纯文本版本）
            content = "\n\n".join(pure_text_pages)
            
            print(f"📖 深度思考模式生成 {len(parsed_pages)} 个段落")
            for page_num, items in parsed_pages.items():
                sfx_count = sum(1 for item in items if item[1] == 1)
                text_count = sum(1 for item in items if item[1] == 0)
                print(f"   段落{page_num}: {text_count}段文本, {sfx_count}个音效")
            
            return jsonify({
                'success': True,
                'story_content': content,
                'content': content,
                'pages': pure_text_pages,          # 纯文本段落列表，用于图片和背景音乐
                'parsed_pages': parsed_pages,       # 带音效标记的完整格式
                'deep_think': True,
                'message': '故事生成成功（深度思考模式）'
            })
        else:
            # 简易模式：使用 SimpleStoryWriter (text_new.py)
            print("📝 简易模式生成故事...")
            
            # 生成故事
            result = simple_story_writer.call({
                "story_topic": story_topic,
                "main_role": main_role,
                "scene": scene,
                "knowledge_points": knowledge_points,
                "language": language
            })
            
            # 解析输出，获取带有音效标记的格式（和深度思考模式相同）
            parsed_pages = parse_output_simple(result['pages'])
            
            # 提取纯文本内容（type=0的文本），用于生成图片和背景音乐
            pure_text_pages = []
            for page_num in sorted(parsed_pages.keys(), key=lambda x: int(x)):
                page_items = parsed_pages[page_num]
                # 只取type=0的文本（普通文本）
                text_parts = [item[0] for item in page_items if item[1] == 0]
                pure_text_pages.append("".join(text_parts))
            
            # 合并为完整故事内容（纯文本版本）
            content = "\n\n".join(pure_text_pages)
            
            print(f"📖 简易模式生成 {len(parsed_pages)} 个段落")
            for page_num, items in parsed_pages.items():
                sfx_count = sum(1 for item in items if item[1] == 1)
                text_count = sum(1 for item in items if item[1] == 0)
                print(f"   段落{page_num}: {text_count}段文本, {sfx_count}个音效")
            
            return jsonify({
                'success': True,
                'story_content': content,
                'content': content,
                'pages': pure_text_pages,           # 纯文本段落列表，用于图片和背景音乐
                'parsed_pages': parsed_pages,       # 带音效标记的完整格式
                'deep_think': False,                # 标记为简易模式
                'message': '故事生成成功（简易模式）'
            })
    
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/generate-image', methods=['POST'])
def generate_image_api():
    """单独生成图片接口 - 支持吉卜力风格图片生成"""
    try:
        data = request.json
        prompt = data.get('prompt', '')
        index = data.get('index', 0)
        story_title = data.get('story_title', '儿童故事')
        main_role = data.get('main_role', '可爱的小动物')
        scene = data.get('scene', '美丽的大自然')
        prev_image_path = data.get('prev_image_path', None)
        session_dir = data.get('session_dir', None)
        
        if not prompt:
            return jsonify({'error': '请提供图片生成提示'}), 400
        
        print(f"\n🎨 [图片生成] 开始生成第 {index + 1} 段的吉卜力风格图片")
        print(f"   故事: {story_title}, 主角: {main_role}, 场景: {scene}")
        print(f"   内容: {prompt[:50]}...")
        
        
        # 读取上一张图片的 base64（如果有）
        prev_image_base64 = None
        if prev_image_path and os.path.exists(prev_image_path):
            try:
                with open(prev_image_path, 'rb') as f:
                    prev_image_base64 = base64.b64encode(f.read()).decode('utf-8')
                print(f"   使用上一张图片保持风格一致性: {prev_image_path}")
            except Exception as e:
                print(f"   读取上一张图片失败: {e}")
        
        
        
        # 确保session_dir是绝对路径
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        
        # 为单段生成一张吉卜力风格图片
        result = image_generator.call({
            "story_text": prompt,
            "story_title": story_title,
            "main_role": main_role,
            "scene": scene,
            "prev_image_base64": prev_image_base64,
            "paragraph_index": index,
            "session_dir": session_dir
        })
        
        image_paths = result.get('image_paths', [])
        image_path = image_paths[0] if image_paths else None
        
        if image_path:
            print(f"   ✅ 第 {index + 1} 段图片生成成功: {image_path}")
            
            # 图片生成成功后，立即在后台启动视频生成
            if session_dir and os.path.exists(image_path):
                print(f"   🎬 立即启动第 {index + 1} 段视频生成...")
                try:
                    status = enqueue_video_task(prompt, image_path, session_dir, index)
                    print(f"   ✅ 第 {index + 1} 段视频任务: {status}\n")
                except Exception as ve:
                    print(f"   ⚠️ 第 {index + 1} 段视频生成任务提交失败: {ve}\n")
            else:
                print(f"   ⚠️ 跳过视频生成（session_dir: {session_dir}, 图片存在: {os.path.exists(image_path) if image_path else False}）\n")
        else:
            print(f"   ⚠️ 第 {index + 1} 段图片生成未返回路径\n")
            return jsonify({'success': False, 'error': '图片生成没有返回可用文件'}), 502
        
        # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
        relative_image_path = image_path.replace(f'{TARGET_DIR}/', '') if image_path and image_path.startswith(f'{TARGET_DIR}/') else image_path
        
        return jsonify({
            'success': True,
            'image_path': relative_image_path,
            'message': '图片生成成功'
        })
    
    except Exception as e:
        print(f"   ❌ 第 {index + 1} 段图片生成失败: {str(e)}\n")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/generate-music', methods=['POST'])
def generate_music_api():
    """单独生成音乐接口"""
    
    # 音乐生成开关（如果遇到问题可以改为 False）
    ENABLE_MUSIC = True
    
    if not ENABLE_MUSIC:
        print(f"⚠️  [音乐生成] 已禁用，跳过")
        return jsonify({
            'success': True,
            'music_path': None,
            'message': '音乐生成已禁用'
        })
    
    try:
        data = request.json
        prompt = data.get('prompt', '')
        session_dir = data.get('session_dir', None)
        index = data.get('index', 0)
        duration = data.get('duration', 30)  # 支持自定义时长，默认30秒
        
        if not prompt:
            return jsonify({'error': '请提供音乐生成提示'}), 400
        
        print(f"\n🎵 [音乐生成] 开始生成音乐")
        print(f"   提示词: {prompt[:50]}...")
        print(f"   时长: {duration}秒")
        
        # 确保session_dir是绝对路径，并提前确定目标落盘路径
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        target_music_path = None
        if session_dir:
            music_dir = os.path.join(session_dir, "music")
            os.makedirs(music_dir, exist_ok=True)
            target_music_path = os.path.join(music_dir, f"music_{index}.wav")
        
        music_path = music_generator.generate(
            story_content=prompt,
            duration=int(duration),  # 使用传入的时长
            seed=int(time.time()) % 1000,
            output_file=target_music_path  # 直接写入目标目录
        )
        
        if music_path:
            print(f"   ✅ 音乐生成成功: {music_path}\n")
            # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
            relative_music_path = music_path.replace(f'{TARGET_DIR}/', '') if music_path.startswith(f'{TARGET_DIR}/') else music_path
            return jsonify({
                'success': True,
                'music_path': relative_music_path,
                'message': '音乐生成成功'
            })
        else:
            print(f"   ⚠️  音乐生成失败，返回空路径\n")
            # 返回成功但音乐路径为空，让前端可以继续
            return jsonify({
                'success': True,
                'music_path': None,
                'message': '音乐生成失败，已跳过'
            })
    
    except Exception as e:
        print(f"   ❌ 音乐生成异常: {str(e)}\n")
        import traceback
        traceback.print_exc()
        # 返回成功但音乐路径为空，让前端可以继续
        return jsonify({
            'success': True,
            'music_path': None,
            'message': f'音乐生成失败: {str(e)}'
        })


@app.route('/api/generate-audio', methods=['POST'])
def generate_audio_api():
    """单独生成语音接口"""
    try:
        data = request.json
        text = data.get('text', '')
        voice = data.get('voice', 'xiaosi')
        session_dir = data.get('session_dir', None)
        index = data.get('index', 0)
        
        if not text:
            return jsonify({'error': '请提供要转换的文本'}), 400
        
        print(f"🎤 [语音生成] 开始生成语音")
        print(f"   文本: {text[:50]}...")
        print(f"   音色: {voice}")
        
        lang = get_lang_by_voice(voice)
        voice_type = resolve_voice_type(voice)

        # 构建speak标签格式的文本
        speak_text = f'<speak voice_type="{voice_type}" emotion="neutral" volume="90" rate="1.1" pitch="1.0" lang="{lang}" audio_format="wav" tts_model="v2_llm">{text.replace(">"," ").strip()}</speak>'

        # 设置语音参数
        voice_params = SpeakInfo(
            voice_type=voice_type,
            emotion="neutral",
            rate="1.1",
            pitch="1.0",
            volume="90",
            lang=lang,
            audio_format="wav",
            tts_model="v2_llm",
        )
        
        # 生成唯一的trace_id
        trace_id = f"audio_{int(time.time())}_{voice}"
        
        # 确定目标落盘路径（直接写入目标目录，不再搬运）
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        target_audio_path = None
        if session_dir:
            tts_dir = os.path.join(session_dir, "tts")
            os.makedirs(tts_dir, exist_ok=True)
            target_audio_path = os.path.join(tts_dir, f"tts_{index}.wav")
        
        # 自定义音色走克隆TTS流程
        if is_custom_voice(voice):
            audio_path = generate_custom_audio(text, voice, target_audio_path, lang=lang)
            if not audio_path:
                raise Exception("自定义音色生成失败")
        else:
            # The normal voice adapter is the same one used by /api/tts.
            # Optional sound effects and clone endpoints never gate narration.
            audio_path = local_tts.synthesize(text, voice, output_path=target_audio_path, lang=lang)
            wav_duration(audio_path)
        
        print(f"   ✅ 语音生成成功: {audio_path}\n")
        
        # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
        relative_audio_path = Path(audio_path).resolve().relative_to(Path(TARGET_DIR).resolve()).as_posix()
        
        return jsonify({
            'success': True,
            'audio_path': relative_audio_path,
            'audio_url': '/api/audio/' + relative_audio_path,
            'message': '语音生成成功'
        })
    
    except Exception as e:
        print(f"   ❌ 语音生成失败: {str(e)}\n")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/tts', methods=['POST'])
def generate_tts():
    """TTS生成接口（原有功能）"""
    try:
        data = request.json
        text = data.get('text', '')
        voice = data.get('voice', 'xiaomei')
        session_dir = data.get('session_dir', None)
        subfolder = data.get('subfolder', 'tts')  # 支持自定义子文件夹
        index = data.get('index', 0)
        
        if not text:
            return jsonify({'error': '文本不能为空'}), 400
        
        lang = get_lang_by_voice(voice)
        
        # 确定目标落盘路径（直接写入目标目录，不再搬运）
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        target_audio_path = None
        if session_dir:
            target_dir = os.path.join(session_dir, subfolder)  # 默认 tts，小助手用 assistant
            os.makedirs(target_dir, exist_ok=True)
            timestamp = int(time.time() * 1000)
            target_audio_path = os.path.join(target_dir, f"{subfolder}_{timestamp}.wav")
        
        # 本地 edge-tts 合成（免费），保持原有返回结构不变
        if not target_audio_path:
            default_dir = os.path.join(TARGET_DIR, 'history_res')
            os.makedirs(default_dir, exist_ok=True)
            target_audio_path = os.path.join(
                default_dir, f"tts_{int(time.time() * 1000)}_{voice}.wav")
        audio_path = local_tts.synthesize(text, voice, target_audio_path, lang=lang)
        
        # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
        relative_path = audio_path.replace(f'{TARGET_DIR}/', '') if audio_path.startswith(f'{TARGET_DIR}/') else audio_path
        
        return jsonify({
            'success': True,
            'audio_path': relative_path,
            'message': '语音生成成功'
        })
    
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


_AUDIO_EXTENSIONS = {'.wav', '.mp3', '.ogg', '.m4a', '.aac', '.flac', '.opus', '.webm'}
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.avif'}
_VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.avi', '.m4v', '.mkv'}


def _send_legacy_media(root, filepath, extensions):
    try:
        root = Path(root).resolve()
        media_path = (root / unquote(filepath).replace('\\', '/')).resolve()
        media_path.relative_to(root)
        if media_path.suffix.lower() in extensions and media_path.is_file():
            return send_file(media_path)
    except (OSError, ValueError, RuntimeError):
        pass
    return jsonify(error='文件不存在'), 404


@app.route('/api/audio/<path:filepath>')
def get_audio(filepath):
    return _send_legacy_media(TARGET_DIR, filepath, _AUDIO_EXTENSIONS)


@app.route('/api/image/<path:filepath>')
def get_image(filepath):
    return _send_legacy_media(TARGET_DIR, filepath, _IMAGE_EXTENSIONS)


@app.route('/api/video/<path:filepath>')
def get_video(filepath):
    return _send_legacy_media(TARGET_DIR, filepath, _VIDEO_EXTENSIONS)


@app.route('/health')
def health():
    """健康检查"""
    return jsonify({'status': 'ok', 'capabilities': get_capabilities(),
                    'checks': 'configuration_and_local_dependencies', 'online_verified': False})


@app.get('/api/capabilities')
def capabilities_api():
    return jsonify({'success': True, 'capabilities': get_capabilities(),
                    'checks': 'configuration_and_local_dependencies', 'online_verified': False})


# ========== 会话目录管理 API ==========
@app.route('/api/create-session', methods=['POST'])
def create_session():
    """创建新的会话目录"""
    try:
        session_dir, tts_session_dir, music_session_dir, images_session_dir, story_session_dir, combined_session_dir, baike_session_dir, overlay_session_dir, sfx_session_dir, exam_session_dir = get_session_dir()
        # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
        relative_session_dir = session_dir.replace(f'{TARGET_DIR}/', '')
        return jsonify({
            'success': True,
            'session_dir': relative_session_dir
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/save-story-text', methods=['POST'])
def save_story_text():
    """保存故事文本"""
    try:
        data = request.json
        session_dir = data.get('session_dir', '')
        story_text = data.get('story_text', '')
        story_params = data.get('story_params', {})
        
        if not session_dir or not story_text:
            return jsonify({'success': False, 'error': '缺少必要参数'}), 400
        
        # 确保session_dir是绝对路径
        if not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        
        # 保存故事文本
        story_file = os.path.join(session_dir, "story", "story.txt")
        with open(story_file, 'w', encoding='utf-8') as f:
            f.write(story_text)
        
        # 保存故事参数
        params_file = os.path.join(session_dir, "story", "params.json")
        with open(params_file, 'w', encoding='utf-8') as f:
            json.dump(story_params, f, ensure_ascii=False, indent=2)
        
        return jsonify({
            'success': True,
            'story_file': story_file,
            'params_file': params_file
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bookshelf', methods=['GET'])
def get_bookshelf():
    """获取书架列表"""
    try:
        with _bookshelf_transaction():
            bookshelf = load_bookshelf()
        return jsonify({
            'success': True,
            'bookshelf': bookshelf
        })
    except BookshelfLockUnavailable:
        raise
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bookshelf/load', methods=['POST'])
def load_book_from_shelf():
    """从书架加载绘本内容"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error='请求须为 JSON 对象'), 400
    with _bookshelf_transaction():
        return _load_bookshelf_book(data.get('session_dir'))


def _load_bookshelf_book(session_dir):
    try:
        directory = _bookshelf_path(session_dir, shelf_only=True)
        if not directory.is_dir():
            return jsonify({'success': False, 'error': '目录不存在'}), 400
        _check_bookshelf_contents(directory)
        data_root = Path(TARGET_DIR).resolve()
        
        def to_rel_path(p):
            """把绝对路径统一转成相对 TARGET_DIR 的 URL 路径（兼容 Windows 反斜杠）。"""
            return _checked_bookshelf_entry(p).relative_to(data_root).as_posix()
        
        # 读取故事文本
        story_file = _checked_bookshelf_entry(directory / 'story' / 'story.txt')
        story_text = ""
        if os.path.exists(story_file):
            with open(story_file, 'r', encoding='utf-8') as f:
                story_text = f.read()
        
        # 读取故事参数
        params_file = _checked_bookshelf_entry(directory / 'story' / 'params.json')
        story_params = {}
        if os.path.exists(params_file):
            with open(params_file, 'r', encoding='utf-8') as f:
                story_params = json.load(f)
        
        # 获取图片列表
        images_dir = _checked_bookshelf_entry(directory / 'images')
        images = []
        if os.path.exists(images_dir):
            for f in sorted(os.listdir(images_dir)):
                if f.endswith('.png'):
                    # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
                    image_path = images_dir / f
                    relative_path = to_rel_path(image_path)
                    images.append(relative_path)
        
        # 获取TTS列表
        tts_dir = _checked_bookshelf_entry(directory / 'tts')
        # 分段故事（先定义，后面需要使用）
        paragraphs = [p.strip() for p in story_text.split('\n\n') if p.strip()]
        
        # 过滤掉音效标记，生成纯文本段落（用于字幕显示）
        import re
        def remove_sfx_tags(text):
            """移除 <SFX>...</SFX> 标记，只保留纯文本"""
            return re.sub(r'<SFX>.*?</SFX>', '', text).strip()
        
        pure_paragraphs = [remove_sfx_tags(p) for p in paragraphs]
        
        tts_files = []
        if os.path.exists(tts_dir):
            for f in sorted(os.listdir(tts_dir)):
                if f.endswith('.wav'):
                    # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
                    audio_path = tts_dir / f
                    relative_path = to_rel_path(audio_path)
                    tts_files.append(relative_path)
        
        # 获取音乐列表（按段落索引匹配）
        music_dir = _checked_bookshelf_entry(directory / 'music')
        music_files = []
        if os.path.exists(music_dir):
            # 使用已定义的 pure_paragraphs 获取段落数量
            total_paragraphs = len(pure_paragraphs)
            # 按段落索引顺序匹配音乐文件
            for i in range(total_paragraphs):
                music_file = music_dir / f"music_{i}.wav"
                if os.path.exists(music_file):
                    # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
                    relative_path = to_rel_path(music_file)
                    music_files.append(relative_path)
                else:
                    music_files.append(None)  # 该段落没有音乐文件
        
        # 读取试卷数据（如果存在）
        exam_data = None
        exam_file = _checked_bookshelf_entry(directory / 'exam' / 'exam.json')
        if os.path.exists(exam_file):
            try:
                with open(exam_file, 'r', encoding='utf-8') as f:
                    exam_data = json.load(f)
                print(f"   ✅ 加载试卷数据: {len(exam_data)} 道题")
            except Exception as e:
                print(f"   ⚠️ 读取试卷数据失败: {e}")
        
        # 获取合成后的音频文件（combined目录）
        combined_dir = _checked_bookshelf_entry(directory / 'combined')
        combined_files = []
        if os.path.exists(combined_dir):
            for f in sorted(os.listdir(combined_dir)):
                if f.endswith('.wav'):
                    # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
                    audio_path = combined_dir / f
                    relative_path = to_rel_path(audio_path)
                    combined_files.append(relative_path)

        # 获取视频文件列表
        videos_dir = _checked_bookshelf_entry(directory / 'videos')
        video_files = []
        if os.path.exists(videos_dir):
            for f in sorted(os.listdir(videos_dir)):
                if f.endswith('.mp4'):
                    # 将绝对路径转换为相对于 TARGET_DIR 的相对路径
                    video_path = videos_dir / f
                    relative_path = to_rel_path(video_path)
                    video_files.append(relative_path)
        
        # 优先使用combined音频（包含音效），如果没有则用tts音频
        audio_files = combined_files if combined_files else tts_files
        
        # 加载百科数据（用于弹幕显示）
        baike_data = {}
        baike_dir = _checked_bookshelf_entry(directory / 'baike')
        if os.path.exists(baike_dir):
            for f in sorted(os.listdir(baike_dir)):
                if f.endswith('.json') and f.startswith('baike_'):
                    baike_file = _checked_bookshelf_entry(baike_dir / f)
                    try:
                        # 从文件名提取段落索引，如 baike_0.json -> 0
                        idx = int(f.replace('baike_', '').replace('.json', ''))
                        with open(baike_file, 'r', encoding='utf-8') as bf:
                            baike_data[str(idx)] = json.load(bf)
                        print(f"   ✅ 加载百科数据: 段落{idx}, {len(baike_data[str(idx)])} 个问答")
                    except Exception as e:
                        print(f"   ⚠️ 加载百科文件 {f} 失败: {e}")
        
        # 加载用户答案（如果存在）
        exam_result_file = _checked_bookshelf_entry(directory / 'exam' / 'exam_result.json')
        exam_result = None
        if os.path.exists(exam_result_file):
            try:
                with open(exam_result_file, 'r', encoding='utf-8') as f:
                    exam_result = json.load(f)
                print(f"   ✅ 加载用户答案: {len(exam_result.get('user_answers', {}))} 道题")
            except Exception as e:
                print(f"   ⚠️ 读取用户答案失败: {e}")
        
        return jsonify({
            'success': True,
            'story_text': story_text,
            'story_params': story_params,
            'paragraphs': pure_paragraphs,  # 返回过滤后的纯文本段落
            'images': images,
            'tts_files': audio_files,  # 优先使用combined音频
            'music_files': music_files,
            'video_files': video_files,  # 添加视频文件路径
            'exam_data': exam_data,  # 添加试卷数据
            'baike_data': baike_data,  # 添加百科数据（弹幕）
            'exam_result': exam_result  # 添加用户答案和完成状态
        })
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bookshelf', methods=['POST'])
def add_to_bookshelf():
    """添加绘本到书架"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(success=False, error='请求须为 JSON 对象'), 400
        book_name = data.get('book_name', '')
        session_dir = data.get('session_dir', '')
        story_params = data.get('story_params', {})
        
        if not isinstance(book_name, str) or not book_name.strip() or not isinstance(story_params, dict):
            return jsonify({'success': False, 'error': '缺少必要参数'}), 400

        # 清理书名，移除不合法的文件名字符
        safe_book_name = "".join(c for c in book_name if c not in r'\/:*?"<>|' and ord(c) >= 32)
        safe_book_name = safe_book_name.strip().rstrip('. ')
        if not safe_book_name:
            safe_book_name = f"绘本_{time.strftime('%Y%m%d%H%M%S')}"

        with _bookshelf_transaction():
            bookshelf = load_bookshelf()
            source_dir = _bookshelf_path(session_dir)
            if not source_dir.is_dir():
                return jsonify(success=False, error='原始会话目录不存在'), 400
            data_root = Path(TARGET_DIR).resolve()
            bookshelf_dir = _checked_bookshelf_entry(data_root / 'bookshelf')
            if source_dir == bookshelf_dir:
                return jsonify(success=False, error='书架根目录不能作为原始会话'), 400
            _check_bookshelf_contents(source_dir)

            target_dir = bookshelf_dir / safe_book_name
            while (target_dir.is_symlink() or target_dir.is_junction() or target_dir.exists()
                   or any(_same_bookshelf_record(str(target_dir), book.get('session_dir'))
                          for book in bookshelf)):
                target_dir = bookshelf_dir / f'{safe_book_name}_{uuid.uuid4().hex[:8]}'
            target_dir = _bookshelf_path(str(target_dir), shelf_only=True)
            target_dir.parent.mkdir(exist_ok=True)

            new_book = {
                'id': uuid.uuid4().hex,
                'name': book_name,
                'session_dir': target_dir.relative_to(data_root).as_posix(),
                'original_session_dir': session_dir,
                'story_topic': story_params.get('story_topic', ''),
                'main_role': story_params.get('main_role', ''),
                'scene': story_params.get('scene', ''),
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            bookshelf.append(new_book)

            # 尽力而为：逐项检查并避免 copytree 主动跟随符号链接。外部进程仍可
            # 在检查与文件系统调用之间替换路径，临时副本发布前再校验一遍。
            published = False
            try:
                with TemporaryDirectory(prefix='.adding-', dir=target_dir.parent) as staging:
                    staged_book = Path(staging) / 'book'
                    shutil.copytree(_checked_bookshelf_entry(source_dir), staged_book, symlinks=True,
                                    ignore=_check_bookshelf_copy_entries, copy_function=_copy_bookshelf_file)
                    _check_bookshelf_contents(staged_book)
                    (staged_book / '.bookshelf-id').write_text(new_book['id'], encoding='ascii')
                    staged_book.rename(_checked_bookshelf_entry(target_dir))
                    published = True
                save_bookshelf(bookshelf)
            except Exception:
                if published:
                    try:
                        shutil.rmtree(_checked_bookshelf_entry(target_dir))
                    except (OSError, ValueError):
                        logger.exception('清理未登记的书架副本失败')
                raise
        
        return jsonify({
            'success': True,
            'book': new_book
        })
    except BookshelfLockUnavailable:
        raise
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bookshelf/remove', methods=['POST'])
def remove_from_bookshelf():
    """从书架移除绘本"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(success=False, error='请求须为 JSON 对象'), 400
        session_dir = data.get('session_dir')
        delete_files = data.get('delete_files', True)  # 是否同时删除文件
        identity = {field: data[field] for field in ('name', 'id', 'created_at') if field in data}

        if not isinstance(delete_files, bool):
            return jsonify(success=False, error='delete_files 须为布尔值'), 400
        if not isinstance(session_dir, str):
            return jsonify(success=False, error='无效的 session_dir'), 400
        if identity and (not isinstance(identity.get('name'), str)
                         or ('id' in identity and type(identity['id']) not in (int, str))
                         or ('created_at' in identity and not isinstance(identity['created_at'], str))):
            return jsonify(success=False, error='请提供名称及有效的记录 ID 或创建时间'), 400

        with _bookshelf_transaction():
            bookshelf = load_bookshelf()
            legacy_name_only = set(identity) == {'name'}
            # Match the stored value even when the legacy path no longer validates.
            # Compare other spellings lexically to support old Windows records.
            matches = [
                i for i, book in enumerate(bookshelf)
                if _same_bookshelf_record(session_dir, book.get('session_dir'))
                and all(book.get(field) == value for field, value in identity.items())
                and (not legacy_name_only or book.get('id') is None)
            ]
            if not matches:
                if delete_files:
                    _bookshelf_path(session_dir, shelf_only=True)
                return jsonify(success=False, error='未找到该绘本'), 404
            legacy_name_match = ('name' in identity and 'id' not in identity
                                 and all(bookshelf[i].get('id') is None for i in matches))
            if len(matches) > 1 and not legacy_name_match:
                return jsonify(success=False, error='书架路径对应多条记录，请同时提供名称和记录 ID 或创建时间'), 409
            removed = bookshelf.pop(matches[0])

            save_bookshelf(bookshelf)
            if delete_files:
                try:
                    directory = _bookshelf_path(removed['session_dir'], shelf_only=True)
                    if any(_bookshelf_reference_is_within(book.get('session_dir'), directory)
                           for book in bookshelf):
                        return jsonify(success=True, message='绘本已从书架移除',
                                       warning='仍有其他书架记录引用该目录，已保留文件')
                    if not directory.is_dir():
                        raise ValueError('书架路径不是有效目录')
                    _check_bookshelf_contents(directory)
                    # 尽力而为：重新确认已解析路径，外部进程仍可能在
                    # 递归删除前替换目录项。
                    directory = _checked_bookshelf_entry(directory)
                except (RuntimeError, ValueError):
                    return jsonify(success=True, message='绘本已从书架移除',
                                   warning='书架文件路径无效或已失效，未删除任何文件')
                except OSError:
                    logger.exception('检查待删除的书架文件失败')
                    return jsonify(success=False, index_removed=True,
                                   error='绘本已从书架移除，但无法检查或删除文件'), 500
                try:
                    deleted_identity = _bookshelf_video_identity(directory)
                    shutil.rmtree(directory)
                    _deleted_video_sessions.add((directory, deleted_identity))
                    for path in list(_video_status):
                        if path.is_relative_to(directory):
                            _video_status.pop(path, None)
                except OSError:
                    logger.exception('删除书架文件失败')
                    return jsonify(success=False, index_removed=True,
                                   error='绘本已从书架移除，但文件删除失败'), 500
        
        return jsonify({
            'success': True,
            'message': '绘本已从书架移除'
        })
    except BookshelfLockUnavailable:
        raise
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def _same_bookshelf_record(requested, stored):
    if requested == stored:
        return True
    try:
        return _bookshelf_lexical_path(requested) == _bookshelf_lexical_path(stored)
    except ValueError:
        return False


def _bookshelf_reference_is_within(session_dir, directory):
    try:
        path = _bookshelf_lexical_path(session_dir)
    except ValueError:
        return False
    configured_root = Path(TARGET_DIR).absolute()
    if not (path.is_relative_to(configured_root / 'bookshelf')
            or path.is_relative_to(configured_root.resolve() / 'bookshelf')):
        return False
    return path.resolve().is_relative_to(directory)


@app.route('/api/history_res/<path:filepath>')
def get_history_res(filepath):
    return _send_legacy_media(Path(TARGET_DIR) / 'history_res', filepath,
                              _AUDIO_EXTENSIONS | _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS)


# ========== ASR 语音识别 API ==========
async def asr_send_json(websocket, json_data):
    await websocket.send(json.dumps(json_data))

async def asr_send_audio(websocket, audio_chunk):
    await websocket.send(audio_chunk)
    await asyncio.sleep(0.001)

async def asr_send_data(websocket, audio_data, init_json, end_json):
    try:
        if init_json:
            await asr_send_json(websocket, init_json)
        
        audio_chunks = [audio_data[i:i+3840] for i in range(0, len(audio_data), 3840)]
        for audio_chunk in audio_chunks:
            await asr_send_audio(websocket, audio_chunk)
        
        if end_json:
            await asr_send_json(websocket, end_json)
    except websockets.exceptions.ConnectionClosedOK:
        pass

async def asr_receive_data(websocket):
    offline_texts = []
    while True:
        data = await websocket.recv()
        if isinstance(data, str):
            response_json = json.loads(data)
            if response_json["code"] != 0:
                raise Exception(f"ASR error: {response_json}")
            
            if "data" in response_json and "showText" in response_json["data"] and response_json["type"] == "offline_asr":
                offline_text = response_json["data"]["showText"]
                offline_texts.append(offline_text)
            
            if "data" in response_json and response_json["data"]["end"]:
                await websocket.close()
                break
    return offline_texts

async def process_asr(audio_data):
    request_id = f"asr_{int(time.time() * 1000)}"
    
    init_json = {
        "type": "start",
        "request_id": request_id,
        "asr_wake_up_type": "action_wake_up",
        "continuous": "false",
        "oneshot_replace_word": "true",
        "enable_punc": "true",
        "max_start_silence": "7000",
        "max_end_silence": "800"
    }
    end_json = {
        "type": "end",
        "request_id": request_id
    }
    
    try:
        async with websockets.connect(ASR_WS_SERVER_URL, ping_interval=None) as websocket:
            await asyncio.gather(
                asr_send_data(websocket, audio_data, init_json, end_json),
                asr_receive_data(websocket)
            )
            # 重新连接获取结果
            async with websockets.connect(ASR_WS_SERVER_URL, ping_interval=None) as ws2:
                await asr_send_data(ws2, audio_data, init_json, end_json)
                offline_texts = await asr_receive_data(ws2)
                return offline_texts
    except Exception as e:
        print(f"ASR error: {e}")
        return []


@app.route('/api/asr', methods=['POST'])
def asr_recognize():
    """语音识别API - 接收音频文件，返回识别文本"""
    try:
        if 'audio' not in request.files:
            return jsonify({'success': False, 'error': '没有上传音频文件'}), 400
        
        audio_file = request.files['audio']
        
        # 保存临时文件
        temp_path = f"temp_asr_{int(time.time() * 1000)}.wav"
        audio_file.save(temp_path)
        try:
            # 语音识别：优先云端 ASR，未配置时回退本地 faster-whisper
            text = transcribe_audio(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
        return jsonify({
            'success': True,
            'text': text
        })
    except Exception as e:
        print(f"ASR API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500




def overlay_sfx_on_tts(tts_file, sfx_file, output_file, sfx_volume=0.5, max_sfx_duration=3.0):
    """将音效叠加到TTS音频的末尾部分
    
    Args:
        tts_file: TTS音频文件路径
        sfx_file: 音效文件路径
        output_file: 输出文件路径
        sfx_volume: 音效音量（相对于TTS，默认0.5即一半）
        max_sfx_duration: 音效最大时长（秒），超过此时长只取最后这么多秒
        
    Returns:
        bool: 成功返回True，失败返回False
    """
    import wave
    import struct
    import array
    
    try:
        if not os.path.exists(tts_file):
            print(f"⚠️ TTS文件不存在: {tts_file}")
            return False
        if not os.path.exists(sfx_file):
            print(f"⚠️ 音效文件不存在: {sfx_file}")
            # 如果音效不存在，直接复制TTS
            import shutil
            shutil.copy(tts_file, output_file)
            return True
        
        # 读取TTS音频
        with wave.open(tts_file, 'rb') as tts_wf:
            tts_params = tts_wf.getparams()
            tts_frames = tts_wf.readframes(tts_wf.getnframes())
            tts_nframes = tts_wf.getnframes()
            tts_sample_width = tts_wf.getsampwidth()
            tts_framerate = tts_wf.getframerate()
            tts_nchannels = tts_wf.getnchannels()
        
        # 读取音效
        with wave.open(sfx_file, 'rb') as sfx_wf:
            sfx_frames = sfx_wf.readframes(sfx_wf.getnframes())
            sfx_nframes = sfx_wf.getnframes()
            sfx_sample_width = sfx_wf.getsampwidth()
            sfx_framerate = sfx_wf.getframerate()
            sfx_nchannels = sfx_wf.getnchannels()
        
        # 计算TTS时长（秒）
        tts_duration = tts_nframes / tts_framerate
        # 计算音效时长（秒）
        sfx_duration = sfx_nframes / sfx_framerate
        
        print(f"      TTS时长: {tts_duration:.2f}秒, 音效时长: {sfx_duration:.2f}秒")
        
        # 1. 音效只取最后3秒（如果超过3秒）
        max_sfx_frames = int(max_sfx_duration * sfx_framerate)
        if sfx_nframes > max_sfx_frames:
            # 只取音效的最后3秒
            sfx_start_frame = sfx_nframes - max_sfx_frames
            sfx_nframes = max_sfx_frames
            sfx_frames = sfx_frames[sfx_start_frame * sfx_sample_width * sfx_nchannels:]
            print(f"      音效截取最后 {max_sfx_duration} 秒")
        
        # 2. 如果TTS时长小于音效时长，将音效截取到TTS的时长
        if tts_nframes < sfx_nframes:
            # 截取音效到TTS的时长
            sfx_nframes = tts_nframes
            sfx_frames = sfx_frames[:sfx_nframes * sfx_sample_width * sfx_nchannels]
            print(f"      音效截取到TTS时长: {tts_duration:.2f}秒")
        
        # 计算音效在TTS中的起始位置（TTS末尾减去音效时长）
        sfx_duration_frames = sfx_nframes
        overlay_start = max(0, tts_nframes - sfx_duration_frames)
        
        # 转换为数组进行处理
        if tts_sample_width == 2:
            fmt = 'h'  # 16-bit
        else:
            fmt = 'b'  # 8-bit
        
        tts_samples = array.array(fmt, tts_frames)
        sfx_samples = array.array(fmt, sfx_frames)
        
        # 调整音效音量
        sfx_samples = array.array(fmt, [int(s * sfx_volume) for s in sfx_samples])
        
        # 叠加音效到TTS末尾
        samples_per_frame = tts_nchannels
        overlay_start_sample = overlay_start * samples_per_frame
        
        for i in range(len(sfx_samples)):
            tts_idx = overlay_start_sample + i
            if tts_idx < len(tts_samples):
                # 叠加并限制范围
                mixed = tts_samples[tts_idx] + sfx_samples[i]
                if tts_sample_width == 2:
                    mixed = max(-32768, min(32767, mixed))
                else:
                    mixed = max(-128, min(127, mixed))
                tts_samples[tts_idx] = mixed
        
        # 写入输出文件
        with wave.open(output_file, 'wb') as out_wf:
            out_wf.setparams(tts_params)
            out_wf.writeframes(tts_samples.tobytes())
        
        print(f"✅ 音效叠加成功: {output_file}")
        return True
        
    except Exception as e:
        print(f"❌ 音效叠加失败: {e}")
        import traceback
        traceback.print_exc()
        # 失败时返回原TTS
        try:
            import shutil
            shutil.copy(tts_file, output_file)
            return True
        except:
            return False

def concatenate_audio_files(audio_files, output_file):
    """拼接多个音频文件
    
    Args:
        audio_files: 音频文件路径列表
        output_file: 输出文件路径
        
    Returns:
        bool: 成功返回True，失败返回False
    """
    import wave
    import struct
    
    try:
        # 收集所有音频数据
        all_frames = []
        params = None
        
        for audio_file in audio_files:
            if not os.path.exists(audio_file):
                print(f"⚠️ 音频文件不存在: {audio_file}")
                continue
                
            with wave.open(audio_file, 'rb') as wf:
                if params is None:
                    params = wf.getparams()
                frames = wf.readframes(wf.getnframes())
                all_frames.append(frames)
        
        if not all_frames or params is None:
            print("❌ 没有可用的音频数据")
            return False
        
        # 写入拼接后的音频
        with wave.open(output_file, 'wb') as out_wf:
            out_wf.setparams(params)
            for frames in all_frames:
                out_wf.writeframes(frames)
        
        print(f"✅ 音频拼接成功: {output_file}")
        return True
        
    except Exception as e:
        print(f"❌ 音频拼接失败: {e}")
        return False

@app.route('/api/generate-paragraph-audio', methods=['POST'])
def generate_paragraph_audio_api():
    """为一个段落生成完整音频（包含TTS和音效拼接）
    
    请求格式:
    {
        "items": [["文本内容", 0], ["音效描述", 1], ...],
        "voice": "xiaosi",
        "session_dir": "output_dir/xxx",
        "paragraph_index": 0
    }
    
    - type=0: 普通文本，调用TTS
    - type=1: 音效描述，调用音效生成
    """
    try:
        data = request.json
        items = data.get('items', [])
        voice = data.get('voice', 'xiaosi')
        session_dir = data.get('session_dir', None)
        paragraph_index = data.get('paragraph_index', 0)
        
        if not items:
            return jsonify({'error': '请提供段落内容'}), 400
        
        print(f"\n🎬 [段落音频生成] 段落 {paragraph_index}")
        print(f"   共 {len(items)} 个片段，并行生成中...")
        
        voice_type = resolve_voice_type(voice)
        lang = get_lang_by_voice(voice)
        custom_voice = is_custom_voice(voice)
        
        # 确保session_dir是绝对路径
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        
        # 确定输出目录
        if session_dir:
            tts_dir = os.path.join(session_dir, 'tts')
            sfx_dir = os.path.join(session_dir, 'sfx')
            os.makedirs(tts_dir, exist_ok=True)
            os.makedirs(sfx_dir, exist_ok=True)
        else:
            tts_dir = os.path.join(TARGET_DIR, 'history_res')
            sfx_dir = os.path.join(TARGET_DIR, 'history_res')
            os.makedirs(os.path.join(TARGET_DIR, 'history_res'), exist_ok=True)
        
        # 统计TTS和音效的索引
        tts_count = 0
        sfx_count = 0
        for item in items:
            if item[1] == 0:
                tts_count += 1
            else:
                sfx_count += 1
        
        print(f"   TTS片段: {tts_count}, 音效片段: {sfx_count}")
        
        # 定义生成单个片段的函数
        def generate_single_item(idx, text, item_type, tts_idx, sfx_idx, custom_voice_enabled):
            """生成单个音频片段，返回 (原始索引, 音频路径)"""
            if item_type == 0:
                # TTS生成（带重试机制）
                max_retries = 3
                for retry in range(max_retries):
                    try:
                        output_file = os.path.join(tts_dir, f'tts_{paragraph_index}_{tts_idx}.wav')
                        # 本地 edge-tts 合成，直接写入 wav 路径（供后续 wave 拼接）
                        local_tts.synthesize(text, voice, output_file, lang=lang)
                        print(f"   [{idx}] ✅ 本地TTS (尝试{retry+1}): {text[:20]}...")
                        return (idx, output_file)
                    except Exception as e:
                        print(f"   [{idx}] ❌ TTS失败 (尝试{retry+1}/{max_retries}): {e}")
                        if retry < max_retries - 1:
                            time.sleep(0.5)
                            continue
                
                print(f"   [{idx}] ❌ TTS彻底失败: {text[:20]}...")
                return (idx, None)
            else:
                # 音效生成
                try:
                    output_file = os.path.join(sfx_dir, f'sfx_{paragraph_index}_{sfx_idx}.wav')
                    result_file = sfx_generator.generate(
                        prompt=text,
                        duration=5,
                        output_file=output_file
                    )
                    if result_file:
                        print(f"   [{idx}] ✅ 音效: {text[:20]}...")
                        return (idx, result_file)
                    else:
                        print(f"   [{idx}] ⚠️ 音效失败: {text[:20]}...")
                        return (idx, None)
                except Exception as e:
                    print(f"   [{idx}] ❌ 音效失败: {e}")
                    return (idx, None)
        
        # 并行生成所有片段
        results = [None] * len(items)
        tts_idx = 0
        sfx_idx = 0
        
        # 准备任务参数
        tasks = []
        for idx, item in enumerate(items):
            text = item[0]
            item_type = item[1]
            if item_type == 0:
                tasks.append((idx, text, item_type, tts_idx, 0, custom_voice))
                tts_idx += 1
            else:
                tasks.append((idx, text, item_type, 0, sfx_idx, custom_voice))
                sfx_idx += 1
        
        # 打印任务列表（调试）
        print(f"   📋 任务列表:")
        for task in tasks:
            task_idx, task_text, task_type, task_tts_idx, task_sfx_idx, _ = task
            type_label = "TTS" if task_type == 0 else "SFX"
            idx_label = f"tts_idx={task_tts_idx}" if task_type == 0 else f"sfx_idx={task_sfx_idx}"
            print(f"      [{task_idx}] {type_label} ({idx_label}): {task_text[:20]}...")
        
        # 使用线程池并行执行
        start_time = time.time()
        with ThreadPoolExecutor(max_workers=min(len(items), 10)) as executor:
            futures = {
                executor.submit(generate_single_item, *task): task[0]
                for task in tasks
            }
            
            for future in as_completed(futures):
                idx, audio_path = future.result()
                results[idx] = audio_path
                print(f"      完成 [{idx}]: {audio_path}")
        
        elapsed = time.time() - start_time
        print(f"   ⏱️ 并行生成完成，耗时: {elapsed:.2f}秒")
        
        # 打印结果顺序（调试）
        print(f"   📋 结果顺序:")
        for i, path in enumerate(results):
            print(f"      [{i}]: {path}")
        
        # 处理音效叠加：如果TTS后面有音效，将音效叠加到TTS末尾
        print(f"   🔧 开始处理音效叠加...")
        processed_files = []
        i = 0
        while i < len(items):
            item_type = items[i][1]
            audio_path = results[i]
            item_text = items[i][0][:20] if items[i][0] else "(空)"
            
            print(f"      处理 [{i}] type={item_type} text={item_text}... path={audio_path}")
            
            if item_type == 0 and audio_path:  # TTS
                # 检查后面是否紧跟音效
                if i + 1 < len(items) and items[i + 1][1] == 1:
                    sfx_path = results[i + 1]
                    if sfx_path:
                        # 将音效叠加到TTS末尾
                        if session_dir:
                            overlay_dir = os.path.join(session_dir, 'overlay')
                            os.makedirs(overlay_dir, exist_ok=True)
                            overlay_file = os.path.join(overlay_dir, f'overlay_{paragraph_index}_{len(processed_files)}.wav')
                        else:
                            overlay_file = os.path.join(TARGET_DIR, 'history_res', f'overlay_{paragraph_index}_{len(processed_files)}.wav')
                        
                        print(f"         🔊 叠加: TTS[{i}] + SFX[{i+1}] -> {overlay_file}")
                        if overlay_sfx_on_tts(audio_path, sfx_path, overlay_file, sfx_volume=0.25):
                            processed_files.append(overlay_file)
                        else:
                            processed_files.append(audio_path)
                        i += 2  # 跳过已处理的音效
                        continue
                
                # TTS后面没有音效，直接添加
                print(f"         ➕ 直接添加TTS: {audio_path}")
                processed_files.append(audio_path)
            elif item_type == 1 and audio_path:  # 独立的音效（前面没有TTS）
                # 单独的音效，降低音量后直接添加
                print(f"         ➕ 独立音效: {audio_path}")
                processed_files.append(audio_path)
            else:
                print(f"         ⏭️ 跳过（audio_path为空或已处理）")
            
            i += 1
        
        print(f"   📦 处理后共 {len(processed_files)} 个音频文件:")
        for idx, pf in enumerate(processed_files):
            print(f"      [{idx}]: {pf}")
        
        # 拼接所有处理后的音频
        audio_files = processed_files
        
        # 拼接所有音频
        if audio_files:
            if session_dir:
                combined_dir = os.path.join(session_dir, 'combined')
                os.makedirs(combined_dir, exist_ok=True)
                combined_file = os.path.join(combined_dir, f'paragraph_{paragraph_index}.wav')
            else:
                combined_file = os.path.join(TARGET_DIR, 'history_res', f'paragraph_{paragraph_index}.wav')
            
            if concatenate_audio_files(audio_files, combined_file):
                print(f"   ✅ 段落音频拼接完成: {combined_file}")
                # 返回相对路径，而不是绝对路径
                relative_path = combined_file.replace(f'{TARGET_DIR}/', '')
                return jsonify({
                    'success': True,
                    'audio_path': relative_path,
                    'audio_files': [f.replace(f'{TARGET_DIR}/', '') for f in audio_files],
                    'message': f'段落 {paragraph_index} 音频生成成功'
                })
            else:
                # 如果拼接失败，返回第一个音频文件
                first_audio_path = audio_files[0].replace(f'{TARGET_DIR}/', '') if audio_files else None
                return jsonify({
                    'success': True,
                    'audio_path': first_audio_path,
                    'audio_files': [f.replace(f'{TARGET_DIR}/', '') for f in audio_files],
                    'message': '音频拼接失败，返回单个文件'
                })
        else:
            return jsonify({
                'success': False,
                'error': '没有生成任何音频'
            }), 500
            
    except Exception as e:
        print(f"❌ 段落音频生成错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/generate-sfx', methods=['POST'])
def generate_sfx_api():
    """生成音效接口"""
    try:
        data = request.json
        prompt = data.get('prompt', '')
        duration = data.get('duration', 5)
        session_dir = data.get('session_dir', None)
        paragraph_index = data.get('paragraph_index', 0)
        sfx_index = data.get('sfx_index', 0)
        
        if not prompt:
            return jsonify({'error': '请提供音效描述'}), 400
        
        print(f"🔊 [音效生成] 开始生成音效")
        print(f"   描述: {prompt}")
        print(f"   时长: {duration}秒")
        
        # 确保session_dir是绝对路径
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        
        # 确定输出文件路径
        if session_dir:
            sfx_dir = os.path.join(session_dir, 'sfx')
            os.makedirs(sfx_dir, exist_ok=True)
            output_file = os.path.join(sfx_dir, f'sfx_{paragraph_index}_{sfx_index}.wav')
        else:
            output_file = os.path.join(TARGET_DIR, 'history_res', f'sfx_{paragraph_index}_{sfx_index}.wav')
            os.makedirs(os.path.join(TARGET_DIR, 'history_res'), exist_ok=True)
        
        # 生成音效
        result_file = sfx_generator.generate(
            prompt=prompt,
            duration=duration,
            output_file=output_file
        )
        
        if result_file:
            print(f"✅ 音效生成成功: {result_file}")
            # 返回相对路径，而不是绝对路径
            relative_path = result_file.replace(f'{TARGET_DIR}/', '')
            return jsonify({
                'success': True,
                'audio_path': relative_path,
                'message': '音效生成成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '音效生成失败'
            }), 500
            
    except Exception as e:
        print(f"❌ 音效生成错误: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500



@app.route('/api/generate-baike', methods=['POST'])
def generate_baike():
    """生成百科问答接口"""
    try:
        data = request.json
        story_title = data.get('story_title', '')
        main_role = data.get('main_role', '')
        scene = data.get('scene', '')
        chapter = data.get('chapter', '')
        paragraph_index = data.get('paragraph_index', 0)
        session_dir = data.get('session_dir', None)
        
        # 确保session_dir是绝对路径
        if session_dir and not os.path.isabs(session_dir):
            session_dir = os.path.join(TARGET_DIR, session_dir)
        
        if not chapter:
            return jsonify({'error': '请提供故事段落内容'}), 400
        
        print(f"\n📚 [百科问答生成] 段落 {paragraph_index}")
        print(f"   内容: {chapter[:50]}...")
        
        params = {
            "story_title": story_title or "儿童故事",
            "main_role": main_role or "小动物",
            "scene": scene or "森林"
        }
        
        # 调用百科问答生成
        result = baike_generator.call(params, chapter)
        
        # 解析JSON结果
        try:
            qa_list = json.loads(result)
        except json.JSONDecodeError:
            print(f"   ⚠️ JSON解析失败，尝试修复")
            # 尝试提取JSON数组
            import re
            match = re.search(r'\[.*\]', result, re.DOTALL)
            if match:
                qa_list = json.loads(match.group())
            else:
                qa_list = []
        
        print(f"   ✅ 生成 {len(qa_list)} 个百科问答")
        for i, qa in enumerate(qa_list):
            print(f"      Q{i+1}: {qa.get('question', '')[:30]}...")
        
        # 保存到文件
        if session_dir:
            baike_dir = os.path.join(session_dir, 'baike')
            os.makedirs(baike_dir, exist_ok=True)
            baike_file = os.path.join(baike_dir, f'baike_{paragraph_index}.json')
            with open(baike_file, 'w', encoding='utf-8') as f:
                json.dump(qa_list, f, ensure_ascii=False, indent=2)
            print(f"   📝 已保存到: {baike_file}")
        
        return jsonify({
            'success': True,
            'qa_list': qa_list,
            'paragraph_index': paragraph_index,
            'message': f'百科问答生成成功，共{len(qa_list)}个问题'
        })
        
    except Exception as e:
        print(f"❌ 百科问答生成错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/nlu', methods=['POST'])
def nlu_answer():
    """NLU问答API - 接收问题，返回回答"""
    try:
        data = request.json
        question = data.get('question', '')
        
        if not question:
            return jsonify({'success': False, 'error': '问题不能为空'}), 400
        
        # 调用问答助手
        answer = qa_assistant.call(question)
        
        return jsonify({
            'success': True,
            'answer': answer
        })
    except Exception as e:
        print(f"NLU API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/generate-exam', methods=['POST'])
def generate_exam():
    """生成测试试卷接口"""
    try:
        data = request.json
        story_text = data.get('story', '')
        baike_qas = data.get('baike_qas', [])
        user_querys = data.get('user_querys', [])
        session_dir = data.get('session_dir', None)
        
        if not baike_qas:
            return jsonify({'error': '请提供百科问答数据'}), 400
        
        # print(f"\n📝 [试卷生成] 开始生成测试题")
        # print(f"   故事长度: {len(story_text)} 字符")
        # print(f"   百科问答数: {len(baike_qas)} 个")
        # print(f"   用户提问数: {len(user_querys)} 个")
        
        params = {
            "story": story_text,
            "baike_qas": baike_qas,
            "user_querys": user_querys
        }
        
        # 调用试卷生成
        result = exam_generator.call(params)
        
        # 解析JSON结果
        try:
            exam_data = json.loads(result)
        except json.JSONDecodeError:
            print(f"   ⚠️ JSON解析失败，尝试修复")
            import re
            match = re.search(r'\[.*\]', result, re.DOTALL)
            if match:
                exam_data = json.loads(match.group())
            else:
                exam_data = []
        
        print(f"   📋 原始生成 {len(exam_data)} 道题目")
        
        # 过滤并验证题目格式
        valid_exam_data = []
        for i, q in enumerate(exam_data):
            # 检查题目格式是否正确（必须是字典且包含必要字段）
            if isinstance(q, dict) and '问题' in q:
                question_text = q.get('问题', '')[:30] if q.get('问题') else '(无问题)'
                print(f"      Q{i+1}: {question_text}...")
                valid_exam_data.append(q)
            else:
                print(f"      Q{i+1}: ⚠️ 格式错误，已跳过: {str(q)[:50]}...")
        
        exam_data = valid_exam_data
        print(f"   ✅ 有效题目: {len(exam_data)} 道")
        
        # 保存到文件
        if session_dir:
            exam_dir = os.path.join(TARGET_DIR, session_dir, 'exam')
            os.makedirs(exam_dir, exist_ok=True)
            exam_file = os.path.join(exam_dir, 'exam.json')
            with open(exam_file, 'w', encoding='utf-8') as f:
                json.dump(exam_data, f, ensure_ascii=False, indent=2)
            print(f"   📝 已保存到: {exam_file}")
        
        return jsonify({
            'success': True,
            'exam': exam_data,
            'message': f'试卷生成成功，共{len(exam_data)}道题'
        })
        
    except Exception as e:
        print(f"❌ 试卷生成错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/save-exam-result', methods=['POST'])
def save_exam_result():
    """保存用户答案"""
    try:
        data = request.json
        session_dir = data.get('session_dir', '')
        user_answers = data.get('user_answers', {})
        exam_completed = data.get('exam_completed', False)
        
        if not session_dir:
            return jsonify({'success': False, 'error': '缺少session_dir'}), 400
        
        exam_dir = os.path.join(session_dir, 'exam')
        os.makedirs(exam_dir, exist_ok=True)
        
        exam_result_file = os.path.join(TARGET_DIR, exam_dir, 'exam_result.json')
        exam_result = {
            'user_answers': user_answers,
            'exam_completed': exam_completed,
            'timestamp': time.time()
        }
        
        with open(exam_result_file, 'w', encoding='utf-8') as f:
            json.dump(exam_result, f, ensure_ascii=False, indent=2)
        
        print(f"   ✅ 保存用户答案到: {exam_result_file}")
        return jsonify({'success': True, 'message': '答案已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _legacy_video_identity(prompt, image_path):
    with open(image_path, 'rb') as source:
        image_hash = hashlib.sha256(source.read()).hexdigest()
    fields = {'image': image_hash, 'prompt': build_page_video_prompt(prompt),
              'model': image2video_model, 'endpoint': image2video_url,
              'resolution': VIDEO_RESOLUTION, 'duration': VIDEO_DURATION, 'version': 1}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _legacy_video_cached(prompt, image_path, video_path):
    try:
        with open(video_path + '.json', encoding='utf-8') as source:
            recorded = json.load(source)
        return (os.path.isfile(video_path) and os.path.getsize(video_path) > 1024
                and recorded.get('cache_key') == _legacy_video_identity(prompt, image_path))
    except (OSError, ValueError):
        return False


def generate_video_task(prompt, image_path, session_dir, paragraph_index, force=False, *, session_identity=None):
    """Generate a page with an input-aware cache; force reaches the actual task."""
    print(f"\n🎬 [视频生成] 开始生成段落 {paragraph_index} 的视频")
    print(f"   接收到的session_dir: {session_dir}")
    print(f"   图片: {image_path}")

    session_dir = Path(_abs_session_dir(session_dir))
    video_dir = session_dir / 'videos'
    video_path = str(video_dir / f'video_{paragraph_index}.mp4')
    with _video_session_guard(session_dir, session_identity) as session_identity:
        video_dir.mkdir(parents=True, exist_ok=True)
        if not force and _legacy_video_cached(prompt, image_path, video_path):
            print(f"   ⏭️ 视频已存在，跳过: {video_path}")
            return video_path

    full_prompt = build_page_video_prompt(prompt)
    print(f"   提示词: {full_prompt[:80]}...")

    last_error = None
    for attempt in range(1, 3):
        try:
            with _video_session_guard(session_dir, session_identity):
                cache_key = _legacy_video_identity(prompt, image_path)
            # The downloader creates its output directory and writes a .part file.
            # Keep both outside the book while waiting for the remote result.
            with TemporaryDirectory(prefix='.video-', dir=TARGET_DIR) as staging:
                temporary_path = os.path.join(staging, 'video.mp4')
                result = video_generator.generate(
                    prompt=full_prompt,
                    image_path=image_path,
                    output_path=temporary_path,
                    duration=VIDEO_DURATION,
                    resolution=VIDEO_RESOLUTION,
                    aspect_ratio=VIDEO_RATIO,
                )
                if result and os.path.isfile(temporary_path) and os.path.getsize(temporary_path) > 1024:
                    if not probe_media(temporary_path)['has_video']:
                        raise RuntimeError('视频服务返回文件没有有效视频流')
                    with _video_session_guard(session_dir, session_identity):
                        os.replace(temporary_path, video_path)
                        with open(video_path + '.json', 'w', encoding='utf-8') as metadata:
                            json.dump({'cache_key': cache_key}, metadata)
                    print(f"   ✅ 视频生成成功: {video_path}")
                    return video_path
            last_error = '未返回有效视频文件'
        except (VideoSessionRemoved, BookshelfLockUnavailable):
            raise
        except VolcanoVideoError as e:
            last_error = str(e)
            print(f"   ⚠️ 视频生成失败（第 {attempt}/2 次）: {e}")
        except Exception as e:
            last_error = str(e)
            print(f"   ⚠️ 视频生成异常（第 {attempt}/2 次）: {e}")
            import traceback
            traceback.print_exc()
        if attempt < 2:
            time.sleep(5)

    print(f"   ❌ 视频生成最终失败（段落 {paragraph_index + 1}）: {last_error}")
    raise RuntimeError(f'视频生成失败: {last_error}')


@app.route('/api/file-exists', methods=['POST'])
def check_file_exists():
    """检查文件是否存在"""
    try:
        data = request.json
        file_path = data.get('file_path', '')
        
        if not file_path:
            return jsonify({'exists': False, 'error': '缺少file_path参数'}), 400
        
        # 将相对路径转换为绝对路径
        absolute_path = Path(_abs_session_dir(file_path))
        state = _video_status.get(absolute_path, {})
        if state.get('status') in {'queued', 'running'}:
            with _video_inflight_lock:
                if (absolute_path, state.get('identity')) not in _video_inflight:
                    state = {}
        if state.get('identity') is not None:
            try:
                with _video_session_guard(absolute_path.parent.parent, state['identity']):
                    pass
            except VideoSessionRemoved:
                state = {}
        exists = absolute_path.is_file() and state.get('status') not in {'queued', 'running', 'failed'}
        return jsonify({'exists': exists, 'file_path': file_path,
                        'status': state.get('status', 'succeeded' if exists else 'missing'),
                        'error': state.get('error')})
    except BookshelfLockUnavailable:
        raise
    except Exception as e:
        return jsonify({'exists': False, 'error': str(e)}), 500


@app.route('/api/generate-video', methods=['POST'])
def generate_video_api():
    """生成视频接口（异步）"""
    try:
        data = request.json
        prompt = data.get('prompt', '')
        image_path = data.get('image_path', '')
        session_dir = data.get('session_dir', '')
        paragraph_index = data.get('paragraph_index', 0)
        
        # 添加调试输出
        print(f"📝 视频生成API接收到的session_dir: {session_dir}")
        print(f"📝 视频生成API接收到的image_path: {image_path}")
        
        if not prompt or not image_path or not session_dir:
            return jsonify({'success': False, 'error': '缺少必要参数'}), 400
        
        if not os.path.exists(image_path):
            return jsonify({'success': False, 'error': '图片文件不存在'}), 400
        
        if not video_generator.is_configured():
            return jsonify({
                'success': False,
                'error': '未配置火山方舟 API Key，请在 .env 中设置 IMAGE2VIDEO_API_KEY'
            }), 500
        
        # 在后台线程中生成视频（使用全局线程池）
        print(f"🎬 [视频生成] 开始生成段落 {paragraph_index} 的视频, 图片: {image_path}, 提示词: {prompt[:50]}...")
        status = enqueue_video_task(prompt, image_path, session_dir, paragraph_index)
        
        # 立即返回，不等待完成
        return jsonify({
            'success': True,
            'message': '视频生成任务已提交' if status == 'queued' else f'视频任务未重复提交（{status}）',
            'status': status,
            'paragraph_index': paragraph_index
        })
    except BookshelfLockUnavailable:
        raise
    except VideoSessionRemoved as e:
        return jsonify(success=False, error=str(e)), 404
    except Exception as e:
        print(f"❌ 视频生成接口错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/generate-videos', methods=['POST'])
def generate_videos_api():
    """
    按绘本页循环生成视频（批量）。

    body:
      - session_dir: 绘本目录（必填）
      - pages: 可选 [{index, image_path, prompt}]；缺省自动扫描 images/pageN.*
      - force: 可选，true 时忽略缓存重新生成
    """
    try:
        data = request.json or {}
        session_dir = data.get('session_dir', '')
        pages = data.get('pages') or None
        force = bool(data.get('force', False))

        if not session_dir:
            return jsonify({'success': False, 'error': '缺少 session_dir'}), 400

        abs_dir = _abs_session_dir(session_dir)
        if not os.path.isdir(abs_dir):
            return jsonify({'success': False, 'error': f'绘本目录不存在: {session_dir}'}), 400

        if not video_generator.is_configured():
            return jsonify({
                'success': False,
                'error': '未配置火山方舟 API Key，请在 .env 中设置 IMAGE2VIDEO_API_KEY'
            }), 500

        result = submit_videos_for_session(abs_dir, pages=pages, force=force)
        logger.info('批量视频任务数量：排队 %d，缓存 %d，运行中 %d，错误 %d',
                    len(result['queued']), len(result['cached']), len(result['running']), len(result['errors']))
        return jsonify({
            'success': True,
            'session_dir': session_dir,
            'video_count': len(os.listdir(os.path.join(abs_dir, 'videos')))
            if os.path.isdir(os.path.join(abs_dir, 'videos')) else 0,
            **result,
        })
    except BookshelfLockUnavailable:
        raise
    except VolcanoVideoError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"❌ 批量视频生成接口错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ========== 图片分析 API（识别图片中的主体和位置）==========
@app.route('/api/analyze-image', methods=['POST'])
def analyze_image():
    """分析图片中的主体和位置"""
    try:
        data = request.json
        image_path = data.get('image_path', '')
        
        if not image_path:
            return jsonify({'success': False, 'error': '缺少 image_path 参数'}), 400
        
        # 检查文件是否存在
        if not os.path.exists(image_path):
            return jsonify({'success': False, 'error': f'图片文件不存在: {image_path}'}), 404
        
        print(f"🔍 开始分析图片: {image_path}")
        
        
        
        # cfg = {
        #     "url": "http://127.0.0.1:9988/v1/chat/completions",
        #     "model": "vlm_query2func_model"
        # }
        
        print(f"   🔍 使用 gemini_analyze.py 分析图片")
        print(f"   📄 图片路径: {image_path}")
        
        
        result = analyzer.call({
            "image_path": image_path,
            "max_objects": 5,
            "debug": False
        })
        
        logger.info('图片分析完成，主体数 %d', len(result) if isinstance(result, (dict, list)) else 0)
        
        return jsonify({
            'success': True,
            'objects': result
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ========== 视频帧分析 API（截取视频帧并识别主体）==========
def _save_browser_frame(encoded, output_path):
    max_bytes = 8 * 1024 * 1024
    if not isinstance(encoded, str):
        raise ValueError('帧必须是 base64 图片')
    if encoded.startswith('data:'):
        header, _, encoded = encoded.partition(',')
        if header not in {'data:image/jpeg;base64', 'data:image/png;base64'}:
            raise ValueError('帧必须是 JPEG 或 PNG 图片')
    if not encoded or len(encoded) > (max_bytes + 2) // 3 * 4:
        raise ValueError('帧大小无效')
    try:
        image = base64.b64decode(encoded, validate=True)
        if len(image) > max_bytes:
            raise ValueError('帧大小无效')
        with PILImage.open(io.BytesIO(image)) as frame:
            if frame.format not in {'JPEG', 'PNG'} or frame.width * frame.height > 3840 * 2160:
                raise ValueError('帧格式或尺寸无效')
            frame.convert('RGB').save(output_path)
    except (binascii.Error, OSError, PILImage.DecompressionBombError) as exc:
        raise ValueError('无法读取浏览器当前帧') from exc


@app.route('/api/analyze-video-frame', methods=['POST'])
def analyze_video_frame():
    """分析浏览器当前帧。旧客户端仍可按时间让服务端截帧。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error='请求必须是 JSON 对象'), 400
    video_path = data.get('video_path')
    if not isinstance(video_path, str) or not video_path:
        return jsonify(success=False, error='缺少视频路径'), 400
    try:
        frame_time = float(data.get('time_seconds', 0))
        if not math.isfinite(frame_time) or frame_time < 0:
            raise ValueError('无效时间')
        path = Path(_abs_session_dir(video_path))
        relative_path = path.relative_to(Path(TARGET_DIR).resolve())
        if len(relative_path.parts) < 2 or relative_path.parts[0] not in {'output_dir', 'bookshelf', 'history_res'}:
            raise ValueError('视频不在媒体目录中')
        if path.suffix.lower() not in _VIDEO_EXTENSIONS:
            raise ValueError('无效视频格式')
        frames = Path(_abs_session_dir(str(path.parent / 'temp_frames')))
    except (TypeError, ValueError, OSError, OverflowError):
        return jsonify(success=False, error='视频路径或帧时间无效'), 400
    if not path.is_file():
        return jsonify(success=False, error='视频文件不存在'), 404

    frame_source = 'browser' if data.get('frame_base64') not in (None, '') else 'server'
    output_path = frames / f'frame_{uuid.uuid4().hex}.png'
    try:
        frames.mkdir(exist_ok=True)
        if frame_source == 'browser':
            try:
                _save_browser_frame(data['frame_base64'], output_path)
            except ValueError as exc:
                return jsonify(success=False, error=str(exc)), 400
        else:
            with ImageCapture(str(path)) as capturer:
                if not capturer.capture_frame_at_time(frame_time, str(output_path)):
                    return jsonify(success=False, error='截取视频帧失败'), 502
        with PILImage.open(output_path) as image:
            width, height = image.size
        objects = analyzer.call({'image_path': str(output_path), 'max_objects': 5, 'debug': False})
    except (OSError, ValueError, requests.RequestException):
        return jsonify(success=False, error='视频帧分析失败，请检查视觉服务或重试'), 502
    return jsonify(success=True, frame_path=str(output_path), frame_source=frame_source,
                   frame_size={'width': width, 'height': height}, objects=objects)


# ========== Stable PDF book workflow ==========
from book_pipeline import register_book_routes, PipelineError
from book_video import wav_duration, probe_media
from quick_routes import routes as quick_routes


app.register_blueprint(quick_routes)


def _book_synthesize(text, voice, output_path, language):
    if not get_capabilities()['tts']:
        raise RuntimeError('朗读依赖未就绪')
    result = local_tts.synthesize(text, voice, output_path=output_path,
                                  lang='en' if language == 'en' else 'cn')
    wav_duration(result)
    return result


def _book_video_generate(image_path, prompt, output_path, options):
    if not get_capabilities()['video']:
        raise RuntimeError('图生视频服务未配置')
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + '.' + uuid.uuid4().hex + '.mp4')
    try:
        result = video_generator.generate(
            prompt=build_page_video_prompt(prompt), image_path=image_path,
            output_path=str(temporary), duration=int(options.get('duration', 5)),
            resolution=options.get('resolution', '720p'),
            aspect_ratio=options.get('ratio', '16:9'))
        if not result or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError('视频服务没有返回有效文件')
        os.replace(temporary, destination)
        return str(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _book_answer(question, context, history):
    if not get_capabilities()['answer']:
        raise RuntimeError('问答模型未配置')
    images = [item.get('image_path') for item in (context or [])
              if isinstance(item, dict) and item.get('image_path')]
    # 默认关闭：只有显式声明 NLU_MODEL 支持图片输入时才发送页面插画
    if images and os.environ.get('NLU_VISION', 'false').strip().lower() in ('1', 'true', 'yes', 'on'):
        try:
            return qa_assistant.call_with_images(question, max_length=240, context=context,
                                                 history=history, image_paths=images)
        except (RuntimeError, requests.RequestException) as exc:
            print(f"Vision QA failed, falling back to text QA: {exc}")
    return qa_assistant.call(question, max_length=240, context=context, history=history)


def _book_transcribe(audio_path, language):
    if not get_capabilities()['asr']:
        raise RuntimeError('语音识别服务未配置')
    return transcribe_audio(audio_path, language=language)


book_service = register_book_routes(app, {
    'data_dir': os.path.join(OUTPUT_DIR, 'books'),
    'max_upload_bytes': BOOK_MAX_UPLOAD_BYTES, 'max_pages': BOOK_MAX_PAGES,
    'reader_options': {
        'vl_base_url': image_analysis_url, 'vl_api_key': image_api_key, 'vl_model': image_model,
        'llm_base_url': simple_story_writer_url, 'llm_api_key': simple_story_writer_api_key,
        'llm_model': simple_story_writer_model,
    },
    'default_video': {'width': BOOK_VIDEO_WIDTH, 'height': BOOK_VIDEO_HEIGHT,
                      'fps': BOOK_VIDEO_FPS, 'model': image2video_model,
                      'resolution': '720p', 'duration': 5, 'ratio': '16:9'},
    'cache_identity': {'tts_model': 'edge-tts-sentence-v1', 'video_model': image2video_model,
                       'answer_model': nlu_model, 'vision_model': image_model,
                       'prompt_version': 'paperbook-v1'},
}, {'synthesize': _book_synthesize, 'video_generate': _book_video_generate,
    'answer': _book_answer, 'capabilities': get_capabilities, 'transcribe': _book_transcribe})
app.extensions['book_service'] = book_service


@app.get('/')
@app.get('/books')
def book_workbench():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'book_reader.html')


@app.get('/book_ui.js')
def book_ui_script():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'book_ui.js')


@app.get('/book_reader.css')
def book_ui_styles():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'book_reader.css')


FRAME_BUCKET = 4  # frames within 0.25 s share one recognition; the client keys its cache the same way


def _snap_frame_time(value):
    return math.floor(value * FRAME_BUCKET + 1e-6) / FRAME_BUCKET


@app.post('/api/books/<book_id>/recognize')
def book_recognize(book_id):
    value = request.get_json(silent=True) or {}
    if not isinstance(value, dict):
        raise PipelineError('请求必须是 JSON 对象')
    page_id = value.get('page_id')
    manifest = book_service.manifest(book_id)
    page = next((item for item in manifest['pages'] if item['page_id'] == page_id), None)
    if page is None:
        raise PipelineError('页标识不存在', 404)
    source = value.get('source', 'image')
    if source not in {'image', 'video', 'film'}:
        raise PipelineError('识别来源必须为 image、video 或 film')
    started = time.perf_counter()
    request_id = str(value.get('request_id', ''))[:100]
    frame_time = value.get('frame_time', 0)
    try:
        frame_time = float(frame_time)
    except (TypeError, ValueError) as exc:
        raise PipelineError('帧时间无效') from exc
    if not math.isfinite(frame_time) or frame_time < 0:
        raise PipelineError('帧时间无效')
    if source == 'film':
        # The film clock maps to exactly one page through the exported timeline.
        if not manifest.get('exports'):
            raise PipelineError('尚未合成整本影片', 409)
        timeline = book_service.timeline(book_id, manifest)
        row = next((item for item in timeline if item.get('video_start', item['book_start']) <= frame_time
                    < item.get('video_end', item['book_end'])), None)
        if row is None:
            raise PipelineError('影片时间超出整片范围')
        page_id = row['page_id']
        page = next(item for item in manifest['pages'] if item['page_id'] == page_id)
        # Snap to the shared frame bucket, but never back across the page boundary.
        frame_time = max(_snap_frame_time(frame_time), row.get('video_start', row['book_start']))
    elif source == 'video':
        frame_time = _snap_frame_time(frame_time)
    image_ref = page['image_path']
    image_path = book_service.asset(book_id, image_ref)
    cached_page = source == 'image' and page.get('objects', {}).get('detections')
    reused = bool(cached_page)
    if cached_page:
        objects = page['objects']
    else:
        if not get_capabilities()['vision']:
            raise PipelineError('视觉识别模型未配置；已保留原图阅读', 503)
        frames = book_service.book_dir(book_id) / 'attachments' / 'frames'
        frames.mkdir(parents=True, exist_ok=True)
        relative_video = None
        if source == 'video':
            relative_video = page.get('media', {}).get('video', {}).get('path')
            if not relative_video:
                raise PipelineError('此页尚无视频，请先生成页级媒体', 409)
        elif source == 'film':
            relative_video = manifest['exports']['video_path']
        if relative_video:
            video_path = book_service.asset(book_id, relative_video)
            with ImageCapture(str(video_path)) as capture:
                if frame_time >= capture.duration:
                    raise PipelineError('帧时间超出当前页短片范围' if source == 'video' else '影片时间超出整片范围')
                # One cache entry per displayed frame: repeated taps on a paused frame are instant.
                fps = capture.fps if capture.fps and capture.fps > 0 else 24
                frame_index = int(math.floor(frame_time * fps + 1e-6))
                key = hashlib.sha256(f'{relative_video}|{frame_index}|{image_model}'.encode()).hexdigest()[:24]
                image_path = frames / (key + '.png')
                if not image_path.is_file():
                    temporary = frames / (key + '.' + uuid.uuid4().hex[:8] + '.png')
                    if not capture.capture_frame_at_time(frame_index / fps, str(temporary)):
                        raise PipelineError('读取当前视频帧失败', 502)
                    os.replace(temporary, image_path)
        else:
            key = hashlib.sha256(f"{image_ref}|{hashlib.sha256(image_path.read_bytes()).hexdigest()}|{image_model}".encode()).hexdigest()[:24]
        cache_file = frames / (key + '.json')
        objects = None
        if cache_file.is_file():
            try:
                objects = json.loads(cache_file.read_text('utf-8'))
                reused = isinstance(objects, dict) and objects.get('coordinate_space') == 'pixel'
            except (OSError, ValueError):
                objects = None
            if not reused:
                objects = None
        if objects is None:
            try:
                objects = analyzer.call_with_metadata({'image_path': str(image_path), 'max_objects': 12,
                           'source_id': f'{book_id}/{page_id}/{source}/{frame_time:.3f}'})
            except Exception as exc:
                raise PipelineError('视觉识别失败，请检查服务配置或重试', 502) from exc
            if isinstance(objects, dict) and objects.get('coordinate_space') == 'pixel':
                temporary = cache_file.with_name(cache_file.name + '.' + uuid.uuid4().hex[:8])
                temporary.write_text(json.dumps(objects, ensure_ascii=False), 'utf-8')
                os.replace(temporary, cache_file)
        if relative_video:
            image_ref = image_path.relative_to(book_service.book_dir(book_id)).as_posix()
    return jsonify(success=True, book_id=book_id, page_id=page_id, request_id=request_id,
                   source=source, frame_time=frame_time, objects=objects, cached=reused,
                   image_ref=image_ref, image_url=book_service.asset_url(book_id, image_ref),
                   timing_ms=round((time.perf_counter() - started) * 1000, 1))


@app.post('/api/books/<book_id>/speak')
def book_speak(book_id):
    book_service.manifest(book_id)
    value = request.get_json(silent=True) or {}
    if not isinstance(value, dict):
        raise PipelineError('请求必须是 JSON 对象')
    text, voice = value.get('text', ''), value.get('voice', 'xiaoyuan')
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise PipelineError('朗读文本必须在 1 至 2000 字内')
    if not isinstance(voice, str) or not voice or len(voice) > 100:
        raise PipelineError('音色无效')
    if not get_capabilities()['tts']:
        raise PipelineError('朗读依赖未就绪', 503)
    directory = book_service.book_dir(book_id) / 'attachments' / 'spoken'
    directory.mkdir(parents=True, exist_ok=True)
    language = 'en' if value.get('language') == 'en' else 'zh'
    # Short labels repeat constantly (every tap on the same object); synthesize each wording once.
    digest = hashlib.sha256(f'{text.strip()}|{voice}|{language}'.encode()).hexdigest()[:24]
    path = directory / f'say-{digest}.wav'
    cached = False
    if path.is_file():
        try:
            duration = wav_duration(path)
            cached = duration > 0
        except Exception:
            cached = False
    if not cached:
        temporary = directory / f'say-{digest}.{uuid.uuid4().hex[:8]}.wav'
        try:
            _book_synthesize(text.strip(), voice, str(temporary), language)
            duration = wav_duration(temporary)
            os.replace(temporary, path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise PipelineError('朗读生成失败，请检查网络服务或重试', 502) from exc
    relative = path.relative_to(book_service.book_dir(book_id)).as_posix()
    return jsonify(success=True, audio_ref=relative, audio_url=book_service.asset_url(book_id, relative),
                   duration=duration, cached=cached)


def _print_service_urls(host, port):
    browser_host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    if ':' in browser_host and not browser_host.startswith('['):
        browser_host = f'[{browser_host}]'
    base_url = f'http://{browser_host}:{port}'
    quick_url = f'{base_url}/quick'
    print(f'纸书智伴工作台 {base_url}/')
    print(f'单页速读 {quick_url}')
    print(f'主题故事创作 {base_url}/tts_v2.html')
    print('能力检查只验证本机配置与依赖；模型服务仍需实际联调。')
    return quick_url


def _open_quick_browser(url):
    try:
        if webbrowser.open(url):
            return
    except (OSError, webbrowser.Error):
        pass
    print(f'无法自动打开浏览器，请手动访问 {url}')


if __name__ == '__main__':
    host, port = app_config.HOST, int(os.environ.get('PORT', '5000'))
    if '--open-quick' in sys.argv[1:]:
        from werkzeug.serving import make_server

        with make_server(host, port, app, threaded=True) as server:
            quick_url = _print_service_urls(host, server.server_port)
            threading.Thread(target=_open_quick_browser, args=(quick_url,), daemon=True).start()
            server.serve_forever()
    else:
        _print_service_urls(host, port)
        app.run(host=host, port=port, debug=False, threaded=True)


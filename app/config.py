import os
import importlib.util
import shutil
from urllib.parse import urlparse

# 输出数据根目录（可通过环境变量 TARGET_DIR 覆盖；默认为项目根目录，
# 书架数据为根目录下 bookshelf.json，生成结果写入 output_dir/ 等子目录）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path):
    """读取本地 .env（仅填充尚未设置的环境变量），避免密钥写入源码。"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Tests set PAPERBOOK_SKIP_DOTENV=1 so a developer's real keys never leak into hermetic runs.
if os.environ.get("PAPERBOOK_SKIP_DOTENV", "").lower() not in ("1", "true", "yes"):
    _load_env_file(os.path.join(_BASE_DIR, ".env"))
TARGET_DIR = os.path.abspath(os.path.expanduser(os.environ.get("TARGET_DIR") or _BASE_DIR))


def _env(key, default="xxxx"):
    """从环境变量读取配置，未设置时使用占位符。"""
    return os.environ.get(key, default)


# ---------------- 服务端点配置（请通过环境变量或修改此处默认值填入） ----------------

# 声音注册（enroll）服务地址
ENROLL_SERVER_URL = _env("ENROLL_SERVER_URL")

# 语音克隆 TTS 流式接口
voice_clone_tts_api_url = _env("VOICE_CLONE_TTS_API_URL")

# NLU 问答模型（OpenAI 兼容接口）
nlu_url = _env("NLU_URL")
nlu_api_key = _env("NLU_API_KEY")
nlu_model = _env("NLU_MODEL", "qwen3-30b-a3b")

# 图像理解（VL）模型
image_analysis_url = _env("IMAGE_ANALYSIS_URL")
image_api_key = _env("IMAGE_API_KEY")
image_model = _env("IMAGE_MODEL", "qwen3-vl-30b-a3b-instruct")

# 图生视频（火山方舟豆包 Seedance）
# 密钥优先读 IMAGE2VIDEO_API_KEY，兼容 ARK_API_KEY；未配置时保留占位符
image2video_api_key = (
    os.environ.get("IMAGE2VIDEO_API_KEY")
    or os.environ.get("ARK_API_KEY")
    or "xxxx"
)
image2video_model = _env("IMAGE2VIDEO_MODEL", "doubao-seedance-1-0-pro-fast-251015")
image2video_url = _env("IMAGE2VIDEO_URL", "https://ark.cn-beijing.volces.com/api/v3")

# 音效生成
text2audio_url = _env("TEXT2AUDIO_URL")
text2audio_api_key = _env("TEXT2AUDIO_API_KEY")
text2audio_model = _env("TEXT2AUDIO_MODEL", "audio1.0")

# 背景音乐生成
text2music_url = _env("TEXT2MUSIC_URL")
text2music_api_key = _env("TEXT2MUSIC_API_KEY")
text2music_model = _env("TEXT2MUSIC_MODEL", "imagination-bgm")

# 图像生成
text2image_url = _env("TEXT2IMAGE_URL")
text2image_api_key = _env("TEXT2IMAGE_API_KEY")
text2image_model = _env("TEXT2IMAGE_MODEL", "gemini-3-pro-image")

# 故事写作（OpenAI 兼容接口）
simple_story_writer_url = _env("SIMPLE_STORY_WRITER_URL")
simple_story_writer_api_key = _env("SIMPLE_STORY_WRITER_API_KEY")
simple_story_writer_model = _env("SIMPLE_STORY_WRITER_MODEL", "deepseek-v3.2")

# ASR WebSocket 服务地址
ASR_WS_SERVER_URL = _env("ASR_WS_SERVER_URL")

# TTS API 地址
tts_api_url = _env("TTS_API_URL")


def is_configured(value):
    """Reject placeholders without disclosing any actual configuration value."""
    text = str(value or '').strip()
    return bool(text) and text.lower() not in {
        'xxxx', 'xxx', 'your_key', 'your_api_key', 'your-api-key', 'changeme',
        'none', 'null', 'placeholder', 'api_key', 'ark_api_key',
    } and not text.startswith(('请填写', 'YOUR_', '<'))


def service_configured(url, key, model):
    parsed = urlparse(str(url or ''))
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc) and is_configured(key) and is_configured(model)


def _module_present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def tool_path(name):
    configured = os.environ.get(name.upper() + '_PATH', '')
    if configured:
        return configured if os.path.isfile(configured) else None
    return shutil.which(name)


def get_capabilities():
    """Configuration/local checks only: this function never makes API requests."""
    vision = service_configured(image_analysis_url, image_api_key, image_model)
    model_path = os.path.expanduser(os.environ.get('WHISPER_MODEL', 'models/faster-whisper-small'))
    if not os.path.isabs(model_path):
        model_path = os.path.join(_BASE_DIR, model_path)
    asr_ready = os.path.isdir(model_path) or os.environ.get('WHISPER_ALLOW_DOWNLOAD', 'false').lower() == 'true'
    return {
        'story': service_configured(simple_story_writer_url, simple_story_writer_api_key, simple_story_writer_model),
        'answer': service_configured(nlu_url, nlu_api_key, nlu_model),
        'ocr': vision, 'vision': vision,
        'image': service_configured(text2image_url, text2image_api_key, text2image_model),
        'music': service_configured(text2music_url, text2music_api_key, text2music_model),
        'sfx': service_configured(text2audio_url, text2audio_api_key, text2audio_model),
        'video': service_configured(image2video_url, image2video_api_key, image2video_model),
        'tts': _module_present('edge_tts') and bool(tool_path('ffmpeg')),
        'asr': _module_present('faster_whisper') and asr_ready,
        'pdf': _module_present('pymupdf'),
        'ffmpeg': bool(tool_path('ffmpeg')), 'ffprobe': bool(tool_path('ffprobe')),
    }


BOOK_VIDEO_WIDTH = int(_env('BOOK_VIDEO_WIDTH', '1280'))
BOOK_VIDEO_HEIGHT = int(_env('BOOK_VIDEO_HEIGHT', '720'))
BOOK_VIDEO_FPS = int(_env('BOOK_VIDEO_FPS', '24'))
BOOK_MAX_PAGES = int(_env('BOOK_MAX_PAGES', '100'))
BOOK_MAX_UPLOAD_BYTES = int(_env('BOOK_MAX_UPLOAD_BYTES', str(128 * 1024 * 1024)))

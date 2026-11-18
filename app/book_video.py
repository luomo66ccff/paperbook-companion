"""Local, reproducible FFmpeg book export. No cloud calls or personal tool paths."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave


class MediaError(RuntimeError):
    pass


def _tool(name, explicit=None):
    tool = explicit or os.environ.get(name.upper() + '_PATH') or shutil.which(name)
    if not tool or not Path(tool).is_file():
        raise MediaError(f'找不到 {name}，请安装 FFmpeg/ffprobe 并加入 PATH')
    return str(tool)


def _run(args, timeout=300):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout,
                                text=True, encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError('媒体工具无法运行或处理超时') from exc
    if result.returncode:
        # Tool stderr may contain absolute paths; callers receive a safe message.
        raise MediaError('媒体处理失败，请检查输入编码和 FFmpeg 安装')
    return result.stdout


def probe_media(path, ffprobe=None):
    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        raise MediaError('媒体文件不存在或为空')
    tool = _tool('ffprobe', ffprobe)
    output = _run([tool, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)], 30)
    try:
        result = json.loads(output)
        streams = result.get('streams', [])
        duration = float(result.get('format', {}).get('duration', 0))
        videos = [s for s in streams if s.get('codec_type') == 'video']
        audios = [s for s in streams if s.get('codec_type') == 'audio']
        video, audio = (videos or [{}])[0], (audios or [{}])[0]
        rate = video.get('avg_frame_rate', '0/1').split('/')
        fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 0
    except (ValueError, TypeError, KeyError) as exc:
        raise MediaError('ffprobe 返回无效的媒体信息') from exc
    if not math.isfinite(duration) or duration < 0:
        raise MediaError('媒体时长无效')
    return {'duration': round(duration, 6), 'width': video.get('width'), 'height': video.get('height'),
            'fps': round(fps, 6), 'video_codec': video.get('codec_name'),
            'audio_codec': audio.get('codec_name'), 'has_video': bool(videos), 'has_audio': bool(audios)}


def wav_duration(path):
    try:
        with wave.open(str(path), 'rb') as audio:
            if audio.getnframes() <= 0 or audio.getframerate() <= 0 or audio.getcomptype() != 'NONE':
                raise ValueError('空或压缩 WAV')
            return audio.getnframes() / audio.getframerate()
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise MediaError('配音必须是可读取且非空的 PCM WAV') from exc


def concatenate_wav(paths, output_path):
    if not paths:
        raise MediaError('没有句子配音')
    params, chunks = None, []
    for path in paths:
        wav_duration(path)
        with wave.open(str(path), 'rb') as audio:
            current = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype())
            if params is not None and current != params:
                raise MediaError('句子配音的采样率、声道或位深不一致')
            params = current
            chunks.append(audio.readframes(audio.getnframes()))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), 'wb') as target:
        target.setnchannels(params[0])
        target.setsampwidth(params[1])
        target.setframerate(params[2])
        for data in chunks:
            target.writeframes(data)
    return wav_duration(output_path)


def write_silence(path, duration=3, sample_rate=24000):
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0.1 <= duration <= 60:
        raise ValueError('无字页停留时间必须为 0.1 到 60 秒')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b'\x00\x00' * round(sample_rate * duration))
    return wav_duration(path)


def _inside(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ':' in relative or '\\' in relative:
        raise MediaError('媒体路径必须是书目录内的相对路径')
    source = (root / relative).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise MediaError('媒体路径越界或不存在')
    return source


def _options(options):
    options = options or {}
    width, height, fps = int(options.get('width', 1280)), int(options.get('height', 720)), int(options.get('fps', 24))
    if not (2 <= width <= 3840 and 2 <= height <= 2160 and width % 2 == height % 2 == 0 and 1 <= fps <= 60):
        raise ValueError('视频尺寸必须是偶数且不超过3840x2160，帧率1到60')
    return {'width': width, 'height': height, 'fps': fps}


def _srt_time(seconds):
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}'


def export_book(book_dir, manifest, options=None, force=False):
    """Loop/trim silent page clips to measured narration, then mux an entire book.

    Timeline book_time is the final film clock. frame_time is the source clip
    clock and loops modulo the measured source duration; they are not synonyms.
    """
    root = Path(book_dir).resolve()
    settings = _options(options)
    ffmpeg = _tool('ffmpeg', (options or {}).get('ffmpeg'))
    ffprobe = _tool('ffprobe', (options or {}).get('ffprobe'))
    pages = manifest.get('pages', [])
    if not pages:
        raise MediaError('书本没有可导出的页')
    missing = [str(page.get('page_num', page['page_id'])) for page in pages
               if page.get('media', {}).get('tts', {}).get('status') not in ('ready', 'silent')
               or page.get('media', {}).get('video', {}).get('status') != 'ready']
    if missing:
        listed = '、'.join(missing[:8]) + ('…' if len(missing) > 8 else '')
        raise MediaError(f'还有 {len(missing)} 页的朗读或动画未完成（第 {listed} 页），请先点“准备整本”，完成后再合成')
    prepared, identities = [], []
    for page in pages:
        tts, video = page.get('media', {}).get('tts', {}), page.get('media', {}).get('video', {})
        if page.get('text', '').strip() and tts.get('status') == 'silent' and tts.get('silent_reason') != 'no_narration_text':
            raise MediaError('有文字页不能以静音冒充配音')
        audio = _inside(root, tts.get('path'))
        clip = _inside(root, video.get('path'))
        duration = wav_duration(audio)
        clip_info = probe_media(clip, ffprobe)
        if not clip_info['has_video'] or clip_info['duration'] <= 0:
            raise MediaError('页视频没有有效视频流')
        prepared.append((page, audio, clip, duration, clip_info['duration']))
        identities.append({'page_id': page['page_id'], 'text': page.get('text', ''),
                           'tts': hashlib.sha256(audio.read_bytes()).hexdigest(),
                           'video': hashlib.sha256(clip.read_bytes()).hexdigest(),
                           'sentences': tts.get('sentences', [])})
    key = hashlib.sha256(json.dumps({'version': 2, 'pages': identities, 'options': settings},
                                   ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    relative = f'exports/{key[:24]}'  # short dir: Windows MAX_PATH
    destination = root / relative
    if not force and (destination / 'result.json').is_file():
        try:
            cached = json.loads((destination / 'result.json').read_text('utf-8'))
            for field in ('video_path', 'srt_path', 'timeline_path'):
                asset = _inside(root, cached[field])
                if hashlib.sha256(asset.read_bytes()).hexdigest() != cached['artifact_hashes'][field]:
                    raise MediaError('导出缓存校验失败')
            cached['cached'] = True
            return cached
        except (ValueError, KeyError, MediaError):
            pass
    (root / 'exports').mkdir(exist_ok=True)
    timeline, subtitles, elapsed = [], [], 0.0
    frame_boundaries, measured_total = [0], 0.0
    for _, _, _, duration, _ in prepared:
        measured_total += duration
        frame_boundaries.append(round(measured_total * settings['fps']))
    frame_boundaries[-1] = math.ceil(measured_total * settings['fps'] - 1e-9)
    if any(right <= left for left, right in zip(frame_boundaries, frame_boundaries[1:])):
        raise MediaError('某页配音短于一帧，无法在当前帧率下保留全部页')
    work = Path(tempfile.mkdtemp(prefix='.export-', dir=root / 'exports'))
    try:
        # One PCM stream for the entire book, encoded to AAC exactly once.
        concatenate_wav([audio for _, audio, *_ in prepared], work / 'narration.wav')
        for index, (page, audio, clip, duration, clip_duration) in enumerate(prepared):
            name = page['page_id'] + '.mkv'
            frame_start, frame_end = frame_boundaries[index:index + 2]
            frame_count = frame_end - frame_start
            visual_start, visual_end = frame_start / settings['fps'], frame_end / settings['fps']
            filters = (f"scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=decrease,"
                       f"pad={settings['width']}:{settings['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={settings['fps']}")
            _run([ffmpeg, '-y', '-v', 'error', '-stream_loop', '-1', '-i', str(clip),
                  '-map', '0:v:0', '-an', '-vf', filters, '-frames:v', str(frame_count),
                  '-c:v', 'ffv1', '-pix_fmt', 'yuv420p', str(work / name)])
            sentence_rows = []
            for sentence in page['media']['tts'].get('sentences', []):
                start, end = elapsed + sentence['start'], elapsed + sentence['end']
                if sentence['start'] < 0 or sentence['end'] <= sentence['start'] or sentence['end'] > duration + 1e-6:
                    raise MediaError('句子时间轴超出真实页配音')
                timing_source = 'provided_offsets'
                if sentence.get('audio_path'):
                    actual_sentence_duration = wav_duration(_inside(root, sentence['audio_path']))
                    if abs(actual_sentence_duration - (sentence['end'] - sentence['start'])) > 1e-6:
                        raise MediaError('句子时间轴与实际句子 WAV 不一致')
                    timing_source = 'measured_sentence_wav'
                sentence_rows.append({**sentence, 'book_start': round(start, 6), 'book_end': round(end, 6),
                                      'timing_source': timing_source,
                                      'frame_start': round((start - visual_start) % clip_duration, 6) if visual_start <= start < visual_end else None,
                                      'frame_end': round((end - visual_start) % clip_duration, 6) if visual_start <= end < visual_end else None,
                                      'visual_boundary_crossed': not (visual_start <= start < visual_end and visual_start <= end < visual_end)})
                if sentence['text'].strip():
                    subtitles.append(f"{len(subtitles) + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{sentence['text']}\n")
            timeline.append({'page_id': page['page_id'], 'page_num': page['page_num'],
                             'book_start': round(elapsed, 6), 'book_end': round(elapsed + duration, 6),
                             'duration': duration, 'source_video_duration': clip_duration,
                             'video_start_frame': frame_start, 'video_end_frame': frame_end,
                             'video_start': frame_start / settings['fps'],
                             'video_end': frame_end / settings['fps'],
                             'visual_offset_seconds': frame_start / settings['fps'] - elapsed,
                             'frame_time_mapping': 'frame_time = (book_time - video_start) % source_video_duration; valid only video_start <= book_time < video_end',
                             'sentences': sentence_rows})
            elapsed += duration
        listing = work / 'concat.txt'
        listing.write_text('\n'.join("file '" + page['page_id'] + ".mkv'" for page, *_ in prepared), 'utf-8')
        _run([ffmpeg, '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing),
              '-i', str(work / 'narration.wav'), '-map', '0:v:0', '-map', '1:a:0',
              '-vf', f"setpts=N/({settings['fps']}*TB)", '-frames:v', str(frame_boundaries[-1]),
              '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
              '-pix_fmt', 'yuv420p', '-r', str(settings['fps']), '-c:a', 'aac',
              '-ar', '48000', '-ac', '2', '-movflags', '+faststart', str(work / 'book.mp4')])
        probe = probe_media(work / 'book.mp4', ffprobe)
        if not probe['has_audio'] or not probe['has_video'] or probe['video_codec'] != 'h264' or probe['audio_codec'] != 'aac':
            raise MediaError('导出文件缺少 H264/AAC 音视频流')
        if (probe['width'], probe['height']) != (settings['width'], settings['height']) or abs(probe['fps'] - settings['fps']) > .01:
            raise MediaError('导出文件尺寸或帧率不匹配')
        if abs(probe['duration'] - elapsed) > 1 / settings['fps'] + .03:
            raise MediaError('导出时长与真实配音时间轴不匹配')
        (work / 'subtitles.srt').write_text('\n'.join(subtitles), 'utf-8')
        (work / 'timeline.json').write_text(json.dumps({'schema_version': 2, 'duration': elapsed,
                                                       'video_frames': frame_boundaries[-1],
                                                       'video_duration': frame_boundaries[-1] / settings['fps'],
                                                       'visual_quantization_max_seconds': 1 / settings['fps'],
                                                       'pages': timeline}, ensure_ascii=False, indent=2), 'utf-8')
        result = {'cache_key': key, 'cached': False, 'video_path': relative + '/book.mp4',
                  'srt_path': relative + '/subtitles.srt', 'timeline_path': relative + '/timeline.json',
                  'duration': round(elapsed, 6), 'probe': probe}
        result['artifact_hashes'] = {field: hashlib.sha256((work / filename).read_bytes()).hexdigest()
                                    for field, filename in (('video_path', 'book.mp4'),
                                                            ('srt_path', 'subtitles.srt'),
                                                            ('timeline_path', 'timeline.json'))}
        (work / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        # Only final artifacts are published; incomplete exports remain invisible.
        if destination.exists():
            if destination.resolve().parent != (root / 'exports').resolve():
                raise MediaError('导出目标路径越界')
            shutil.rmtree(destination)
        os.replace(work, destination)
        return result
    finally:
        if work.exists():
            shutil.rmtree(work)

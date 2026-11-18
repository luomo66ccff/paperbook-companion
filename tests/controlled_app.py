"""Real Flask/PDF/FFmpeg browser fixture with clearly controlled AI outputs.

Usage: python tests/controlled_app.py --data-dir <temporary directory> --port 18782
This is a test harness, never a production AI provider or an acceptance result.
"""
import argparse
import io
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


def create_fixture(path):
    import pymupdf
    document = pymupdf.open()
    first = document.new_page(width=420, height=300)
    first.draw_rect((70, 90, 190, 220), color=(0.2, .4, .8), fill=(.7, .85, 1))
    first.insert_text((20, 40), '小兔子把红苹果送给小狐狸。', fontname='china-s', fontsize=18)
    scan = pymupdf.open()
    page = scan.new_page(width=420, height=300)
    page.draw_circle((220, 155), 65, color=(.8, .2, .2), fill=(1, .7, .6))
    page.insert_text((20, 40), '小狐狸喜欢红苹果。', fontname='china-s', fontsize=18)
    second = document.new_page(width=420, height=300)
    second.insert_image(second.rect, stream=page.get_pixmap(alpha=False).tobytes('png'))
    document.new_page(width=420, height=300).draw_circle((210, 150), 60)
    document.save(path)
    document.close()
    scan.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--port', type=int, default=18782)
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ['TARGET_DIR'] = str(data_dir)
    import story_tts_server as server
    from pdf_book_reader import PDFBookReader
    from PIL import Image
    from coordinates import object_contract

    fixture_pdf = data_dir / 'controlled-mixed.pdf'

    class ControlledReader(PDFBookReader):
        is_fixture = False

        def read_book(self, pdf_path, *args, **kwargs):
            self.is_fixture = Path(pdf_path).read_bytes() == fixture_pdf.read_bytes()
            return super().read_book(pdf_path, *args, **kwargs)

        def ocr_page(self, path):
            # Only the bundled fixture has fixed transcripts. Any other book (e.g. the
            # competition samples) reaches OCR only for pages without a text layer,
            # which this controlled harness reports as wordless instead of inventing text.
            if not self.is_fixture:
                return '[无文字页面]'
            return {'p0001': '小兔子把红苹果送给小狐狸。', 'p0002': '小狐狸喜欢红苹果。',
                    'p0003': '[无文字页面]'}[Path(path).stem]
        def verify_ocr(self, path, text):
            return text, True
        def analyze_page_scene(self, path):
            return {'summary': '受控测试画面', 'scene': '绘本', 'characters': [], 'key_objects': []}
        def detect_objects(self, path, max_objects=8):
            with Image.open(path) as image:
                width, height = image.size
            return {'coordinate_space': 'pixel', 'detections': [
                {'label': '绘本角色<character>', 'box': [round(width * .15), round(height * .25),
                                                       round(width * .65), round(height * .85)]}]}

    def synthesize(text, voice, output_path=None, **kwargs):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rate, duration = 24000, .8
        samples = b''.join(struct.pack('<h', round(5000 * math.sin(2 * math.pi * 440 * n / rate)))
                           for n in range(round(duration * rate)))
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            audio.writeframes(samples)
        return str(path)

    def video_generate(**kwargs):
        destination = Path(kwargs['output_path'])
        subprocess.run([shutil.which('ffmpeg'), '-y', '-v', 'error', '-loop', '1',
                        '-i', kwargs['image_path'], '-t', '0.5', '-vf',
                        'scale=320:240:force_original_aspect_ratio=decrease,pad=320:240:(ow-iw)/2:(oh-ih)/2',
                        '-r', '24', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(destination)],
                       check=True, capture_output=True, timeout=30)
        return str(destination)

    def analyze(params):
        with Image.open(params['image_path']) as image:
            width, height = image.size
        return object_contract({'detections': [{'label': '绘本角色<character>',
                            'box': [round(width * .15), round(height * .25), round(width * .65), round(height * .85)]}]},
                               width, height, params.get('source_id', 'controlled'))

    caps = {name: True for name in ('ocr', 'vision', 'tts', 'video', 'answer', 'asr', 'pdf', 'ffmpeg', 'ffprobe')}
    caps.update(image=False, music=False, sfx=False, story=False)
    server.get_capabilities = lambda: dict(caps)
    server.local_tts.synthesize = synthesize
    server.local_asr.transcribe = lambda path, language='zh': '小兔子送了什么？'
    server.video_generator.generate = video_generate
    server.analyzer.call_with_metadata = analyze
    server.book_service.adapters['capabilities'] = lambda: dict(caps)
    server.book_service.adapters['reader_factory'] = ControlledReader
    server.book_service.adapters['answer'] = lambda question, context, history: (
        f"原文写着：{context[0]['excerpt']} [{context[0]['page_id']}]"
    )
    create_fixture(data_dir / 'controlled-mixed.pdf')
    server.app.run(host='127.0.0.1', port=args.port, threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()

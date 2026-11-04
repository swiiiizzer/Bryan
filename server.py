"""Single-instance MP3 conversion server. Ephemeral files, no database required."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
FILES = Path(tempfile.gettempdir()) / 'mp3-exe-jobs'
FILES.mkdir(exist_ok=True)
JOBS = {}
LOCK = threading.Lock()
SLOT = threading.BoundedSemaphore(1)
TTL = 900
MAX_DURATION = 1200
ORIGIN = os.environ.get('FRONTEND_ORIGIN', 'https://mp3-95-bryan.briazitox.chatgpt.site').rstrip('/')


def validate_request(data):
    if not isinstance(data, dict):
        raise ValueError('Envie um link de vídeo válido.')
    video = data.get('video_id')
    quality = str(data.get('quality', '192'))
    if not isinstance(video, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        raise ValueError('O endereço do vídeo não é válido.')
    if quality not in ('128', '192', '320'):
        raise ValueError('Escolha 128, 192 ou 320 kbps.')
    return video, quality


def cleanup():
    now = time.time()
    with LOCK:
        expired = [key for key, job in JOBS.items()
                   if job['status'] in ('ready', 'failed') and now - job['finished'] > TTL]
        for key in expired:
            JOBS.pop(key, None)
            shutil.rmtree(FILES / key, ignore_errors=True)


def run(args, timeout):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)


def conversion(job_id, video, quality):
    folder = FILES / job_id
    folder.mkdir()
    try:
        url = 'https://www.youtube.com/watch?v=' + video
        common = ['python3', '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
                  '--socket-timeout', '20', '--retries', '1', '--no-warnings']
        meta = json.loads(run(common + ['--skip-download', '--dump-single-json', '--', url], 65).stdout)
        duration = meta.get('duration')
        if meta.get('is_live') or meta.get('live_status') in ('is_live', 'is_upcoming'):
            raise ValueError('Transmissões ao vivo não são aceitas.')
        if not isinstance(duration, (int, float)) or duration <= 0 or duration > MAX_DURATION:
            raise ValueError('Escolha um vídeo de até 20 minutos.')
        title = str(meta.get('title') or 'Audio do YouTube')[:180]
        with LOCK:
            JOBS[job_id].update(title=title, status='converting')
        run(common + ['-f', 'bestaudio/best', '--max-filesize', '100M',
                      '--match-filter', 'duration <= 1200 & !is_live',
                      '-x', '--audio-format', 'mp3', '--audio-quality', quality + 'K',
                      '--output', str(folder / 'audio.%(ext)s'), '--', url], 240)
        output = folder / 'audio.mp3'
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError('Não foi possível gerar o MP3 deste vídeo.')
        with LOCK:
            JOBS[job_id].update(status='ready', title=title, finished=time.time())
    except ValueError as exc:
        with LOCK:
            JOBS[job_id].update(status='failed', error=str(exc), finished=time.time())
        shutil.rmtree(folder, ignore_errors=True)
    except subprocess.TimeoutExpired:
        with LOCK:
            JOBS[job_id].update(status='failed', error='O vídeo demorou demais para processar. Tente um vídeo menor.', finished=time.time())
        shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        # Never expose yt-dlp logs, network details, cookies or credentials to visitors.
        with LOCK:
            JOBS[job_id].update(status='failed', error='O YouTube não permitiu baixar este vídeo agora. Ele pode estar restrito ou o acesso do servidor pode ter sido bloqueado.', finished=time.time())
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        SLOT.release()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'dist'), **kwargs)

    def log_message(self, fmt, *args):
        # Avoid logging video IDs or request bodies.
        print('%s %s' % (self.command, self.path.split('?')[0]), flush=True)

    def end_headers(self):
        origin = self.headers.get('Origin', '')
        if origin == ORIGIN:
            self.send_header('Access-Control-Allow-Origin', ORIGIN)
            self.send_header('Vary', 'Origin')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()

    def json_response(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def allowed_origin(self):
        origin = self.headers.get('Origin')
        own = ('https://' + self.headers.get('Host', ''))
        local = ('http://' + self.headers.get('Host', ''))
        return origin is None or origin in (ORIGIN, own, local)

    def do_OPTIONS(self):
        if not self.allowed_origin():
            return self.json_response({'error': 'Origem não autorizada.'}, 403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '600')
        self.end_headers()

    def do_POST(self):
        cleanup()
        if urlsplit(self.path).path != '/api/jobs':
            return self.json_response({'error': 'Endereço não encontrado.'}, 404)
        if not self.allowed_origin():
            return self.json_response({'error': 'Origem não autorizada.'}, 403)
        if self.headers.get_content_type() != 'application/json':
            return self.json_response({'error': 'Envie os dados em JSON.'}, 415)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('Pedido inválido.')
            data = json.loads(self.rfile.read(size))
            video, quality = validate_request(data)
        except (ValueError, UnicodeError) as exc:
            return self.json_response({'error': str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Pedido inválido.'}, 400)
        if not SLOT.acquire(blocking=False):
            return self.json_response({'error': 'O conversor está ocupado. Aguarde a conversão atual terminar e tente novamente.'}, 429)
        key = uuid.uuid4().hex
        with LOCK:
            JOBS[key] = {'id': key, 'status': 'downloading', 'created': time.time(), 'quality': quality}
        threading.Thread(target=conversion, args=(key, video, quality), daemon=True).start()
        self.json_response({'id': key, 'status': 'downloading'}, 202)

    def do_GET(self):
        cleanup()
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.json_response({'status': 'ok'})
        match = re.fullmatch(r'/api/(jobs|download)/([a-f0-9]{32})', path)
        if match:
            kind, key = match.groups()
            with LOCK:
                job = dict(JOBS.get(key, {}))
            if not job:
                return self.json_response({'error': 'Este arquivo expirou. Faça uma nova conversão.'}, 404)
            if kind == 'jobs':
                return self.json_response({k: v for k, v in job.items() if k not in ('created', 'finished')})
            if job['status'] != 'ready':
                return self.json_response({'error': 'O arquivo ainda não está pronto.'}, 409)
            file = FILES / key / 'audio.mp3'
            try:
                stream = file.open('rb')
            except FileNotFoundError:
                return self.json_response({'error': 'Este arquivo expirou. Faça uma nova conversão.'}, 404)
            from urllib.parse import quote
            filename = re.sub(r'[\x00-\x1f/\\]', '_', job.get('title', 'audio'))[:180] + '.mp3'
            with stream:
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Disposition', "attachment; filename=audio.mp3; filename*=UTF-8''" + quote(filename, safe=''))
                self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                self.send_header('Cache-Control', 'private, no-store')
                self.end_headers()
                try:
                    shutil.copyfileobj(stream, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        if path.startswith('/api/'):
            return self.json_response({'error': 'Endereço não encontrado.'}, 404)
        if path.endswith('/') and path != '/':
            return self.json_response({'error': 'Endereço não encontrado.'}, 404)
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith('/api/'):
            self.send_response(405)
            self.send_header('Allow', 'GET, POST, OPTIONS')
            self.end_headers()
        else:
            super().do_HEAD()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8000'))), Handler)
    server.daemon_threads = True
    print('MP3.exe listening', flush=True)
    server.serve_forever()

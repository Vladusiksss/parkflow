"""One managed RTSP worker. Credentials never appear in responses, argv or logs."""
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import Literal


class CameraSettings(BaseModel):
    name: str = Field(default='Камера 01', min_length=1, max_length=80)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    transport: Literal['tcp', 'udp'] = 'tcp'
    inference: bool = False

    @field_validator('rtsp_url')
    @classmethod
    def validate_url(cls, value):
        if value is None or value == '':
            return None  # preserve the saved secret
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if parsed.scheme not in ('rtsp', 'rtsps') or not parsed.hostname or parsed.fragment:
                raise ValueError()
            if any(c.isspace() or ord(c)<32 for c in value) or (port is not None and not 1<=port<=65535):
                raise ValueError()
        except ValueError:
            # Never include the original input in a public error response.
            raise ValueError('Потрібна коректна RTSP-адреса') from None
        return value


class SpacePolygon(BaseModel):
    id: str = Field(min_length=1, max_length=16)
    polygon: list[tuple[float, float]] = Field(min_length=4, max_length=4)

    @field_validator('polygon')
    @classmethod
    def convex_quad(cls, points):
        if any(not math.isfinite(v) or not 0<=v<=1 for p in points for v in p):
            raise ValueError('Координати мають бути в діапазоні 0–1')
        turns = []
        area = 0
        for i in range(4):
            a,b,c = points[i],points[(i+1)%4],points[(i+2)%4]
            turns.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
            area += a[0]*b[1]-a[1]*b[0]
        if abs(area)<.0001 or not (all(t>0 for t in turns) or all(t<0 for t in turns)):
            raise ValueError('Позначте чотири кути опуклого місця по периметру')
        return points


class PolygonSettings(BaseModel):
    spaces: list[SpacePolygon] = Field(max_length=48)


class CameraManager:
    def __init__(self, database, demo, root):
        self.database, self.demo, self.root = database, demo, Path(root)
        self.directory = Path(database.path).resolve().parent / (Path(database.path).stem+'-camera')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.directory/'camera.json'
        self.model_path = Path(os.getenv('PARKFLOW_YOLO_MODEL', str(self.root/'models/yolo11n.pt'))).resolve()
        self.lock = threading.RLock()
        self.process = None
        self.run_dir = None
        self.last_observation = 0
        self.ingestion_error = False
        self.config = {'name':'Камера 01','rtsp_url':None,'transport':'tcp','inference':False,'spaces':[]}
        if self.config_path.exists():
            self.config.update(json.loads(self.config_path.read_text(encoding='utf-8')))

    def capabilities(self):
        return {'opencv':importlib.util.find_spec('cv2') is not None,
                'yolo':importlib.util.find_spec('ultralytics') is not None,
                'model':self.model_path.is_file()}

    def _running(self):
        return self.process is not None and self.process.poll() is None

    def _read(self, filename):
        if not self.run_dir:
            return {}
        try:
            return json.loads((Path(self.run_dir.name)/filename).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def info(self):
        with self.lock:
            report = self._read('status.json') if self._running() else {}
            running = self._running()
            fresh = running and time.time()-report.get('frame_at',0)<5
            phase = report.get('phase','CONNECTING') if running else 'STOPPED'
            if running and phase == 'ONLINE' and not fresh:
                phase = 'RECONNECTING'
            if self.process is not None and self.process.poll() is not None:
                phase = 'FAILED'
            return {'name':self.config['name'], 'configured':bool(self.config['rtsp_url']),
                    'transport':self.config['transport'], 'inference':self.config['inference'],
                    'spaces':self.config['spaces'], 'demo':self.demo,
                    'running':running, 'phase':phase, 'frame_at':report.get('frame_at'),
                    'width':report.get('width'), 'height':report.get('height'),
                    'ai_state':report.get('ai_state','OFF'), 'ingestion_error':self.ingestion_error,
                    'capabilities':self.capabilities()}

    def _persist(self):
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.config,ensure_ascii=False),encoding='utf-8')
        os.replace(temporary,self.config_path)

    def save(self, settings):
        with self.lock:
            if self._running():
                raise HTTPException(409,'Спочатку зупиніть камеру')
            update = settings.model_dump()
            if update['rtsp_url'] is None:
                update['rtsp_url'] = self.config['rtsp_url']
            if not update['rtsp_url']:
                raise HTTPException(422,'Вкажіть RTSP-адресу камери')
            source_changed = update['rtsp_url'] != self.config['rtsp_url']
            self.config.update(update)
            if source_changed:
                self.config['spaces'] = []  # old geometry must never be applied to a new feed
                self._invalidate()
            self._persist()
            return self.info()

    def save_polygons(self, body):
        with self.lock:
            ids = [s.id for s in body.spaces]
            if len(ids)!=len(set(ids)):
                raise HTTPException(422,'Кожне місце можна позначити лише один раз')
            with self.database.connect() as db:
                known = {r[0] for r in db.execute('SELECT id FROM spaces WHERE camera_id=1')}
            if not set(ids)<=known:
                raise HTTPException(422,'Розмітка містить невідоме місце')
            self.stop()
            self.config['spaces'] = [s.model_dump() for s in body.spaces]
            self._persist()
            self._invalidate()
            return self.info()

    def _invalidate(self):
        if not self.demo:
            with self.database.connect() as db:
                db.execute("UPDATE spaces SET physical='UNKNOWN',confidence=0,observed_at=0,candidate=NULL,candidate_since=NULL,candidate_last=NULL WHERE camera_id=1")

    def start(self):
        with self.lock:
            if self._running():
                return self.info()
            caps = self.capabilities()
            if not self.config['rtsp_url']:
                raise HTTPException(409,'Спочатку збережіть RTSP-адресу')
            if not caps['opencv']:
                raise HTTPException(409,'Встановіть модуль камери: запустіть install-camera.cmd і перезапустіть сервер')
            if self.config['inference']:
                if self.demo:
                    raise HTTPException(409,'AI-зайнятість недоступна в деморежимі. Запустіть start-camera.cmd')
                if not self.config['spaces']:
                    raise HTTPException(409,'Спочатку перегляньте кадр і позначте місця; вимкніть AI для перегляду')
                if not caps['yolo'] or not caps['model']:
                    raise HTTPException(409,'Для AI запустіть install-vision.cmd і перезапустіть сервер')
            self.stop()
            self._invalidate()
            self.run_dir = tempfile.TemporaryDirectory(prefix='runtime-',dir=self.directory)
            payload = self.config | {'output':self.run_dir.name,'model':str(self.model_path)}
            env = os.environ.copy()
            env['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;'+self.config['transport']
            env['OPENCV_LOG_LEVEL'] = 'SILENT'
            try:
                self.process = subprocess.Popen([sys.executable,'-m','vision.service'],cwd=self.root,
                    stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    env=env,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                self.process.stdin.write((json.dumps(payload)+'\n').encode())
                self.process.stdin.flush()
            except (OSError, BrokenPipeError):
                self.stop()
                raise HTTPException(503,'Не вдалося запустити процес камери') from None
            self.last_observation = 0
            self.ingestion_error = False
            return self.info()

    def stop(self):
        with self.lock:
            if self.process:
                if self.process.stdin:
                    try:
                        self.process.stdin.close()
                    except OSError:
                        pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
                self.process = None
            if self.run_dir:
                self.run_dir.cleanup()
                self.run_dir = None
                self._invalidate()
            return self.info()

    def frame(self):
        with self.lock:
            report = self._read('status.json')
            if not self._running() or time.time()-report.get('frame_at',0)>5:
                raise HTTPException(503,'Свіжий кадр поки недоступний')
            try:
                return (Path(self.run_dir.name)/'frame.jpg').read_bytes()
            except OSError:
                raise HTTPException(503,'Кадр ще готується') from None

    def ingest(self, consume):
        with self.lock:
            if self.demo or not self._running() or not self.config['inference']:
                return
            batch = self._read('observations.json')
            if not batch or batch.get('observed_at',0)<=self.last_observation:
                return
            self.last_observation = batch['observed_at']
            try:
                consume(batch)
                self.ingestion_error = False
            except (HTTPException, ValueError):
                self.ingestion_error = True

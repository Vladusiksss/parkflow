import asyncio
import hashlib
import math
import mimetypes
import os
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .db import Database, event, expire, public_state
from .camera import CameraManager, CameraSettings, PolygonSettings

ROOT = Path(__file__).resolve().parents[1]
# Windows registry associations can otherwise serve .js as text/plain; nosniff blocks it.
mimetypes.init()
mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('image/svg+xml', '.svg')


class Booking(BaseModel):
    space_id: str = Field(min_length=1, max_length=16)
    plate: str = Field(min_length=4, max_length=16)

    @field_validator('plate')
    @classmethod
    def normalize_plate(cls, value):
        value = re.sub(r'[\s-]', '', value.upper())
        if not re.fullmatch(r'[A-ZА-ЯІЇЄ0-9]{4,12}', value):
            raise ValueError('Номер має містити 4–12 літер або цифр')
        return value


class Observation(BaseModel):
    space_id: str = Field(min_length=1, max_length=16)
    occupied: bool
    confidence: float = Field(ge=0, le=1)


class VisionBatch(BaseModel):
    camera_id: int = Field(ge=1)
    observed_at: float = Field(gt=0)
    spaces: list[Observation] = Field(min_length=1, max_length=200)


class Tariff(BaseModel):
    rate: int = Field(ge=0, le=1000000, description='Копійок за годину')


class SpaceSettings(BaseModel):
    disabled: bool


class DemoStatus(BaseModel):
    status: Literal['FREE', 'OCCUPIED', 'UNKNOWN']


def create_app(db_path=None, demo=None, admin_key=None, vision_key=None):
    demo = os.getenv('PARKFLOW_DEMO', '0') == '1' if demo is None else demo
    admin_key = admin_key or os.getenv('PARKFLOW_ADMIN_KEY')
    vision_key = vision_key or os.getenv('PARKFLOW_VISION_KEY')
    if not admin_key or not vision_key or admin_key == vision_key:
        raise RuntimeError('Set distinct PARKFLOW_ADMIN_KEY and PARKFLOW_VISION_KEY (or use python run.py).')
    database = Database(db_path or os.getenv('PARKFLOW_DB', str(ROOT/'data/parkflow.db')), demo)
    camera = CameraManager(database, demo, ROOT)

    def snapshot():
        with database.connect() as db:
            return public_state(db, time.time(), demo)

    async def maintenance():
        while True:
            await asyncio.to_thread(camera.ingest, lambda batch: occupancy(VisionBatch.model_validate(batch)))
            with database.connect() as db:
                now = time.time()
                state = public_state(db, now, demo)
                db.execute('INSERT OR REPLACE INTO history VALUES(?,?,?,?)',
                           (int(now//60)*60, state['counts']['FREE'], state['counts']['OCCUPIED'], len(state['spaces'])))
                db.execute('DELETE FROM history WHERE minute<?', (now-7*86400,))
                db.execute('DELETE FROM events WHERE at<?', (now-7*86400,))
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(camera.stop)

    app = FastAPI(title='ParkFlow MVP', version='0.2.0', lifespan=lifespan)
    app.state.database = database
    app.state.camera = camera

    # Pydantic's default error body echoes input; a bad RTSP URL may contain a password.
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith('/api/admin/camera'):
            return JSONResponse({'detail':'Перевірте адресу, поля камери та координати полігонів'}, status_code=422)
        return JSONResponse({'detail':'Перевірте введені дані'},status_code=422)

    def require_key(expected, authorization):
        supplied = (authorization or '').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(403, 'Недостатньо прав доступу')

    def admin(authorization: str | None = Header(default=None)):
        require_key(admin_key, authorization)

    def vision(authorization: str | None = Header(default=None)):
        require_key(vision_key, authorization)

    def driver(request: Request):
        token = request.cookies.get('parkflow_driver', '')
        ident = hashlib.sha256(token.encode()).hexdigest()
        with database.connect() as db:
            if not token or not db.execute('SELECT 1 FROM drivers WHERE id=?', (ident,)).fetchone():
                raise HTTPException(401, 'Спочатку відкрийте застосунок водія')
        return ident

    @app.middleware('http')
    async def security(request: Request, call_next):
        # Browser mutations require a same-origin custom header, including guest creation.
        if request.method in ('POST','PUT','PATCH','DELETE') and not request.headers.get('authorization'):
            origin = request.headers.get('origin')
            if request.headers.get('x-parkflow') != '1' or (origin and origin != str(request.base_url).rstrip('/')):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Invalid request origin'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path not in ('/docs', '/redoc', '/docs/oauth2-redirect'):
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'demo': demo}

    @app.post('/api/guest')
    def guest(request: Request, response: Response):
        token = request.cookies.get('parkflow_driver') or secrets.token_urlsafe(32)
        ident = hashlib.sha256(token.encode()).hexdigest()
        with database.connect() as db:
            db.execute('INSERT OR IGNORE INTO drivers VALUES(?,?)', (ident, time.time()))
        response.set_cookie('parkflow_driver', token, httponly=True, samesite='strict', secure=request.url.scheme == 'https', max_age=30*86400)
        return {'ok': True}

    @app.get('/api/parkings')
    def parkings():
        state = snapshot()
        return [state['parking'] | {'counts': state['counts']}]

    @app.get('/api/parkings/1')
    def parking():
        return snapshot()

    @app.get('/api/parkings/1/spaces')
    def spaces():
        return snapshot()['spaces']

    @app.get('/api/me')
    def me(ident=Depends(driver)):
        with database.connect() as db:
            expire(db, time.time())
            reservations = [dict(r) for r in db.execute('SELECT * FROM reservations WHERE driver_id=? ORDER BY created_at DESC LIMIT 10', (ident,))]
            sessions = [dict(r) for r in db.execute('SELECT * FROM sessions WHERE driver_id=? ORDER BY started_at DESC LIMIT 10', (ident,))]
        return {'reservations': reservations, 'sessions': sessions}

    @app.post('/api/reservations', status_code=201)
    def reserve(body: Booking, ident=Depends(driver)):
        with database.connect() as db:
            now = time.time()
            state = public_state(db, now, demo)
            space = next((s for s in state['spaces'] if s['id'] == body.space_id), None)
            if not space:
                raise HTTPException(404, 'Місце не знайдено')
            if space['status'] != 'FREE':
                raise HTTPException(409, 'Місце вже недоступне. Оберіть інше.')
            if db.execute("SELECT 1 FROM reservations WHERE driver_id=? AND state='ACTIVE'", (ident,)).fetchone() or db.execute('SELECT 1 FROM sessions WHERE driver_id=? AND ended_at IS NULL', (ident,)).fetchone():
                raise HTTPException(409, 'У вас уже є активна бронь або паркування')
            rid = secrets.token_urlsafe(16)
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?)', (rid, ident, body.space_id, body.plate, now, now+900, 'ACTIVE'))
            event(db, 'RESERVED', {'space': body.space_id})
            return dict(db.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone())

    @app.delete('/api/reservations/{rid}')
    def cancel(rid: str, ident=Depends(driver)):
        with database.connect() as db:
            expire(db, time.time())
            row = db.execute('SELECT * FROM reservations WHERE id=? AND driver_id=?', (rid, ident)).fetchone()
            if not row:
                raise HTTPException(404, 'Бронювання не знайдено')
            if row['state'] == 'ACTIVE':
                db.execute("UPDATE reservations SET state='CANCELLED' WHERE id=?", (rid,))
                event(db, 'CANCELLED', {'space': row['space_id']})
        return {'ok': True}

    @app.post('/api/vision/occupancy', dependencies=[Depends(vision)])
    def occupancy(body: VisionBatch):
        now = time.time()
        if body.observed_at < now-15 or body.observed_at > now+2:
            raise HTTPException(422, 'Frame timestamp is stale or in the future')
        if len({s.space_id for s in body.spaces}) != len(body.spaces):
            raise HTTPException(422, 'Duplicate space in frame')
        with database.connect() as db:
            for obs in body.spaces:
                row = db.execute('SELECT * FROM spaces WHERE id=? AND camera_id=?', (obs.space_id, body.camera_id)).fetchone()
                if not row:
                    raise HTTPException(404, 'Space is not assigned to this camera')
                if body.observed_at <= row['observed_at']:
                    raise HTTPException(409, 'Out-of-order frame')
                target = 'UNKNOWN' if obs.confidence < .7 else 'OCCUPIED' if obs.occupied else 'FREE'
                continuous = row['candidate'] == target and row['candidate_last'] is not None and body.observed_at-row['candidate_last'] <= 3
                since = row['candidate_since'] if continuous else body.observed_at
                hold = 5 if target == 'OCCUPIED' else 8
                previous = row['physical'] if body.observed_at-row['observed_at'] <= 30 else 'UNKNOWN'
                physical = target if target == 'UNKNOWN' or body.observed_at-since >= hold else previous
                db.execute('UPDATE spaces SET physical=?,confidence=?,observed_at=?,candidate=?,candidate_since=?,candidate_last=? WHERE id=?',
                           (physical, obs.confidence, body.observed_at, target, since, body.observed_at, obs.space_id))
                if physical != row['physical']:
                    event(db, 'VISION', {'space': obs.space_id, 'status': physical})
        return {'ok': True, 'processed': len(body.spaces)}

    @app.get('/api/admin', dependencies=[Depends(admin)])
    def dashboard():
        with database.connect() as db:
            state = public_state(db, time.time(), demo)
            reservations = [dict(r) for r in db.execute("SELECT * FROM reservations WHERE state='ACTIVE' ORDER BY created_at DESC")]
            sessions = [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY started_at DESC LIMIT 50')]
            history = [dict(r) for r in db.execute('SELECT * FROM history ORDER BY minute DESC LIMIT 120')][::-1]
            events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 12')]
            billed = db.execute('SELECT COALESCE(SUM(amount),0) FROM sessions WHERE ended_at IS NOT NULL').fetchone()[0]
        return state | {'reservations': reservations, 'sessions': sessions, 'history': history, 'events': events, 'billed': billed, 'paid': 0}

    @app.get('/api/admin/camera', dependencies=[Depends(admin)])
    def camera_info():
        return camera.info()

    @app.put('/api/admin/camera', dependencies=[Depends(admin)])
    def save_camera(body: CameraSettings):
        return camera.save(body)

    @app.put('/api/admin/camera/polygons', dependencies=[Depends(admin)])
    def save_camera_polygons(body: PolygonSettings):
        return camera.save_polygons(body)

    @app.post('/api/admin/camera/start', dependencies=[Depends(admin)])
    def start_camera():
        return camera.start()

    @app.post('/api/admin/camera/stop', dependencies=[Depends(admin)])
    def stop_camera():
        return camera.stop()

    @app.get('/api/admin/camera/frame', dependencies=[Depends(admin)])
    def camera_frame():
        return Response(camera.frame(),media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    @app.patch('/api/admin/tariff', dependencies=[Depends(admin)])
    def tariff(body: Tariff):
        with database.connect() as db:
            db.execute('UPDATE parking SET rate=? WHERE id=1', (body.rate,))
            event(db, 'TARIFF', {'rate': body.rate})
        return {'ok': True}

    @app.patch('/api/admin/spaces/{sid}', dependencies=[Depends(admin)])
    def settings(sid: str, body: SpaceSettings):
        with database.connect() as db:
            state = public_state(db, time.time(), demo)
            if sid not in {s['id'] for s in state['spaces']}:
                raise HTTPException(404, 'Місце не знайдено')
            if body.disabled and (db.execute("SELECT 1 FROM reservations WHERE space_id=? AND state='ACTIVE'", (sid,)).fetchone() or db.execute('SELECT 1 FROM sessions WHERE space_id=? AND ended_at IS NULL', (sid,)).fetchone()):
                raise HTTPException(409, 'Місце має активну бронь або сесію')
            db.execute('UPDATE spaces SET disabled=? WHERE id=?', (body.disabled, sid))
            event(db, 'SPACE_SETTINGS', {'space': sid, 'disabled': body.disabled})
        return {'ok': True}

    @app.post('/api/admin/demo/{sid}', dependencies=[Depends(admin)])
    def demo_status(sid: str, body: DemoStatus):
        if not demo:
            raise HTTPException(403, 'Демонстраційний режим вимкнено')
        with database.connect() as db:
            if not db.execute('SELECT 1 FROM spaces WHERE id=?', (sid,)).fetchone():
                raise HTTPException(404, 'Місце не знайдено')
            db.execute('UPDATE spaces SET physical=?, observed_at=?, confidence=.98 WHERE id=?', (body.status, time.time(), sid))
            event(db, 'DEMO', {'space': sid, 'status': body.status})
        return {'ok': True}

    @app.post('/api/admin/entry/{rid}', dependencies=[Depends(admin)])
    def entry(rid: str):
        with database.connect() as db:
            now = time.time()
            expire(db, now)
            existing = db.execute('SELECT * FROM sessions WHERE reservation_id=?', (rid,)).fetchone()
            if existing:
                return dict(existing)
            row = db.execute("SELECT * FROM reservations WHERE id=? AND state='ACTIVE'", (rid,)).fetchone()
            if not row:
                raise HTTPException(409, 'Немає активної броні')
            physical = db.execute('SELECT * FROM spaces WHERE id=?', (row['space_id'],)).fetchone()
            if physical['disabled'] or physical['physical'] != 'FREE' or (not demo and now-physical['observed_at']>30):
                raise HTTPException(409, 'Місце зайняте або потребує перевірки')
            sid = secrets.token_urlsafe(16)
            rate = db.execute('SELECT rate FROM parking WHERE id=1').fetchone()[0]
            db.execute('INSERT INTO sessions(id,reservation_id,driver_id,space_id,started_at,rate) VALUES(?,?,?,?,?,?)',
                       (sid, rid, row['driver_id'], row['space_id'], now, rate))
            db.execute("UPDATE reservations SET state='CONSUMED' WHERE id=?", (rid,))
            event(db, 'ENTRY', {'space': row['space_id']})
            return dict(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())

    @app.post('/api/admin/exit/{sid}', dependencies=[Depends(admin)])
    def leave(sid: str):
        with database.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Сесію не знайдено')
            if row['ended_at'] is None:
                now = time.time()
                minutes = max(1, math.ceil((now-row['started_at'])/60))
                amount = (minutes*row['rate']+59)//60
                db.execute('UPDATE sessions SET ended_at=?,amount=? WHERE id=?', (now, amount, sid))
                event(db, 'EXIT', {'space': row['space_id'], 'amount': amount})
            return dict(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())

    @app.websocket('/ws')
    async def websocket(ws: WebSocket):
        await ws.accept()
        try:
            # Only anonymous occupancy data is broadcast; personal data uses authenticated HTTP.
            while True:
                await ws.send_json(snapshot())
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=1)
                except asyncio.TimeoutError:
                    pass
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get('/')
    def index():
        return FileResponse(ROOT/'frontend/index.html')

    @app.get('/admin')
    @app.get('/admin/')
    def admin_page():
        return FileResponse(ROOT/'frontend/admin.html')

    app.mount('/static', StaticFiles(directory=ROOT/'frontend'), name='static')
    return app

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS parking (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
 lat REAL NOT NULL, lng REAL NOT NULL, rate INTEGER NOT NULL CHECK(rate>=0));
CREATE TABLE IF NOT EXISTS spaces (
 id TEXT PRIMARY KEY, floor INTEGER NOT NULL, kind TEXT NOT NULL,
 distance INTEGER NOT NULL, camera_id INTEGER NOT NULL DEFAULT 1,
 physical TEXT NOT NULL DEFAULT 'UNKNOWN', confidence REAL NOT NULL DEFAULT 0,
 observed_at REAL NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
 candidate TEXT, candidate_since REAL, candidate_last REAL);
CREATE TABLE IF NOT EXISTS drivers (id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reservations (
 id TEXT PRIMARY KEY, driver_id TEXT NOT NULL REFERENCES drivers(id),
 space_id TEXT NOT NULL REFERENCES spaces(id), plate TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_space ON reservations(space_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS active_driver ON reservations(driver_id) WHERE state='ACTIVE';
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, reservation_id TEXT UNIQUE REFERENCES reservations(id),
 driver_id TEXT NOT NULL REFERENCES drivers(id), space_id TEXT NOT NULL REFERENCES spaces(id),
 started_at REAL NOT NULL, ended_at REAL, rate INTEGER NOT NULL,
 amount INTEGER, payment_status TEXT NOT NULL DEFAULT 'NOT_CONNECTED');
CREATE UNIQUE INDEX IF NOT EXISTS session_space ON sessions(space_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS session_driver ON sessions(driver_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS history (
 minute INTEGER PRIMARY KEY, free INTEGER NOT NULL, occupied INTEGER NOT NULL, total INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
"""


class Database:
    def __init__(self, path, demo=False):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO parking VALUES (1,?,?,?,?,?)",
                       ('ParkFlow · Центральний', 'Київ, вул. Антоновича, 176', 50.4117, 30.5226, 4000))
            for floor in (1, 2):
                for row in 'ABC':
                    for n in range(1, 9):
                        ident = f'{floor}-{row}{n:02}'
                        kind = 'EV' if row == 'C' and n <= 2 else 'ACCESSIBLE' if row == 'A' and n == 1 else 'STANDARD'
                        physical = ('OCCUPIED' if (n + ord(row) + floor) % 3 == 0 else 'FREE') if demo else 'UNKNOWN'
                        db.execute('INSERT OR IGNORE INTO spaces(id,floor,kind,distance,physical,confidence,observed_at) VALUES(?,?,?,?,?,?,?)',
                                   (ident, floor, kind, 20 + (ord(row)-65)*25+n*5, physical, .98 if demo else 0, time.time() if demo else 0))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def event(db, kind, detail):
    db.execute('INSERT INTO events(at,kind,detail) VALUES(?,?,?)', (time.time(), kind, json.dumps(detail, ensure_ascii=False)))


def expire(db, now):
    db.execute("UPDATE reservations SET state='EXPIRED' WHERE state='ACTIVE' AND expires_at<=?", (now,))


def public_state(db, now, demo):
    expire(db, now)
    parking = dict(db.execute('SELECT * FROM parking').fetchone())
    reserved = {r[0] for r in db.execute("SELECT space_id FROM reservations WHERE state='ACTIVE'")}
    sessions = {r[0] for r in db.execute('SELECT space_id FROM sessions WHERE ended_at IS NULL')}
    spaces = []
    for row in db.execute('SELECT * FROM spaces ORDER BY id'):
        s = dict(row)
        physical = s['physical'] if demo or now-s['observed_at'] <= 30 else 'UNKNOWN'
        status = 'DISABLED' if s['disabled'] else 'OCCUPIED' if s['id'] in sessions or physical == 'OCCUPIED' else 'UNKNOWN' if physical == 'UNKNOWN' else 'RESERVED' if s['id'] in reserved else 'FREE'
        spaces.append({k: s[k] for k in ('id','floor','kind','distance','camera_id','confidence','observed_at')} | {'status': status})
    counts = {state: sum(s['status'] == state for s in spaces) for state in ('FREE','OCCUPIED','RESERVED','UNKNOWN','DISABLED')}
    return {'parking': parking, 'spaces': spaces, 'counts': counts, 'server_time': now, 'demo': demo}

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app

ADMIN = {'Authorization':'Bearer admin-test'}
VISION = {'Authorization':'Bearer vision-test'}
DRIVER = {'X-ParkFlow':'1'}


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path/'test.db', demo=True, admin_key='admin-test', vision_key='vision-test')


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers.update(DRIVER)
        c.post('/api/guest')
        yield c


def free_space(client):
    return next(s['id'] for s in client.get('/api/parkings/1').json()['spaces'] if s['status']=='FREE')


def book(client, space=None):
    return client.post('/api/reservations',json={'space_id':space or free_space(client),'plate':'КА 1234 АВ'})


def status(client, ident):
    return next(s['status'] for s in client.get('/api/parkings/1').json()['spaces'] if s['id']==ident)


def test_reserve_cancel_and_private_ownership(client, app):
    r=book(client).json()
    assert status(client,r['space_id'])=='RESERVED'
    assert book(client).status_code==409
    with TestClient(app) as other:
        other.headers.update(DRIVER)
        other.post('/api/guest')
        assert other.delete('/api/reservations/'+r['id']).status_code==404
        assert other.get('/api/me').json()['reservations']==[]
    assert client.delete('/api/reservations/'+r['id']).status_code==200
    assert status(client,r['space_id'])=='FREE'


def test_concurrent_booking_only_one_wins(client, app):
    space=free_space(client)
    # Distinct browsers contend for the same physical space.
    def attempt(n):
        with TestClient(app) as c:
            c.headers.update(DRIVER)
            c.post('/api/guest')
            return book(c,space).status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes=list(pool.map(attempt,range(6)))
    assert codes.count(201)==1
    assert codes.count(409)==5


def test_expiration_preserves_occupancy(client, app):
    r=book(client).json()
    client.post('/api/admin/demo/'+r['space_id'],headers=ADMIN,json={'status':'OCCUPIED'})
    with app.state.database.connect() as db:
        db.execute('UPDATE reservations SET expires_at=? WHERE id=?',(time.time()-1,r['id']))
    assert status(client,r['space_id'])=='OCCUPIED'
    assert client.get('/api/me').json()['reservations'][0]['state']=='EXPIRED'


def test_sessions_snapshot_rate_idempotency_and_billing(client, app):
    r=book(client).json()
    s=client.post('/api/admin/entry/'+r['id'],headers=ADMIN).json()
    assert client.post('/api/admin/entry/'+r['id'],headers=ADMIN).json()['id']==s['id']
    assert status(client,r['space_id'])=='OCCUPIED'
    client.patch('/api/admin/tariff',headers=ADMIN,json={'rate':10000})
    with app.state.database.connect() as db:
        db.execute('UPDATE sessions SET started_at=? WHERE id=?',(time.time()-137*60+2,s['id']))
    finished=client.post('/api/admin/exit/'+s['id'],headers=ADMIN).json()
    assert finished['amount']==9134 # 137 min * 4000 kopecks / 60, round up kopeck
    assert finished['rate']==4000
    assert finished['payment_status']=='NOT_CONNECTED'
    assert client.post('/api/admin/exit/'+s['id'],headers=ADMIN).json()['amount']==9134
    assert client.get('/api/admin',headers=ADMIN).json()['paid']==0


def test_roles_origin_and_validation(client):
    assert client.get('/api/admin').status_code==403
    assert client.get('/api/admin',headers=VISION).status_code==403
    assert client.post('/api/reservations',headers={'Origin':'https://evil.test'},json={'space_id':'1-A01','plate':'AA1234AA'}).status_code==403
    assert client.post('/api/reservations',json={'space_id':free_space(client),'plate':'<script>'}).status_code==422
    assert client.patch('/api/admin/tariff',headers=ADMIN,json={'rate':-1}).status_code==422
    assert client.get('/static/../data/local-keys.json').status_code==404
    assert client.get('/static/app.js').headers['content-type'].startswith('text/javascript')
    assert client.get('/static/style.css').headers['content-type'].startswith('text/css')


def test_websocket_does_not_leak_plate_or_identity(client):
    reservation=book(client).json()
    with client.websocket_connect('/ws') as ws:
        snapshot=ws.receive_json()
        assert len(snapshot['spaces'])==48
        assert 'plate' not in str(snapshot)
        assert reservation['driver_id'] not in str(snapshot)


def test_disable_and_entry_conflict(client):
    space=free_space(client)
    assert client.patch('/api/admin/spaces/'+space,headers=ADMIN,json={'disabled':True}).status_code==200
    assert book(client,space).status_code==409
    client.patch('/api/admin/spaces/'+space,headers=ADMIN,json={'disabled':False})
    r=book(client,space).json()
    assert client.patch('/api/admin/spaces/'+space,headers=ADMIN,json={'disabled':True}).status_code==409
    client.post('/api/admin/demo/'+space,headers=ADMIN,json={'status':'OCCUPIED'})
    assert client.post('/api/admin/entry/'+r['id'],headers=ADMIN).status_code==409


@pytest.fixture
def live(tmp_path):
    app=create_app(tmp_path/'live.db',demo=False,admin_key='admin-test',vision_key='vision-test')
    with TestClient(app) as c:
        yield c,app


def observe(c, at, occupied=False, confidence=.95, ids=None, camera=1):
    return c.post('/api/vision/occupancy',headers=VISION,json={'camera_id':camera,'observed_at':at,'spaces':ids or [{'space_id':'1-A01','occupied':occupied,'confidence':confidence}]})


def test_vision_stability_unknown_stale_and_frame_order(live):
    c,app=live
    assert status(c,'1-A01')=='UNKNOWN'
    start=time.time()-12
    for i in range(9):
        assert observe(c,start+i).status_code==200
    assert status(c,'1-A01')=='FREE'
    assert observe(c,start+8).status_code==409
    assert observe(c,start+9,confidence=.5).status_code==200
    assert status(c,'1-A01')=='UNKNOWN'
    with app.state.database.connect() as db:
        db.execute("UPDATE spaces SET physical='FREE',observed_at=? WHERE id='1-A01'",(time.time()-31,))
    assert status(c,'1-A01')=='UNKNOWN'
    assert observe(c,time.time()-16).status_code==422
    assert c.post('/api/admin/demo/1-A01',headers=ADMIN,json={'status':'FREE'}).status_code==403


def test_vision_hysteresis_gap_and_rollback(live):
    c,app=live
    start=time.time()-14
    for delta in (0,1,2,7,8,9):
        assert observe(c,start+delta,occupied=True).status_code==200
    assert status(c,'1-A01')=='UNKNOWN' # gap restarted confirmation timer
    for delta in (10,11,12):
        observe(c,start+delta,occupied=True)
    assert status(c,'1-A01')=='OCCUPIED'
    bad=[{'space_id':'1-A01','occupied':False,'confidence':.1},{'space_id':'nonexistent','occupied':False,'confidence':.9}]
    assert observe(c,start+13,ids=bad).status_code==404
    assert status(c,'1-A01')=='OCCUPIED' # entire batch rolled back
    assert observe(c,start+13,camera=2).status_code==404

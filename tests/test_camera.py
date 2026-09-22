import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.camera import CameraSettings, PolygonSettings

ADMIN={'Authorization':'Bearer admin-test'}
VISION={'Authorization':'Bearer vision-test'}
SECRET_URL='rtsp://operator:private-password@192.0.2.10:554/stream?token=private-token'
SQUARE={'id':'1-A01','polygon':[[.1,.1],[.3,.1],[.3,.4],[.1,.4]]}


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path/'camera-test.db',demo=False,admin_key='admin-test',vision_key='vision-test')


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_separate_pages_and_camera_authorization(client):
    driver=client.get('/').text
    admin=client.get('/admin').text
    assert 'id="admin-login"' not in driver
    assert '/static/admin.js' not in driver
    assert 'id="camera-form"' in admin
    assert 'id="booking-form"' not in admin
    assert 'id="admin-login"' in admin
    for endpoint in ('/api/admin/camera','/api/admin/camera/frame'):
        assert client.get(endpoint).status_code==403
        assert client.get(endpoint,headers=VISION).status_code==403
    for endpoint in ('/api/admin/camera/start','/api/admin/camera/stop'):
        assert client.post(endpoint,headers=VISION).status_code==403
    assert client.put('/api/admin/camera',headers=VISION,json={'rtsp_url':SECRET_URL}).status_code==403


def test_credentials_are_write_only_and_persist(client,app):
    response=client.put('/api/admin/camera',headers=ADMIN,json={'rtsp_url':SECRET_URL,'name':'Північна камера'})
    assert response.status_code==200
    assert response.json()['configured']
    assert 'private-' not in response.text
    assert '192.0.2' not in response.text
    assert 'private-' not in client.get('/api/admin/camera',headers=ADMIN).text
    assert 'private-' not in client.get('/api/parkings/1').text
    # Blank URL preserves the secret while other settings change.
    assert client.put('/api/admin/camera',headers=ADMIN,json={'name':'Нова назва','transport':'udp','rtsp_url':''}).status_code==200
    saved=json.loads(app.state.camera.config_path.read_text(encoding='utf-8'))
    assert saved['rtsp_url']==SECRET_URL
    assert saved['transport']=='udp'
    restarted=create_app(app.state.database.path,demo=False,admin_key='admin-test',vision_key='vision-test')
    assert restarted.state.camera.config['rtsp_url']==SECRET_URL
    assert client.get('/static/../data/camera.json').status_code==404


@pytest.mark.parametrize('url',['file:///secret','http://127.0.0.1','rtsp://','rtsp://user:private-password@host:bad/','rtsp://host/\n'])
def test_invalid_source_never_echoes_credentials(client,url):
    response=client.put('/api/admin/camera',headers=ADMIN,json={'rtsp_url':url})
    assert response.status_code==422
    assert 'private-password' not in response.text


def test_polygon_validation_and_source_change(client,app):
    client.put('/api/admin/camera',headers=ADMIN,json={'rtsp_url':SECRET_URL})
    assert client.put('/api/admin/camera/polygons',headers=ADMIN,json={'spaces':[SQUARE]}).status_code==200
    invalid=[
        [SQUARE,SQUARE],
        [SQUARE|{'id':'not-a-space'}],
        [SQUARE|{'polygon':[[0,0],[1,1],[1,0],[0,1]]}],
        [SQUARE|{'polygon':[[0,0],[2,0],[1,1],[0,1]]}],
        [SQUARE|{'polygon':[[0,0],[0,0],[0,0],[0,0]]}],
    ]
    for spaces in invalid:
        assert client.put('/api/admin/camera/polygons',headers=ADMIN,json={'spaces':spaces}).status_code==422
    assert len(app.state.camera.config['spaces'])==1
    with app.state.database.connect() as db:
        db.execute("UPDATE spaces SET physical='FREE', observed_at=? WHERE id='1-A01'",(time.time(),))
    client.put('/api/admin/camera',headers=ADMIN,json={'rtsp_url':'rtsp://192.0.2.11/other'})
    assert app.state.camera.config['spaces']==[]
    first=client.get('/api/parkings/1').json()['spaces'][0]
    assert first['status']=='UNKNOWN'


def test_start_dependencies_and_ai_preconditions(client,app,monkeypatch):
    assert client.post('/api/admin/camera/start',headers=ADMIN).status_code==409
    client.put('/api/admin/camera',headers=ADMIN,json={'rtsp_url':SECRET_URL})
    monkeypatch.setattr(app.state.camera,'capabilities',lambda:{'opencv':False,'yolo':False,'model':False})
    assert client.post('/api/admin/camera/start',headers=ADMIN).status_code==409
    monkeypatch.setattr(app.state.camera,'capabilities',lambda:{'opencv':True,'yolo':False,'model':False})
    client.put('/api/admin/camera',headers=ADMIN,json={'inference':True})
    assert client.post('/api/admin/camera/start',headers=ADMIN).status_code==409
    client.put('/api/admin/camera/polygons',headers=ADMIN,json={'spaces':[SQUARE]})
    assert client.post('/api/admin/camera/start',headers=ADMIN).status_code==409
    assert client.get('/api/admin/camera/frame',headers=ADMIN).status_code==503


def test_demo_cannot_mix_ai_and_synthetic_states(tmp_path,monkeypatch):
    app=create_app(tmp_path/'demo.db',demo=True,admin_key='admin-test',vision_key='vision-test')
    monkeypatch.setattr(app.state.camera,'capabilities',lambda:{'opencv':True,'yolo':True,'model':True})
    app.state.camera.save(CameraSettings(rtsp_url=SECRET_URL,inference=True))
    with TestClient(app) as c:
        assert c.post('/api/admin/camera/start',headers=ADMIN).status_code==409


def test_managed_worker_decodes_video_and_cleans_up(client,app,tmp_path):
    cv2=pytest.importorskip('cv2')
    np=pytest.importorskip('numpy')
    video=tmp_path/'camera-fixture.avi'
    writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),10,(320,180))
    assert writer.isOpened()
    for i in range(30):
        frame=np.full((180,320,3),70,dtype=np.uint8)
        cv2.rectangle(frame,(20+i*2,50),(100+i*2,120),(70,180,220),-1)
        writer.write(frame)
    writer.release()
    # Test seam only: public API rejects file paths. Exercise actual capture/IPC/encoding.
    app.state.camera.config['rtsp_url']=str(video)
    response=client.post('/api/admin/camera/start',headers=ADMIN)
    assert response.status_code==200
    original_process=app.state.camera.process
    assert client.post('/api/admin/camera/start',headers=ADMIN).status_code==200
    assert app.state.camera.process is original_process
    assert client.put('/api/admin/camera',headers=ADMIN,json={'name':'busy'}).status_code==409
    deadline=time.monotonic()+15
    frame=None
    while time.monotonic()<deadline:
        r=client.get('/api/admin/camera/frame',headers=ADMIN)
        if r.status_code==200:
            frame=r.content
            assert r.headers['cache-control']=='no-store'
            break
        time.sleep(.1)
    assert frame is not None, app.state.camera.info()
    decoded=cv2.imdecode(np.frombuffer(frame,np.uint8),cv2.IMREAD_COLOR)
    assert decoded.shape[:2]==(180,320)
    runtime=Path(app.state.camera.run_dir.name)
    assert runtime.exists()
    assert client.post('/api/admin/camera/stop',headers=ADMIN).status_code==200
    assert original_process.poll() is not None
    assert not runtime.exists()
    assert client.get('/api/admin/camera/frame',headers=ADMIN).status_code==503


def test_ingestion_and_stale_frames(client,app,tmp_path):
    manager=app.state.camera
    class Running:
        stdin=None
        def poll(self): return None
        def wait(self,timeout): return 0
    import tempfile
    manager.process=Running()
    manager.run_dir=tempfile.TemporaryDirectory(dir=tmp_path)
    manager.config['inference']=True
    at=time.time()-10
    # Use the same validated occupancy endpoint as managed ingestion does.
    def consume(batch):
        response=client.post('/api/vision/occupancy',headers=VISION,json=batch)
        response.raise_for_status()
    for i in range(9):
        batch={'camera_id':1,'observed_at':at+i,'spaces':[{'space_id':'1-A01','occupied':False,'confidence':.9}]}
        (Path(manager.run_dir.name)/'observations.json').write_text(json.dumps(batch))
        manager.ingest(consume)
    assert client.get('/api/parkings/1').json()['spaces'][0]['status']=='FREE'
    # Preview access independently refuses an old frame even if the child is still alive.
    (Path(manager.run_dir.name)/'status.json').write_text(json.dumps({'frame_at':time.time()-6}))
    assert client.get('/api/admin/camera/frame',headers=ADMIN).status_code==503
    manager.stop()
    assert client.get('/api/parkings/1').json()['spaces'][0]['status']=='UNKNOWN'


def test_ai_service_maps_predictions_to_batch(tmp_path,monkeypatch):
    np=pytest.importorskip('numpy')
    pytest.importorskip('cv2')
    import sys
    import threading
    from types import SimpleNamespace
    from vision import service
    stopped=threading.Event()
    # Stub just the model: exercise frame quality, polygon mapping, encoding and IPC output.
    box=SimpleNamespace(xyxy=np.array([[32,18,96,72]]),conf=np.array([.95]))
    class Model:
        names={2:'car'}
        def __init__(self,path): pass
        def predict(self,*args,**kwargs): return [SimpleNamespace(boxes=[box])]
    class Stream:
        def __init__(self,source): pass
        def latest(self):
            frame=np.tile(np.arange(320,dtype=np.uint16)%200,(180,1)).astype(np.uint8)
            return time.time(),np.stack([frame]*3,axis=2)
        def close(self): pass
    monkeypatch.setitem(sys.modules,'ultralytics',SimpleNamespace(YOLO=Model))
    monkeypatch.setattr(service,'LatestFrame',Stream)
    write=service.atomic
    def save(path,data):
        write(path,data)
        if path.name=='observations.json':stopped.set()
    monkeypatch.setattr(service,'atomic',save)
    service.run({'output':str(tmp_path),'inference':True,'model':'fixture.pt','rtsp_url':SECRET_URL,'spaces':[SQUARE]},stopped)
    batch=json.loads((tmp_path/'observations.json').read_text())
    assert batch['spaces']==[{'space_id':'1-A01','occupied':True,'confidence':.95}]
    assert json.loads((tmp_path/'status.json').read_text())['ai_state']=='RUNNING'
    assert 'private-password' not in (tmp_path/'status.json').read_text()

"""RTSP/video -> YOLO -> normalized polygons -> authenticated occupancy API.

Run from repository root: python -m vision.worker --config vision/spaces.example.json
"""
import argparse
import json
import logging
import os
from pathlib import Path
import threading
import time

import cv2
import httpx

from .geometry import classify, polygon_array

log = logging.getLogger('parkflow.vision')


class LatestFrame:
    """Drain the stream on a dedicated thread to avoid processing buffered, stale frames."""
    def __init__(self, source):
        self.source = source
        self.lock = threading.Lock()
        self.frame = None
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.capture, daemon=True)
        self.thread.start()

    def capture(self):
        while not self.closed.is_set():
            cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
            if not cap.isOpened():
                cap.release()
                log.warning('Camera unavailable; retrying in 3 seconds')
                self.closed.wait(3)
                continue
            try:
                is_file = Path(self.source).is_file() if not '://' in self.source else False
                fps = cap.get(cv2.CAP_PROP_FPS) or 25
                while not self.closed.is_set():
                    started = time.monotonic()
                    ok, frame = cap.read()
                    if not ok:
                        break
                    with self.lock:
                        self.frame = (time.time(), frame)
                    if is_file:
                        self.closed.wait(max(0, 1/fps-(time.monotonic()-started)))
            finally:
                cap.release()
                with self.lock:
                    self.frame = None
            self.closed.wait(1)

    def latest(self):
        with self.lock:
            return self.frame

    def close(self):
        self.closed.set()
        self.thread.join(timeout=6)


def main():
    from ultralytics import YOLO
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--model', default='yolo11n.pt')
    parser.add_argument('--api', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    config = json.loads(open(args.config, encoding='utf-8').read())
    if config.get('example_only', False):
        raise SystemExit('Calibrate real polygons first; example_only must be false in your own config.')
    key = os.environ.get('PARKFLOW_VISION_KEY')
    source = os.environ.get('PARKFLOW_CAMERA_URL')
    if not key or not source:
        raise SystemExit('Set PARKFLOW_VISION_KEY and PARKFLOW_CAMERA_URL')
    if not config.get('spaces') or len({s['id'] for s in config['spaces']}) != len(config['spaces']):
        raise SystemExit('Config requires unique space IDs')
    for space in config['spaces']:
        polygon_array(space['polygon'], 1920, 1080)
    model = YOLO(args.model)
    classes = [int(i) for i, name in model.names.items() if name in {'car','motorcycle','bus','truck'}]
    if not classes:
        raise SystemExit('Model has no vehicle classes')
    stream = LatestFrame(source)
    last_at = 0
    try:
        with httpx.Client(timeout=8, headers={'Authorization': 'Bearer '+key}) as client:
            while True:
                started = time.monotonic()
                latest = stream.latest()
                if latest is None or latest[0] <= last_at or time.time()-latest[0]>3:
                    time.sleep(.2)
                    continue
                captured_at, frame = latest
                last_at = captured_at
                height, width = frame.shape[:2]
                # A frozen or obscured feed can still be wrong; these are simple quality gates.
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                bad_quality = gray.mean()<15 or gray.mean()>245 or gray.std()<8
                results = model.predict(frame, classes=classes, conf=.25, verbose=False)[0]
                boxes = [(*b.xyxy[0].tolist(), float(b.conf[0])) for b in results.boxes]
                observations = []
                for space in config['spaces']:
                    polygon = polygon_array(space['polygon'], width, height)
                    occupied, confidence = classify(polygon, boxes)
                    observations.append({'space_id':space['id'], 'occupied':occupied,
                                         'confidence':0. if bad_quality else confidence})
                try:
                    response = client.post(args.api.rstrip('/')+'/api/vision/occupancy',
                                           json={'camera_id':config['camera_id'], 'observed_at':captured_at, 'spaces':observations})
                    if response.status_code in (403,404,422):
                        raise RuntimeError(f'Configuration rejected by API: HTTP {response.status_code}')
                    response.raise_for_status()
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    # Do not replay old FREE observations on reconnect. Server expires stale data.
                    log.warning('Observation not delivered (%s); will send a fresh frame', type(exc).__name__)
                time.sleep(max(0, 1-(time.monotonic()-started)))
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    main()

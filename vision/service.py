"""Managed camera process. One transient JPEG, no recording, secret config via stdin."""
import json
import os
from pathlib import Path
import sys
import threading
import time

from .worker import LatestFrame


def atomic(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(data)
    # Readers only see complete files. A brief Windows sharing conflict can be retried.
    for attempt in range(4):
        try:
            os.replace(temporary,path)
            return
        except PermissionError:
            if attempt==3:
                raise
            time.sleep(.025)


def run(config, closed):
    import cv2
    from .geometry import classify, polygon_array
    out = Path(config['output'])
    report = {'phase':'CONNECTING','frame_at':0,'ai_state':'OFF'}

    def publish(**values):
        report.update(values)
        atomic(out/'status.json',json.dumps(report).encode())

    publish()
    model = None
    if config['inference']:
        publish(ai_state='LOADING')
        try:
            from ultralytics import YOLO
            model = YOLO(config['model'])
            classes = [int(i) for i,n in model.names.items() if n in {'car','truck','bus','motorcycle'}]
            if not classes:
                raise ValueError('Vehicle classes required')
            publish(ai_state='READY')
        except Exception:
            publish(phase='AI_ERROR',ai_state='ERROR')
            # Keep status readable; do not repeatedly download/reload a broken model.
            closed.wait()
            return
    stream = LatestFrame(config['rtsp_url'])
    last_frame, last_ai = 0, 0
    try:
        while not closed.is_set():
            latest = stream.latest()
            if latest is None or time.time()-latest[0]>3:
                publish(phase='RECONNECTING')
                closed.wait(.5)
                continue
            at, frame = latest
            if at<=last_frame:
                closed.wait(.1)
                continue
            last_frame = at
            height,width = frame.shape[:2]
            preview = cv2.resize(frame,(min(width,1280),max(1,round(height*min(1,1280/width)))))
            ok,jpeg = cv2.imencode('.jpg',preview,[cv2.IMWRITE_JPEG_QUALITY,75])
            if ok:
                atomic(out/'frame.jpg',jpeg.tobytes())
            publish(phase='ONLINE',frame_at=at,width=width,height=height)
            if model and at-last_ai>=1:
                last_ai = at
                try:
                    result = model.predict(frame,classes=classes,conf=.25,verbose=False)[0]
                    boxes = [(*b.xyxy[0].tolist(),float(b.conf[0])) for b in result.boxes]
                    gray = cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
                    bad = gray.mean()<15 or gray.mean()>245 or gray.std()<8
                    spaces = []
                    for space in config['spaces']:
                        occupied,confidence = classify(polygon_array(space['polygon'],width,height),boxes)
                        spaces.append({'space_id':space['id'],'occupied':occupied,'confidence':0 if bad else confidence})
                    atomic(out/'observations.json',json.dumps({'camera_id':1,'observed_at':at,'spaces':spaces}).encode())
                    publish(ai_state='RUNNING')
                except Exception:
                    publish(ai_state='ERROR')
            closed.wait(.25)
    finally:
        stream.close()


def main():
    config = json.loads(sys.stdin.buffer.readline())
    closed = threading.Event()

    def parent_closed():
        sys.stdin.buffer.read()  # EOF when the owning backend exits, even unexpectedly
        closed.set()

    threading.Thread(target=parent_closed,daemon=True).start()
    try:
        run(config,closed)
    except KeyboardInterrupt:
        closed.set()


if __name__ == '__main__':
    main()

"""Mark four corners for each space on a locally supplied camera frame.

python -m vision.calibrate --image frame.jpg --ids 1-A01 1-A02 --output vision/spaces.json
Click 4 ordered corners. N = next/save; R = reset current polygon; Esc = cancel.
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from .geometry import polygon_array


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--ids', nargs='+', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--camera-id', type=int, default=1)
    args = parser.parse_args()
    if len(set(args.ids)) != len(args.ids):
        raise SystemExit('Space IDs must be unique')
    frame = cv2.imread(args.image)
    if frame is None:
        raise SystemExit('Cannot read image')
    factor = min(1, 1200/frame.shape[1], 750/frame.shape[0])
    frame = cv2.resize(frame, None, fx=factor, fy=factor)
    h, w = frame.shape[:2]
    points, spaces = [], []
    window = 'ParkFlow calibration: click 4 corners, N next, R reset, Esc cancel'
    cv2.namedWindow(window)

    def mouse(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN and len(points)<4:
            points.append([x/w, y/h])

    cv2.setMouseCallback(window, mouse)
    try:
        for ident in args.ids:
            points.clear()
            while True:
                display = frame.copy()
                cv2.putText(display, ident, (20,35), cv2.FONT_HERSHEY_SIMPLEX, 1, (50,230,80), 2)
                for s in spaces:
                    p = np.asarray(s['polygon'])*[w,h]
                    cv2.polylines(display, [p.astype('int32')], True, (200,150,0), 2)
                if points:
                    p = (np.asarray(points)*[w,h]).astype('int32')
                    cv2.polylines(display, [p], len(points)==4, (50,230,80), 2)
                cv2.imshow(window, display)
                key = cv2.waitKey(20)&255
                if key == 27 or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE)<1:
                    return
                if key == ord('r'):
                    points.clear()
                if key == ord('n') and len(points)==4:
                    try:
                        polygon_array(points,w,h)
                    except ValueError as exc:
                        print(exc)
                        continue
                    spaces.append({'id':ident,'polygon':[p[:] for p in points]})
                    break
        Path(args.output).write_text(json.dumps({'camera_id':args.camera_id,'spaces':spaces},indent=2),encoding='utf-8')
        print('Saved:', args.output)
    finally:
        cv2.destroyAllWindows()


if __name__ == '__main__':
    main()

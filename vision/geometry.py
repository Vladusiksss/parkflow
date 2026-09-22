"""Geometry separated from YOLO so detection mapping can be tested independently."""
import cv2
import numpy as np


def polygon_array(points, width, height):
    values = np.asarray(points, dtype=np.float32)
    if values.ndim != 2 or values.shape[1] != 2 or len(values) < 3:
        raise ValueError('Polygon needs at least three [x,y] points')
    if not np.isfinite(values).all() or (values < 0).any() or (values > 1).any():
        raise ValueError('Polygon coordinates must be normalized to [0,1]')
    values = values * np.array([width, height], dtype=np.float32)
    if not cv2.isContourConvex(values) or cv2.contourArea(values) < 4:
        raise ValueError('Polygon must be convex, ordered and non-zero in area')
    return values


def classify(polygon, boxes, occupied_overlap=.35, free_overlap=.10):
    """boxes: [(x1,y1,x2,y2,confidence),...]. Score is a heuristic, not calibrated probability."""
    area = cv2.contourArea(polygon)
    best_overlap, best_confidence = 0., 0.
    for x1, y1, x2, y2, confidence in boxes:
        rectangle = np.asarray([[x1,y1],[x2,y1],[x2,y2],[x1,y2]], dtype=np.float32)
        intersection, _ = cv2.intersectConvexConvex(polygon, rectangle)
        overlap = max(0., float(intersection)/area)
        if overlap > best_overlap:
            best_overlap, best_confidence = overlap, float(confidence)
    if best_overlap >= occupied_overlap:
        return True, best_confidence
    if best_overlap > free_overlap:
        return False, .5  # ambiguous overlap -> UNKNOWN
    return False, .75  # absence of a detection is weaker evidence; validate on real footage

import pytest

pytest.importorskip('cv2')
from vision.geometry import classify, polygon_array


def test_overlap_mapping():
    p=polygon_array([[.1,.1],[.5,.1],[.5,.5],[.1,.5]],100,100)
    assert classify(p,[(10,10,50,50,.96)])==(True,.96)
    assert classify(p,[(60,60,90,90,.99)])==(False,.75)
    assert classify(p,[(10,10,20,50,.99)])==(False,.5)


def test_invalid_polygons():
    with pytest.raises(ValueError):
        polygon_array([[0,0],[2,0],[0,1]],100,100)
    with pytest.raises(ValueError):
        polygon_array([[0,0],[1,1],[1,0],[0,1]],100,100)

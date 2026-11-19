"""Pure detection boundary tests; native same-buffer parity is also required."""
from pathlib import Path
import sys
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/worker'))
from pipeline.gva_detection_metadata import detections_from_relation


class OD:
    def __init__(self, id=1, label='person', valid=True, confidence_valid=True):
        self.id,self.label,self.valid,self.confidence_valid = id,label,valid,confidence_valid
    def get_obj_type(self):return self.label
    def get_oriented_location(self):return self.valid,10,20,30,40,.125,.8125
    def get_confidence_lvl(self):return self.confidence_valid,.8125


class Track:
    def __init__(self,id=2,tracking_id=17,valid=True):self.id,self.tracking_id,self.valid=id,tracking_id,valid
    def get_info(self):return self.valid,self.tracking_id,0,0,False


class Relation(list):
    def __init__(self,items,relations=((1,2),)):
        super().__init__(items);self.relations=set(relations)
    def get_relation(self,a,b):return int((a,b)in self.relations)


def extract(items,relations=((1,2),)):
    return detections_from_relation(Relation(items,relations),{1:'car',7:'pedestrian'},OD,Track,0,lambda x:x)


def test_source_box_confidence_class_and_native_tracking_are_preserved():
    d,=extract([OD(),object(),Track()])
    assert d.bbox == [10,20,40,60]
    assert d.confidence == .8125 and d.class_name == 'pedestrian' and d.class_id == 7
    assert d.model_name == 'vehicle'
    assert d.metadata == {'track_id':17,'raw_bbox':[10,20,40,60],
                          'predicted':False,'tracker_backend':'gvatrack'}


def test_tracking_uses_first_related_record_in_metadata_order():
    d,=extract([OD(),Track(3,91),Track(2,17)],((1,2),(1,3)))
    assert d.metadata['track_id'] == 91


def test_unrelated_invalid_tracking_record_is_not_consumed():
    d,=extract([Track(3,99,False),OD(),Track(2,17)])
    assert d.metadata['track_id'] == 17


def test_motion_faces_unmapped_and_non_detection_metadata_are_ignored():
    assert extract([OD(label='motion'), OD(label='face'), OD(label='others'),object()]) == []


@pytest.mark.parametrize('items,relations,error',[
    ([OD()],(), 'track ID'), ([OD(valid=False),Track()],((1,2),), 'location'),
    ([OD(),Track(valid=False)],((1,2),),'tracking info'),
    ([OD(confidence_valid=False),Track()],((1,2),),'confidence')])
def test_invalid_native_subject_fields_fail_explicitly(items,relations,error):
    with pytest.raises(RuntimeError,match=error):extract(items,relations)


def test_detection_order_and_empty_relation_are_preserved():
    result=extract([OD(4,'car'),Track(2,17),OD(1),Track(5,33)],((1,2),(4,5)))
    assert [d.class_name for d in result] == ['car','pedestrian']
    assert [d.metadata['track_id'] for d in result] == [33,17]
    assert detections_from_relation(None,{},OD,Track,0,str) == []

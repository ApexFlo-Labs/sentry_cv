"""Registry-owned cleanup preserves valid recovery histories and gait processing."""
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/worker'))
from pipeline.person_reid import OpenVINOGaitExtractor
from pipeline.v8_events import V8EventPipeline


def setup():
 gait=OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
 gait.buffers=defaultdict(list);gait.buffer_times=defaultdict(list)
 gait.cache={};gait.centers={};gait.minimum_motion_pixels=2
 gait.min_sequence=1000;gait.max_sequence=60
 gait._silhouettes=lambda frame,people,camera:[np.full((64,44),128,np.uint8)for _ in people]
 p=V8EventPipeline.__new__(V8EventPipeline);p.gait=gait;p.camera_id='cam';p.presences={};p.telemetry=None
 return p,gait


def collect(p,presence,track,x):
 state=p.presences.setdefault(presence,{'id':presence,'track_id':track})
 state['track_id']=track;p._remember_gait_center(state)
 person=SimpleNamespace(metadata={'track_id':track},bbox=[x,0,x+30,80])
 p.gait.collect(np.zeros((1,1,3),np.uint8),[person],'cam',{track:presence},captured_at=x)


def test_many_retirements_release_owned_history_without_erasing_live_sequence():
 p,gait=setup();collect(p,'live',1,0);collect(p,'live',1,3)
 retained=gait.buffers['live'][0];times=list(gait.buffer_times['live']);gait.cache['live']=np.ones(4096,np.float32)
 for index in range(2000):
  key=f'old-{index}';track=index+2;collect(p,key,track,0);collect(p,key,track,3)
  gait.cache[key]=np.ones(4096,np.float32);p._retire_gait_state(key);p.presences.pop(key)
 assert set(gait.buffers)==set(gait.buffer_times)==set(gait.cache)=={'live'}
 assert set(gait.centers)==set(p._gait_center_owners)=={('cam',1)}
 assert gait.buffers['live'][0] is retained and gait.buffer_times['live']==times


def test_reassociated_center_history_retires_without_deleting_reused_id():
 p,gait=setup();collect(p,'presence',7,0);collect(p,'presence',7,3)
 collect(p,'presence',8,0);collect(p,'presence',8,3)
 before=gait.buffers['presence'][:];collect(p,'other',7,6)
 assert all(a is b for a,b in zip(gait.buffers['presence'],before))
 p._retire_gait_state('presence')
 assert ('cam',8) not in gait.centers
 assert ('cam',7) in gait.centers and p._gait_center_owners[('cam',7)]=='other'
 p._retire_gait_state()
 assert not any([gait.buffers,gait.buffer_times,gait.cache,gait.centers,p._gait_center_owners])


def test_recovery_keeps_history_until_existing_registry_unknown_timeout():
 p,gait=setup();collect(p,'p',7,0);collect(p,'p',7,3)
 state=p.presences['p'];state.update(kind='person',confirmed=True,last_seen=0,
  missing_evaluated_since=None,missing_evaluations=0,lost=False,episodes={})
 key=('person','7');p.tracks={key:state};p._presence_timeouts=lambda _:(5,18)
 p._lifecycle_enabled=lambda _:True;events=[];p._persist=lambda e:events.append(e)
 p._event=lambda kind,*a,**k:{'event_type':kind,**k}
 p._lost_and_unknown(set(),2,set());p._lost_and_unknown(set(),10,set())
 assert 'p' in gait.buffers and 'p' in p.presences
 p._lost_and_unknown(set(),21,{key});assert 'p' in gait.buffers
 p._lost_and_unknown(set(),21,set())
 assert 'p' not in gait.buffers and 'p' not in p.presences
 assert [e['event_type']for e in events]==['track_lost','presence_ended_unknown']


def test_pending_gait_completes_before_cleanup_without_late_emission():
 p,gait=setup();collect(p,'p',7,0);collect(p,'p',7,3)
 class Future:
  def result(self):
   assert 'p' in gait.buffers
   return [('must-not-emit',)]
 p.session_id='s';p._pending_gait=(Future(),'s',None,p.presences['p'],None,0,'frame')
 p._emit_feature_candidates=lambda *a:(_ for _ in ()).throw(AssertionError('late feature'))
 p._retire_gait_state('p')
 assert p._pending_gait is None and 'p' not in gait.buffers


def configured_pipeline():
 p,gait=setup()
 class Store:
  def persist(self,*args,**kwargs):return True
  def close(self):pass
 binding={'app':'person_identity','use_case_id':'identity','embedding_profile_ids':['gaitbase-v18.1']}
 config={'analytics':{'v8_events':{'v8':{'deployment_id':'test','config_revision':1,'bindings':[binding]}}}}
 p=V8EventPipeline('cam',config,gait=gait,handoff_client=Store())
 return p,gait,config


def test_geometry_only_revision_keeps_gait_but_disabled_profile_releases_it():
 import copy
 p,gait,config=configured_pipeline();collect(p,'p',7,0);collect(p,'p',7,3)
 state=p.presences['p'];state.update(owner=None,episodes={},zone_state={},zone_votes={},line_state={})
 retained=gait.buffers['p'][0]
 updated=copy.deepcopy(config);v8=updated['analytics']['v8_events']['v8'];v8['config_revision']=2
 v8['geometry']={'zones':[{'id':'zone','poly':[[0,0],[1,0],[1,1],[0,1]]}]}
 p.reconfigure(updated)
 assert gait.buffers['p'][0] is retained
 updated=copy.deepcopy(updated);v8=updated['analytics']['v8_events']['v8']
 v8['config_revision']=3;v8['bindings'][0]['embedding_profile_ids']=[]
 p.reconfigure(updated)
 assert not any([gait.buffers,gait.buffer_times,gait.cache,gait.centers,p._gait_center_owners])
 p.close()


def test_new_stream_session_clears_retired_gait_but_outage_alone_does_not():
 p,gait,_=configured_pipeline();collect(p,'p',7,0);collect(p,'p',7,3)
 p.presences['p']['confirmed']=False
 events=[];p._persist=lambda e:events.append(e) or True
 old_session=p.session_id
 p.camera_status('unavailable','source_timeout')
 assert 'p' in gait.buffers and p.session_id==old_session
 p.camera_status('healthy','none')
 assert p.session_id!=old_session and not p.presences
 assert not any([gait.buffers,gait.buffer_times,gait.cache,gait.centers,p._gait_center_owners])
 assert [e['event_type']for e in events]==['camera_health','camera_health']
 p.close()

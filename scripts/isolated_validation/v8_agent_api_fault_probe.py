#!/usr/bin/env python3
"""Actual baked V8 client/scheduler against a schema-checking Unix API fixture.

Synthetic scene vector, real baked JPEG/package code. No model/capacity test,
no real durable storage/Management claim. Never changes retry delays.
"""
from pathlib import Path
import argparse
import hashlib,json,multiprocessing as mp,socketserver,sys,threading,time
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.sentinel_v8_handoff import V8HandoffClient,V8HandoffScheduler,create_handoff_channels,canonical_json_bytes
from pipeline.v8_events import V8EventPipeline
from v8_capture_agent import Store,Handler

ROOT=Path('/results')


def run_blocked_coalescing(root):
 """Actual IPC/Unix HTTP with four waiting workers and a replacement flood."""
 gate=threading.Event();entered=threading.Event();attempts=[]
 class WaitingStore(Store):
  def accept(self,manifest,parts,**kwargs):
   attempts.append({'package_id':manifest['package_id'],
                    'event_type':manifest['observation']['event_type'],
                    'source_frame_id':manifest['observation'].get('source_frame_id')})
   if len(attempts)==1:
    entered.set()
    assert gate.wait(8),'Fixture gate was not released before the production timeout'
   try:
    return super().accept(manifest,parts,**kwargs)
   except Exception as exc:
    with (root/'validation-errors.jsonl').open('a') as log:
     log.write(json.dumps({'event_type':manifest['observation']['event_type'],
                          'error_type':type(exc).__name__,
                          'message':getattr(exc,'message',str(exc))[:3000],
                          'path':list(getattr(exc,'absolute_path',[]))})+'\n')
    raise
 store=WaitingStore(Path('/opt/traffic-pilot/contracts/sentry-v8'),root/'packages.jsonl',
                    root/'agent-status.json',root/'gallery',8)
 class Endpoint(Handler):pass
 Endpoint.store=store
 server=socketserver.UnixStreamServer(str(root/'agent.sock'),Endpoint)
 serving=threading.Thread(target=server.serve_forever,daemon=True);serving.start()
 parents,children=create_handoff_channels(mp.get_context('spawn'),['api-probe'])
 scheduler=V8HandoffScheduler(parents,str(root/'agent.sock'),root/'scheduler.json').start()
 client=V8HandoffClient(children['api-probe'],'api-regression','api-probe')
 vector=np.ones(768,np.float32);vector/=np.linalg.norm(vector)
 binding={'app':'scene_search','use_case_id':'scene','scope':'full_frame','geometry_ids':[]}
 config={'analytics':{'v8_events':{'v8':{'deployment_id':'api-regression','config_revision':1,'bindings':[binding]}}}}
 pipeline=V8EventPipeline('api-probe',config,scene=SimpleNamespace(embed=lambda image:vector),handoff_client=client)
 began=time.monotonic()
 try:
  # Each real camera-health transition is non-replaceable and valid schema7.
  for state,reason in [('healthy','none'),('degraded','stale_frames'),
                       ('unavailable','stream_disconnected'),('healthy','none')]:
   event=pipeline._event('camera_health',time.time(),None,
                        state=state,reason=reason,sampling_gap_seconds=0)
   event.update(as_of=event['observed_at'],last_reliable_at=event['observed_at'])
   assert pipeline._persist(event)
  assert entered.wait(3),'Agent did not receive the first request'
  deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   with scheduler.condition:blocked=scheduler.retained_count==4 and not scheduler.heap
   if blocked:break
   time.sleep(.01)
  else:raise RuntimeError('Four delivery workers did not enter their request waits')
  for index in range(64):
   packet=SimpleNamespace(frame=np.full((90,160,3),index,np.uint8))
   pipeline._scene(packet,binding,None,time.time(),f'api-probe:{index+10}','interval')
  with scheduler.condition:
   during={'retained_packages':scheduler.retained_count,'retained_bytes':scheduler.retained_bytes,
           'heap_entries':len(scheduler.heap),'semantic_index_entries':len(scheduler.pending_by_key),
           'coalesced':scheduler.counters['coalesced']}
   assert during['retained_packages']==5 and during['heap_entries']==1
   assert during['semantic_index_entries']==1 and during['coalesced']==63
  (root/'during-blocked.json').write_text(json.dumps(during,indent=2)+'\n')
  gate.set()
  deadline=time.monotonic()+5
  while time.monotonic()<deadline:
   status=json.loads((root/'scheduler.json').read_text());c=status['counters']
   if c['durable']==5 and status['retained_packages']==0:break
   time.sleep(.01)
  else:raise RuntimeError('Final scene did not reach the agent after gate release')
  assert c['admitted']==68 and c['coalesced']==63
  assert c['exhausted']==c['protocol_errors']==c['backpressure']==0
  assert not scheduler.heap and not scheduler.pending_by_key
  scenes=[x for x in attempts if x['event_type']=='scene_sample']
  assert len(attempts)==5 and len(store.packages)==5
  assert len(scenes)==1 and scenes[0]['source_frame_id']=='api-probe:73'
  return {'case':'blocked-coalescing','passed':True,'during_blocked':during,
          'attempts':attempts,'scheduler':status,'agent_unique_packages':len(store.packages),
          'duration_seconds':time.monotonic()-began}
 finally:
  gate.set();pipeline.close();scheduler.close();server.shutdown();server.server_close()


def run_case(name):
 root=ROOT/name;root.mkdir();attempts=[]
 if name=='blocked-coalescing':return run_blocked_coalescing(root)
 class FaultStore(Store):
  def accept(self,manifest,parts,**kwargs):
   wire_digest=hashlib.sha256(canonical_json_bytes(manifest)+b''.join(parts[k]for k in sorted(parts))).hexdigest()
   n=len(attempts)+1;attempts.append({'attempt':n,'package_id':manifest['package_id'],'digest':wire_digest,'time':time.monotonic()})
   if name=='retry-429-503' and n<=2:return (429 if n==1 else 503),{'error':{'code':'backpressure','message':'controlled fault','retryable':True}}
   if name=='exhaust-503':return 503,{'error':{'code':'unavailable','message':'controlled fault','retryable':True}}
   if name=='reject-400':return 400,{'error':{'code':'invalid_package','message':'controlled fault','retryable':False}}
   if name=='reject-401':return 401,{'error':{'code':'unauthorized','message':'controlled fault','retryable':False}}
   if name=='slow-first-response' and n==1:time.sleep(31)  # Real production timeout remains30s.
   if name=='disconnect-before-store' and n==1:return 599,{}
   status,payload=super().accept(manifest,parts,**kwargs)
   if name=='disconnect-after-store' and n==1:return 599,{}
   if name=='wrong-package-ack':payload['package_id']='00000000-0000-4000-8000-000000000000'
   if name=='missing-evidence-ack':payload['sequences']=payload['sequences'][:1]
   return status,payload
 store=FaultStore(Path('/opt/traffic-pilot/contracts/sentry-v8'),root/'packages.jsonl',root/'agent-status.json',root/'gallery',2)
 class Endpoint(Handler):
  def _send(self,status,payload):
   if status==599:return
   super()._send(status,payload)
 Endpoint.store=store
 socket=str(root/'agent.sock');server=socketserver.UnixStreamServer(socket,Endpoint)
 serving=threading.Thread(target=server.serve_forever,daemon=True);serving.start()
 parents,children=create_handoff_channels(mp.get_context('spawn'),['api-probe'])
 scheduler=V8HandoffScheduler(parents,str(root/'absent.sock') if name=='agent-absent' else socket,root/'scheduler.json').start()
 client=V8HandoffClient(children['api-probe'],'api-regression','api-probe')
 vector=np.ones(768,np.float32);vector/=np.linalg.norm(vector)
 binding={'app':'scene_search','use_case_id':'scene','scope':'full_frame','geometry_ids':[]}
 config={'analytics':{'v8_events':{'v8':{'deployment_id':'api-regression','config_revision':1,'bindings':[binding]}}}}
 pipeline=V8EventPipeline('api-probe',config,scene=SimpleNamespace(embed=lambda image:vector),handoff_client=client)
 began=time.monotonic()
 try:
  packet=SimpleNamespace(frame=np.full((90,160,3),128,np.uint8))
  pipeline._scene(packet,binding,None,time.time(),'api-probe:1','interval')
  deadline=time.monotonic()+(80 if name=='slow-first-response' else 14)
  while time.monotonic()<deadline:
   status=json.loads((root/'scheduler.json').read_text());c=status['counters']
   if c['durable']+c['duplicate']+c['exhausted']>=1:break
   time.sleep(.02)
  else:raise RuntimeError('bounded API completion deadline')
  success=name not in {'exhaust-503','reject-400','reject-401','agent-absent','wrong-package-ack','missing-evidence-ack'}
  assert c['admitted']==1 and status['retained_packages']==0 and status['retained_bytes']==0
  assert c['durable']+c['duplicate']==int(success) and c['exhausted']==int(not success)
  assert len({x['digest']for x in attempts})==len({x['package_id']for x in attempts})==int(name!='agent-absent')
  expected={'retry-429-503':3,'exhaust-503':6,'disconnect-before-store':2,'disconnect-after-store':2,'slow-first-response':2,'agent-absent':0}.get(name,1)
  assert len(attempts)==expected,(name,len(attempts),expected)
  if name in {'disconnect-after-store','slow-first-response'}:assert c['duplicate']==1 and len(store.packages)==1
  if name in {'reject-400','reject-401'}:assert c['protocol_errors']==1
  return {'case':name,'passed':True,'attempts':attempts,'scheduler':status,'agent_unique_packages':len(store.packages),'duration_seconds':time.monotonic()-began}
 finally:
  pipeline.close();scheduler.close();server.shutdown();server.server_close()


def main():
 defaults=['normal-durable','retry-429-503','disconnect-before-store','disconnect-after-store','reject-400','wrong-package-ack','missing-evidence-ack','exhaust-503']
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--cases',nargs='+',choices=defaults+['reject-401','agent-absent','slow-first-response','blocked-coalescing'],default=defaults);args=parser.parse_args()
 cases=args.cases;results=[]
 report={'scope':__doc__,'results':results,'passed':False}
 for name in cases:
  try:result=run_case(name)
  except Exception as e:result={'case':name,'passed':False,'error':type(e).__name__+':'+str(e)}
  results.append(result);(ROOT/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps({'case':name,'passed':result['passed'],'error':result.get('error')}),flush=True)
 report['passed']=all(x['passed']for x in results);(ROOT/'report.json').write_text(json.dumps(report,indent=2));return 0 if report['passed']else 3


if __name__=='__main__':raise SystemExit(main())

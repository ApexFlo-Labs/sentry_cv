#!/usr/bin/env python3
"""Bounded image recovery tests; live source, temporary TLS relay, V8 API capture.

This checks native execution, source isolation and package integrity at /v1/packages.
It does not test recognition accuracy, remote Management ingest or maximum capacity.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen
from urllib.parse import urlsplit

def recovery_camera_healthy(camera):
    age=camera.get('decoder',{}).get('last_frame_age_seconds')
    return (camera.get('decoder',{}).get('state')=='healthy'
            and isinstance(age,(int,float)) and not isinstance(age,bool)
            and 0<=age<4)


ROOT=Path(__file__).resolve().parents[2]
UPSTREAM='rtsps://gpu-box.burmilla-bass.ts.net:443/traffic1'

def verify_relay_upstream_tls(address, context):
    """Fail a misconfigured TEST relay before starting a CV container.

    This verifies the relay's upstream connection, not the image's approved
    legacy-compatible camera policy. A controlled self-signed fixture needs its
    public test CA supplied explicitly to this relay.
    """
    if address.scheme != 'rtsps':
        return {'tls': False, 'verified': None}
    with socket.create_connection((address.hostname,address.port or 322),timeout=8) as raw:
        with context.wrap_socket(raw,server_hostname=address.hostname):
            return {'tls': True, 'verified': True}

class Relay:
    def __init__(self,temporary,upstream=UPSTREAM,ca_file=None):
        self.address=urlsplit(upstream)
        if self.address.scheme not in {'rtsp','rtsps'} or not self.address.hostname:
            raise ValueError('Recovery upstream must be RTSP/RTSPS with a host')
        self.upstream_context=ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        self.upstream_preflight=verify_relay_upstream_tls(self.address,self.upstream_context)
        cert=Path(temporary)/'server.crt';key=Path(temporary)/'server.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
        self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.context.load_cert_chain(cert,key)
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen(16);self.listener.settimeout(.2)
        suffix=self.address.path or '/'
        if self.address.query:suffix+='?'+self.address.query
        self.url=f'rtsps://127.0.0.1:{self.listener.getsockname()[1]}{suffix}'
        self.stop=threading.Event();self.block=threading.Event();self.connections=[];self.lock=threading.Lock();self.accepted=0
        threading.Thread(target=self.accept,daemon=True).start()
    def accept(self):
        while not self.stop.is_set():
            try:s,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:return
            if self.block.is_set():s.close();continue
            threading.Thread(target=self.forward,args=(s,),daemon=True).start()
    def forward(self,plain):
        pair=[]
        try:
            plain.settimeout(8)
            client=self.context.wrap_socket(plain,server_side=True);pair.append(client)
            raw=socket.create_connection((self.address.hostname,self.address.port or
                (322 if self.address.scheme=='rtsps' else 554)),timeout=8)
            upstream=(self.upstream_context.wrap_socket(raw,server_hostname=self.address.hostname)
                      if self.address.scheme=='rtsps' else raw);pair.append(upstream)
            with self.lock:self.connections.extend(pair);self.accepted+=1
            for s in pair:s.settimeout(3)
            while not self.stop.is_set() and not self.block.is_set():
                ready=[s for s in pair if s.pending()]
                if not ready:ready,_,_=select.select(pair,[],[],.2)
                for s in ready:
                    data=s.recv(65536)
                    if not data:return
                    (upstream if s is client else client).sendall(data)
        except (OSError,ssl.SSLError):pass
        finally:
            with self.lock:
                for s in pair:
                    if s in self.connections:self.connections.remove(s)
                    try:s.close()
                    except OSError:pass
    def interrupt(self):
        self.block.set()
        with self.lock:
            for s in self.connections:
                try:s.shutdown(socket.SHUT_RDWR)
                except OSError:pass
    def close(self):
        self.stop.set();self.interrupt();self.listener.close()


def gil_blocking_fault_script(script):
    """Hold the worker GIL in libc, without changing an accelerator or kernel."""
    needle = 'while True:time.sleep(1)'
    if script.count(needle) != 1:
        raise ValueError('native GIL fault requires exactly one known injection point')
    return script.replace(needle, 'import ctypes;ctypes.PyDLL(None).sleep(600)').replace(
        'test_injected_native_stall', 'test_injected_native_gil_stall')


def gil_stall_recovery_verified(events):
    injected = [e for e in events if e.get('event') == 'test_injected_native_gil_stall']
    if len(injected) != 1 or type(injected[0].get('pid')) is not int or injected[0]['pid'] <= 0:
        return False
    blocked = injected[0]['pid']
    return bool(any(e.get('event') == 'native_worker_watchdog_restart'
                    and e.get('camera_process_pid') == blocked
                    and type(e.get('pid')) is int and e['pid'] > 0 and e['pid'] != blocked for e in events)
                and any(e.get('event') == 'camera_process_exited'
                        and e.get('camera_process_pid') == blocked
                        and e.get('exit_code') == -9 for e in events))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--image',required=True);ap.add_argument('--engine',choices=['podman','docker'],default='podman');ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--mode',choices=['reconnect','native-hang','prefetch-missing-pts','bad-first-source'],default='reconnect');ap.add_argument('--port',type=int,default=18381)
    ap.add_argument('--upstream-url',default=UPSTREAM)
    ap.add_argument('--upstream-ca-file',type=Path,
                    help='Optional trust for the TEST relay only; never mounted in the CV image')
    ap.add_argument('--fps',type=int,default=4)
    ap.add_argument('--profile', choices=['S','SVX','SA'], default='S',
                    help='Recovery workload; scene is enabled on both cameras')
    ap.add_argument('--native-hang-gil', action='store_true',
                    help='Explicit native-hang variant: hold worker GIL in libc and require external parent SIGKILL/recovery')
    args=ap.parse_args()
    if args.native_hang_gil and args.mode!='native-hang':ap.error('GIL fault requires native-hang mode')
    if args.mode=='prefetch-missing-pts' and args.profile!='SA':ap.error('prefetch fault requires SA subject workload')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);out.chmod(0o755)
    config=out/'config';secrets=out/'secrets';delivery=Path(tempfile.mkdtemp(prefix='sv8-rec-'));fault=out/'fault'
    for p in (config,secrets,delivery,fault):p.mkdir(exist_ok=True);p.chmod(0o755)
    delivery.chmod(0o777)
    spec=importlib.util.spec_from_file_location('capacity_helper',ROOT/'scripts/v8_capacity_probe.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    identity=json.loads(subprocess.check_output([args.engine,'image','inspect',args.image],text=True))[0]
    image_id=identity['Id']
    report={'image':args.image,'image_config_id':image_id,'profile':args.profile,'mode':args.mode,'native_hang_gil':args.native_hang_gil,'scope':__doc__,'passed':False,'phases':[]}
    snapshot=out/'harness-snapshot';snapshot.mkdir(exist_ok=True)
    report['harness_sha256']={}
    for source in [Path(__file__),ROOT/'scripts/v8_capacity_probe.py',
                   Path(__file__).with_name('v8_capture_agent.py')]:
        raw=source.read_bytes();(snapshot/source.name).write_bytes(raw)
        report['harness_sha256'][source.name]=hashlib.sha256(raw).hexdigest()
    container_id=None;unavailable_socket=None
    samples=[];agent=None;name=f'sentry-recovery-{args.mode}-{os.getpid()}'
    log_handle=(out/'agent.log').open('w')
    def health():
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/healthz',timeout=5) as r:h=json.load(r)
            row={'monotonic':time.monotonic(),'wall_time':time.time(),'health':h};samples.append(row)
            with (out/'health.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
            return h
        except Exception:return {}
    def cameras(h):return {c['camera_id']:c for c in h.get('telemetry',{}).get('cameras',[])}
    def decoded(c):return c.get('processing',{}).get('totals',{}).get('decoded_frames',0)
    healthy=recovery_camera_healthy
    def verified_entries():
        r=subprocess.run([args.engine,'logs',name],capture_output=True,text=True,timeout=10)
        entries=[]
        for line in (r.stdout+r.stderr).splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('event')=='camera_recovery_verified' and event.get('camera_id')=='capacity-01':entries.append(event)
        return entries
    def verified_recoveries():return len(verified_entries())
    def wait_for(predicate,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            h=health();cs=cameras(h)
            if predicate(h,cs):return h,cs
            time.sleep(2)
        raise RuntimeError('bounded recovery condition not satisfied')
    try:
        with tempfile.TemporaryDirectory(prefix='sentry-recovery-certs-') as tmp:
            report['failure_stage']='test_relay_upstream_preflight'
            relay=Relay(tmp,args.upstream_url,args.upstream_ca_file)
            report['test_relay_upstream_preflight']=relay.upstream_preflight
            report['failure_stage']='camera_startup_or_recovery_assertion'
            try:
                state=helper.desired_state(2,args.fps,full_usecases=True)
                helper.apply_profiles(state,[args.profile]*2)
                helper.validation_phase('startup', native_clock_deadlines_active=True)
                (config/'desired_state.json').write_text(json.dumps(state))
                first_url=relay.url
                if args.mode=='bad-first-source':
                    # Reserve a local port without listening: deterministic
                    # connection refusal, never an unrelated external outage.
                    unavailable_socket=socket.socket()
                    unavailable_socket.bind(('127.0.0.1',0))
                    first_url=f'rtsp://127.0.0.1:{unavailable_socket.getsockname()[1]}/unavailable'
                for cam,url in zip(state['cameras'],[first_url,args.upstream_url]):
                    (secrets/(cam['camera_id']+'.url')).write_text(url+'\n')
                agent=subprocess.Popen([sys.executable,str(Path(__file__).with_name('v8_capture_agent.py')),'--socket',str(delivery/'agent.sock'),'--schemas',str(ROOT/'newdetails/sentry-v8'),'--log',str(out/'packages.jsonl'),'--status',str(out/'agent-status.json')],stdout=log_handle,stderr=subprocess.STDOUT)
                deadline=time.monotonic()+10
                while not (delivery/'agent.sock').exists() and time.monotonic()<deadline:time.sleep(.1)
                if not (delivery/'agent.sock').exists():raise RuntimeError('agent capture startup failed')
                cmd=[args.engine,'run','-d','--name',name,'--network=host','--device','/dev/dri','-e',f'APEX_API_PORT={args.port}','-e','SENTINEL_DLS_PROGRESS_INTERVAL_SECONDS=5','-v',f'{config}/desired_state.json:/configs/desired_state.json:ro','-v',f'{secrets}:/run/secrets/sentinel/cameras:ro','-v',f'{delivery}:/run/sentinel-delivery']
                # Bad-first isolation is an ordinary deployment, not a timed
                # recovery injection. Use the baked first-frame deadline there.
                # Other modes retain their explicitly shortened test deadline.
                if args.mode!='bad-first-source':
                    cmd+=['-e','SENTINEL_SURFACE_FIRST_FRAME_TIMEOUT_SECONDS=15']
                if Path('/dev/accel/accel0').exists():cmd+=['--device','/dev/accel/accel0']
                if args.engine=='podman':cmd+=['--group-add','keep-groups']
                else:
                    groups={p.stat().st_gid for p in [*Path('/dev/dri').glob('renderD*'),Path('/dev/accel/accel0')] if p.exists()}
                    for group in sorted(groups):cmd+=['--group-add',str(group)]
                run_id=os.environ.get('SENTINEL_VALIDATION_RUN_ID');case_id=os.environ.get('SENTINEL_VALIDATION_CASE')
                if bool(run_id)!=bool(case_id):raise ValueError('both ownership labels required')
                if run_id:cmd+=['--label','sentinel.validation_run='+run_id,'--label','sentinel.validation_case='+case_id]
                if args.mode=='native-hang':
                    (fault/'sitecustomize.py').write_text('''import sys,os,time\nsys.path.insert(0,"/opt/traffic-pilot/services/worker")\nfrom pipeline.multi_camera_gst import MultiCameraGstPipeline\noriginal=MultiCameraGstPipeline.create_branch\ndef create(self,camera_id,description,configure=None):\n    def setup(branch):\n        if configure:configure(branch)\n        if camera_id!="capacity-01":return\n        from gi.repository import Gst\n        count=[0]\n        def stall(pad,info):\n            count[0]+=1\n            if count[0]==40:\n                try:fd=os.open("/tmp/sentry-native-fault-once",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)\n                except FileExistsError:return Gst.PadProbeReturn.OK\n                import logging\n                logging.getLogger("test_fault").error("test injected blocked primary callback",extra={"event":"test_injected_native_stall","camera_id":camera_id})\n                while True:time.sleep(1)\n            return Gst.PadProbeReturn.OK\n        branch.get_by_name("primarydet").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,stall)\n    return original(self,camera_id,description,configure=setup)\nMultiCameraGstPipeline.create_branch=create\n''')
                    fault_script=fault/'sitecustomize.py'
                    fault_script.write_text(fault_script.read_text().replace(
                        'count[0]==40',f'count[0]=={max(40,args.fps*10)}'))
                if args.native_hang_gil:
                    hook=fault/'sitecustomize.py'
                    hook.write_text(gil_blocking_fault_script(hook.read_text()))
                if args.mode=='prefetch-missing-pts':
                    (fault/'sitecustomize.py').write_text('''import sys,os,logging\nfrom types import SimpleNamespace\nsys.path.insert(0,"/opt/traffic-pilot/services/worker")\nfrom pipeline import surface_stream\noriginal=surface_stream._record_consumed_surface\ncount=[0]\ndef consumed(sample,trace=None):\n    caller=sys._getframe(1)\n    if caller.f_code.co_name=="run_surface_camera" and caller.f_locals.get("name")=="capacity-01":\n        count[0]+=1\n        if count[0]>=40:\n            try:fd=os.open("/tmp/sentry-prefetch-timestamp-fault-once",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)\n            except FileExistsError:return original(sample,trace)\n            logging.getLogger("test_fault").error("test injected missing PTS at retained-surface consumption",extra={"event":"test_injected_prefetch_missing_pts","camera_id":"capacity-01","stream_session_id":caller.f_locals.get("stream_session_id")})\n            invalid=SimpleNamespace(get_buffer=lambda:SimpleNamespace(pts=(1<<64)-1))\n            return original(invalid,trace)\n    return original(sample,trace)\nsurface_stream._record_consumed_surface=consumed\n''')
                if args.mode in ['native-hang','prefetch-missing-pts']:
                    inspect=json.loads(subprocess.check_output([args.engine,'image','inspect',args.image],text=True))[0]
                    existing_path=next((e.split('=',1)[1] for e in inspect['Config'].get('Env',[]) if e.startswith('PYTHONPATH=')),'/opt/traffic-pilot')
                    cmd+=['-e','PYTHONPATH=/fault:'+existing_path,'-v',f'{fault}:/fault:ro']
                cmd.append(image_id);(out/'command.json').write_text(json.dumps(cmd,indent=2))
                result=subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=40)
                container_id=result.stdout.strip()
                if not re.fullmatch('[0-9a-f]{12,64}',container_id):raise RuntimeError('invalid owned container ID')
                if args.mode=='bad-first-source':
                    print('image started; first source unavailable, waiting for healthy peer',flush=True)
                    h,cs=wait_for(lambda h,cs:len(cs)==2 and healthy(cs['capacity-02'])
                        and decoded(cs['capacity-02'])>=5 and decoded(cs['capacity-01'])==0
                        and h.get('ready'),150)
                else:
                    print('image started; waiting for two active cameras',flush=True)
                    h,cs=wait_for(lambda h,cs:len(cs)==2 and all(healthy(c) and decoded(c)>=5 for c in cs.values()) and h.get('ready'),150)
                initial_pid=cs['capacity-01'].get('pid');report['initial_worker_pid']=initial_pid
                helper.validation_phase('steady')
                report['phases'].append({'phase':'baseline','counts':{k:decoded(v) for k,v in cs.items()}})
                if args.mode=='bad-first-source':
                    peer_samples=[]
                    for _ in range(7):
                        h=health();cs=cameras(h)
                        assert not healthy(cs.get('capacity-01',{})) and decoded(cs.get('capacity-01',{}))==0
                        peer_samples.append({'ready':h.get('ready'),
                            'sample_monotonic_seconds':time.monotonic(),
                            'cameras':helper.camera_runtime_snapshot(h)})
                        time.sleep(5)
                    report['bad_first_peer_samples']=peer_samples
                    report['bad_first_peer_progress']=helper.isolation_peers_progress(
                        peer_samples,'capacity-01',{'capacity-01':args.fps,'capacity-02':args.fps})
                    assert report['bad_first_peer_progress'],'Unavailable first camera blocked peer readiness/progress'
                elif args.mode=='reconnect':
                    for round_no in (1,2):
                        before={k:decoded(v) for k,v in cs.items()};pid=cs['capacity-02'].get('pid');verified_before=verified_recoveries()
                        relay.interrupt();print(f'round {round_no}: relay offline for 18 seconds',flush=True)
                        end=time.monotonic()+18;peer_states=[]
                        while time.monotonic()<end:
                            h=health();cs=cameras(h);peer=cs.get('capacity-02',{})
                            peer_states.append({'healthy':healthy(peer),'pid':peer.get('pid'),'decoded':decoded(peer),'ready':h.get('ready')})
                            time.sleep(2)
                        relay.block.clear();print(f'round {round_no}: relay restored; waiting for fresh frames',flush=True)
                        h,cs=wait_for(lambda h,cs:len(cs)==2 and healthy(cs['capacity-01']) and decoded(cs['capacity-01'])>before['capacity-01']+5 and verified_recoveries()>verified_before,90)
                        entries=verified_entries()
                        session=cs['capacity-01'].get('decoder',{}).get('stream_session_id')
                        assert entries[-1].get('stream_session_id')==session,'health/log stream session mismatch'
                        result={'round':round_no,'recovered_stream_session_id':session,'verified_recovery_log_count':verified_recoveries(),'peer_kept_processing':bool(peer_states and all(s['healthy'] and s['ready'] and s['pid']==pid for s in peer_states) and peer_states[-1]['decoded']>=before['capacity-02']+30), 'worker_pid_preserved':cs['capacity-01'].get('pid')==initial_pid,'fresh_counts':{k:decoded(v) for k,v in cs.items()},'peer_samples':peer_states}
                        report['phases'].append(result)
                        assert result['peer_kept_processing'] and result['worker_pid_preserved'], 'source outage disrupted healthy peer/group'
                        time.sleep(5)
                else:
                    print('waiting for one-shot injected fault and supervised replacement',flush=True)
                    h,cs=wait_for(lambda h,cs:len(cs)==2 and all(healthy(c) and decoded(c)>=8 for c in cs.values()) and cs['capacity-01'].get('pid')!=initial_pid and h.get('ready'),150)
                    report['replacement_worker_pid']=cs['capacity-01'].get('pid')
                    report['phases'].append({'phase':'replacement_recovered','counts':{k:decoded(v) for k,v in cs.items()}})
                    time.sleep(8);h=health()
                report['final_health']=h
                packages=[]
                for line in (out/'packages.jsonl').read_text().splitlines():
                    try:packages.append(json.loads(line))
                    except ValueError:pass
                session_by_camera={k:v.get('decoder',{}).get('stream_session_id') for k,v in cameras(h).items()}
                required_cameras=({'capacity-02'} if args.mode=='bad-first-source' else {'capacity-01','capacity-02'})
                assert required_cameras.issubset(session_by_camera),'required final camera telemetry absent'
                assert all(isinstance(session_by_camera[k],str) and session_by_camera[k] for k in required_cameras),'required final stream session absent'
                report['package_session_matches']={k:any(p.get('camera_id')==k and p.get('stream_session_id')==sid for p in packages) for k,sid in session_by_camera.items() if k in required_cameras}
                assert all(report['package_session_matches'].values()),'package session does not match current health'
                report['relay_tls_connections']=relay.accepted
                report['agent']=json.loads((out/'agent-status.json').read_text())
                report['source_utc_sanity']=helper.source_utc_sanity(packages)
                assert report['source_utc_sanity']['passed'],'invalid/future output timestamp after recovery'
                assert report['agent'].get('evidence_bytes',0)>0 and report['agent'].get('embeddings',0)>0
                actual_cameras=set(report['agent'].get('cameras',{}))
                if args.mode=='bad-first-source':
                    assert required_cameras.issubset(actual_cameras) and actual_cameras<={'capacity-01','capacity-02'}
                else:
                    assert actual_cameras==required_cameras
                report['passed']=True
            finally:relay.close()
    except Exception as e:
        report['error']=str(e);print('FAILED:',type(e).__name__,str(e),flush=True)
    finally:
        logs=subprocess.run([args.engine,'logs',container_id],capture_output=True,text=True,timeout=20) if container_id else subprocess.CompletedProcess([],0,'','')
        text=logs.stdout+logs.stderr;(out/'container.log').write_text(text)
        events=[]
        for line in text.splitlines():
            try:d=json.loads(line)
            except ValueError:continue
            if d.get('event') in ['camera_pipeline_stalled','camera_source_restart_begin','camera_source_restart_requested','camera_recovery_verified','native_graph_rebuild_requested','native_graph_watchdog_fenced','native_worker_watchdog_restart','camera_process_restarted','rtsp_video_pad_linked','camera_process_exited','test_injected_native_gil_stall']:events.append(d)
        report['recovery_events']=events
        if args.native_hang_gil:
            report['gil_blocking_parent_recovery_verified']=gil_stall_recovery_verified(events)
            if report['passed'] and not report['gil_blocking_parent_recovery_verified']:
                report['passed']=False;report['error']='missing actual GIL fault/external parent SIGKILL/recovery proof'
        if report['passed'] and args.mode=='prefetch-missing-pts':
            rejected=[e for e in events if e.get('event')=='camera_pipeline_stalled'
                      and 'surface timestamp missing' in json.dumps(e)]
            traces=[e.get('native_progress',{}).get('stages',{}).get('camera.consumed',{}) for e in rejected]
            report['invalid_pts_rejected_before_consumption_trace']=bool(rejected
                and all(e.get('camera_id')=='capacity-01' for e in rejected)
                and all(isinstance(t.get('pts_ns'),int) and 0<=t['pts_ns']<(1<<64)-1
                        and t.get('count',0)>0 for t in traces))
            if ('test_injected_prefetch_missing_pts' not in text
                    or not report['invalid_pts_rejected_before_consumption_trace']):
                report['passed']=False;report['error']='missing injected-PTS rejection/trace evidence'
        if report['passed'] and args.mode!='bad-first-source':
            expected='camera_recovery_verified' if args.mode=='reconnect' else 'native_worker_watchdog_restart'
            if not any(d['event']==expected for d in events):report['passed']=False;report['error']='required recovery log missing'
        if container_id:subprocess.run([args.engine,'rm','-f',container_id],capture_output=True,timeout=20)
        if unavailable_socket is not None:unavailable_socket.close()
        if agent is not None:
            agent.terminate()
            try:agent.wait(timeout=5)
            except subprocess.TimeoutExpired:agent.kill();agent.wait()
        log_handle.close()
        shutil.rmtree(delivery,ignore_errors=True)
        (out/'report.json').write_text(json.dumps(report,indent=2))
        status_file=out/'agent-status.json'
        if os.geteuid()==0 and status_file.exists():
            # The root-run fixture atomically creates this file0600. Keep its
            # privacy while allowing the owning workspace user's evidence copy.
            owner=out.parent.stat().st_uid
            os.chown(status_file,owner,owner)
        print(json.dumps({'passed':report['passed'],'error':report.get('error'),'report':str(out/'report.json'),'recovery_events':len(events)},indent=2),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())

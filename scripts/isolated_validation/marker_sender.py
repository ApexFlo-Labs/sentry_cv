#!/usr/bin/env python3
"""Independently paced, one-epoch TCP source for visible frame-marker tests."""
import argparse,json,socket,subprocess,time,os,signal,ipaddress
from fractions import Fraction
from pathlib import Path
from mpegts_pacing import FramePacer,frame_groups
from reconnecting_replay import stop_child

def main():
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--fixture',required=True);p.add_argument('--logdir',required=True)
 p.add_argument('--bind',default='127.0.0.1',help='Test-source interface; default remains loopback')
 p.add_argument('--peer',help='When serving another test host, permit only this source IP')
 a=p.parse_args();ipaddress.ip_address(a.bind)
 if a.peer:ipaddress.ip_address(a.peer)
 info=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=avg_frame_rate,nb_frames','-of','json',a.fixture]))['streams'][0]
 source_fps=float(Fraction(info['avg_frame_rate']));source_frames=int(info['nb_frames'])
 if not 0<source_fps<=120 or source_frames<=0:raise ValueError('explicit marker fixture FPS/frame count required')
 def stop(*unused):raise SystemExit(0)
 signal.signal(signal.SIGTERM,stop)
 root=Path(a.logdir);root.mkdir(parents=True,exist_ok=True)
 producer=None;first_pts=None
 with socket.socket() as server:
  server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);server.bind((a.bind,a.port));server.listen(1)
  while True:
   connection,peer=server.accept()
   if not a.peer or peer[0]==a.peer:break
   connection.close()
  epoch={'port':a.port,'accepted_mono':time.monotonic(),'accepted_utc':time.time(),'fixture':a.fixture,'source_fps':source_fps,'source_frames':source_frames,'pacing':'DTS intervals anchored to actual send time; no catchup'}
  try:
   with connection,(root/(str(a.port)+'-pes.jsonl')).open('w',buffering=1) as log:
    producer=subprocess.Popen(['ffmpeg','-v','error','-stream_loop','-1','-i',a.fixture,'-map','0:v:0','-an','-c:v','copy','-f','mpegts','pipe:1'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    epoch['producer_started_mono']=time.monotonic();(root/(str(a.port)+'.json')).write_text(json.dumps(epoch))
    pacer=FramePacer()
    for (pts,dts),data in frame_groups(producer.stdout):
     pacer.pace(dts);before=time.monotonic()
     connection.sendall(data);sent=time.monotonic()
     if first_pts is None:
      first_pts=pts;epoch['first_bytes_mono']=before;epoch['first_bytes_utc']=time.time();(root/(str(a.port)+'.json')).write_text(json.dumps(epoch))
     relative=(pts-first_pts+(1<<32))%(1<<33)-(1<<32)
     index=round(relative*source_fps/90000)
     log.write(json.dumps({'source_index':index,'marker_index':index%source_frames,'pts90k':pts,'dts90k':dts,'transmitted_mono':sent,'transmitted_utc':time.time(),'first_bytes_submitted_mono':before,'transport_bytes':len(data),'port':a.port})+'\n')
  except (BrokenPipeError,ConnectionResetError):pass
  finally:stop_child(producer)

if __name__=='__main__':main()

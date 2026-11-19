#!/usr/bin/env python3
"""Stream an OpenCL trace; outstanding calls are diagnostics, not kernel timing."""
import argparse,gzip,hashlib,json,re,time
from pathlib import Path

PATTERN=re.compile(rb'^(>>>>|<<<<) Time: (\d+) TID = (\d+) ([A-Za-z_][A-Za-z0-9_]*)')


def analyze(source,output,gzip_copy=None,rate=24.):
 if output.exists()or(gzip_copy is not None and gzip_copy.exists()):raise ValueError('fresh output required')
 stats={};active={};max_time=0;longest=[];digest=hashlib.sha256();lines=0;unmatched=0;seen=0;carry=b'';started=time.monotonic()
 compressed=gzip.GzipFile(filename=str(gzip_copy),mode='wb',compresslevel=1,mtime=0)if gzip_copy else None
 def consume(line):
  nonlocal max_time,lines,unmatched
  lines+=1;match=PATTERN.match(line)
  if not match:return
  direction,stamp,tid,name=match.groups();stamp=int(stamp);tid=int(tid);name=name.decode();max_time=max(max_time,stamp)
  row=stats.setdefault(name,{'entries':0,'exits':0,'matched':0,'sum_wall_us':0,'max_wall_us':0})
  stack=active.setdefault(tid,[])
  if direction==b'>>>>':
   row['entries']+=1;stack.append({'api':name,'started_us':stamp,'entry':line[:1024].decode(errors='replace')})
   if len(stack)>256:raise RuntimeError('diagnostic call nesting exceeds bound')
  else:
   row['exits']+=1
   index=next((i for i in range(len(stack)-1,-1,-1)if stack[i]['api']==name),None)
   if index is None:unmatched+=1;return
   entry=stack.pop(index);duration=stamp-entry['started_us'];row['matched']+=1;row['sum_wall_us']+=duration;row['max_wall_us']=max(row['max_wall_us'],duration)
   if len(longest)<20 or duration>longest[-1]['wall_us']:
    longest.append({'tid':tid,'wall_us':duration,**entry,'exit':line[:512].decode(errors='replace')});longest.sort(key=lambda x:-x['wall_us']);del longest[20:]
 try:
  with source.open('rb')as file:
   while True:
    chunk=file.read(1024*1024)
    if not chunk:break
    digest.update(chunk);seen+=len(chunk)
    if compressed:compressed.write(chunk)
    parts=(carry+chunk).split(b'\n');carry=parts.pop()
    for line in parts:consume(line)
    if rate:
     delay=seen/(rate*1024*1024)-(time.monotonic()-started)
     if delay>0:time.sleep(delay)
  if carry:consume(carry)
 finally:
  if compressed:compressed.close()
 pending=[{'tid':tid,'age_at_last_api_log_us':max_time-entry['started_us'],**entry}for tid,stack in active.items()for entry in stack]
 result={'source':str(source),'source_sha256':digest.hexdigest(),'bytes_read':seen,'lines_read':lines,'last_log_elapsed_us':max_time,'api_counts_and_call_wall_time':stats,'longest_completed_calls':longest,'outstanding_at_file_end':sorted(pending,key=lambda x:-x['age_at_last_api_log_us']),'exit_without_entry':unmatched,'gzip_lossless_copy':str(gzip_copy)if gzip_copy else None,'scope':'Host API call entry/exit wall time only; not kernel execution or camera latency. Outstanding at EOF may be normal shutdown/thermal termination. Correlate independent native-stall capture before inferring a hang.'}
 output.write_text(json.dumps(result,indent=2));return result


if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--gzip-copy',type=Path);p.add_argument('--max-mib-per-second',type=float,default=24);a=p.parse_args();r=analyze(a.source,a.output,a.gzip_copy,a.max_mib_per_second);print(json.dumps({'source':str(a.source),'bytes':r['bytes_read'],'outstanding':len(r['outstanding_at_file_end']),'output':str(a.output)}))

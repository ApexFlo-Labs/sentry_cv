#!/usr/bin/env python3
"""Attach host GDB to one explicitly owned fleet child before first inference.

Diagnostic run only: debugger attachment can perturb startup/scheduling and its
run is never capacity evidence. No global core/ptrace setting is changed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def proc(pid):
    try:
        root=Path('/proc')/str(pid)
        command=root.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode()
        status=root.joinpath('status').read_text()
        ppid=int(next(v.split()[1] for v in status.splitlines() if v.startswith('PPid:')))
        return command,ppid
    except (OSError,StopIteration,ValueError):
        return None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',required=True)
    p.add_argument('--case',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=240)
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    child=None
    stop=[False]
    def interrupt(*unused):
        stop[0]=True
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
    signal.signal(signal.SIGTERM,interrupt)
    signal.signal(signal.SIGINT,interrupt)
    deadline=time.monotonic()+a.seconds
    target=None
    record=None
    while time.monotonic()<deadline and not stop[0]:
        ids=subprocess.check_output(['docker','ps','-q','--filter',
            'label=sentinel.validation_run='+a.run_id,'--filter',
            'label=sentinel.validation_case='+a.case],text=True).split()
        if ids:
            assert len(ids)==1,'Ambiguous owned CV container'
            info=json.loads(subprocess.check_output(['docker','inspect',ids[0]],text=True))[0]
            init=info['State']['Pid']
            children=[]
            for entry in Path('/proc').iterdir():
                if entry.name.isdigit():
                    item=proc(int(entry.name))
                    if item:children.append((int(entry.name),*item))
            fleet={pid for pid,cmd,ppid in children if ppid==init and 'stream_fleet_openvino' in cmd}
            candidates=[(pid,cmd,ppid) for pid,cmd,ppid in children
                        if ppid in fleet and 'multiprocessing.spawn' in cmd]
            if candidates:
                assert len(candidates)==1,'Ambiguous native group children'
                target=candidates[0][0]
                record={'utc':datetime.now(timezone.utc).isoformat(),'container_id':ids[0],
                    'image_config_id':info['Image'],'container_init_host_pid':init,
                    'worker_host_pid':target,'worker_command':candidates[0][1],
                    'worker_parent_host_pid':candidates[0][2],
                    'process_starttime':Path('/proc',str(target),'stat').read_text().rsplit(')',1)[1].split()[19],
                    'scope':__doc__}
                break
        time.sleep(.1)
    assert target is not None,'Native worker not found before deadline'
    command=['gdb','-q','-batch','-ex','set pagination off',
        '-ex','set auto-load safe-path /nonexistent','-ex','set debuginfod enabled off',
        '-ex','set sysroot /proc/'+str(target)+'/root',
        '-ex','set print thread-events off',
        '-ex','handle SIGPIPE nostop noprint pass',
        '-ex','handle SIGUSR1 nostop noprint pass',
        '-ex','handle SIGSEGV stop print pass','-ex','handle SIGABRT stop print pass',
        '-p',str(target),'-ex','continue','-ex','info threads',
        '-ex','thread apply all bt 24','-ex','info proc mappings','-ex','detach']
    record['command']=command
    (a.output/'attach.json').write_text(json.dumps(record,indent=2)+'\n')
    with (a.output/'gdb.log').open('w') as log:
        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            child.wait(timeout=max(30,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            child.send_signal(signal.SIGINT)
            try:child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=20)
    text=(a.output/'gdb.log').read_text(errors='replace')
    record.update(exit_code=child.returncode,signal_caught=any(v in text for v in [
        'received signal SIGSEGV','received signal SIGABRT']),
        finished_utc=datetime.now(timezone.utc).isoformat())
    (a.output/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    for directory,dirs,files in os.walk(a.output):
        os.chown(directory,1000,1000)
        for name in files:os.chown(Path(directory)/name,1000,1000)
    print(json.dumps(record))


if __name__=='__main__':
    main()

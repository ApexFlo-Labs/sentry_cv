#!/usr/bin/env python3
"""Post-performance K3s/containerd check using entirely separate runtime state.

Tests projected ConfigMap/Secret input, Intel devices, /healthz and package API
output, revision update and pod replacement. Privileged hostNetwork access is
explicit; this does not qualify a production device plugin or remote ingest.
"""
import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from urllib.request import urlopen

import psutil
from run_guarded_case import read_linux_sensors


def isolated_image_import_command(ctr):
    # containerd2.2 restricts discard-unpacked-layers to its local importer.
    # The socket/namespace prefix is always the disposable validation CRI.
    return [*ctr,'images','import','--local','--discard-unpacked-layers','-']


def isolated_containerd_config(content, snapshots, state, address):
    # JSON-quoted strings are also valid basic TOML strings.
    q = lambda p: json.dumps(str(p))
    return f'''version = 3
root = {q(content)}
state = {q(state)}
[grpc]
address = {q(address)}
[plugins.'io.containerd.snapshotter.v1.overlayfs']
root_path = {q(snapshots)}
[plugins.'io.containerd.cri.v1.images']
discard_unpacked_layers = true
[plugins.'io.containerd.cri.v1.images'.pinned_images]
sandbox = "registry.k8s.io/pause:3.10"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]
runtime_type = "io.containerd.runc.v2"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]
SystemdCgroup = true
'''


def isolated_kubelet_root(runtime):
    directory=Path(runtime)/'kubelet'
    # Linux sockaddr_un.sun_path includes its terminating byte. Kubelet binds
    # pod-resource and plugin-registration sockets below its configured root.
    if not directory.is_absolute()or len(os.fsencode(str(directory/'plugins_registry/kubelet.sock')))>=108:
        raise ValueError('Private kubelet socket path exceeds Linux Unix socket limit')
    return directory


def safe(value, token=''):
    if isinstance(value,str):
        if token:value=value.replace(token,'<redacted-token>')
        return re.sub(r'(?:rtsps?|https?|tcp)://[^\s"<>]+','<redacted-url>',value)
    if isinstance(value,dict):return {key:safe(item,token) for key,item in value.items()}
    if isinstance(value,(list,tuple)):return [safe(item,token) for item in value]
    return value


def safe_output(value,token=''):
    """Redact JSON values after parsing so escaped build history stays valid."""
    try:parsed=json.loads(value)
    except (ValueError,TypeError):return safe(value,token)
    return json.dumps(safe(parsed,token),indent=2)+'\n'


def verified_config_digest(reported_id,expected_id,read_content,architecture='amd64',expected_kind='config'):
    """Verify OCI content bytes; CRI may report a manifest ID, not config ID."""
    if expected_kind not in ('config','descriptor'):raise ValueError('Unknown expected image identity kind')
    current=expected_id if expected_kind=='descriptor'else reported_id
    seen=[]
    for _ in range(5):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',current)or current in seen:
            raise ValueError('Invalid/cyclic image content descriptor')
        seen.append(current);payload=read_content(current)
        if isinstance(payload,str):payload=payload.encode()
        if 'sha256:'+hashlib.sha256(payload).hexdigest()!=current:
            raise ValueError('Image content bytes do not match descriptor')
        document=json.loads(payload)
        if document.get('rootfs',{}).get('type')=='layers':
            if expected_kind=='config'and current!=expected_id:
                raise ValueError('Imported OCI config differs from frozen Docker config')
            if expected_kind=='descriptor'and reported_id not in seen:
                raise ValueError('CRI identity is outside frozen Docker descriptor chain')
            if document.get('os')!='linux'or document.get('architecture')!=architecture:
                raise ValueError('Imported config platform differs from frozen image')
            return {'reported_cri_id':reported_id,'verified_config_digest':current,
                    'verified_descriptor_chain':seen}
        if isinstance(document.get('config'),dict)and document['config'].get('digest'):
            current=document['config']['digest'];continue
        if isinstance(document.get('manifests'),list):
            choices=[x for x in document['manifests']if x.get('platform',{}).get('os')=='linux'
                     and x.get('platform',{}).get('architecture')==architecture]
            if len(choices)!=1:raise ValueError('Imported image has ambiguous/missing platform descriptor')
            current=choices[0]['digest'];continue
        raise ValueError('Unsupported image identity document')
    raise ValueError('Image descriptor chain too deep')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--image', required=True, help='Expected local Docker config SHA256')
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--port', type=int, default=18382)
    parser.add_argument('--output-name',default='kubernetes-current',
                        help='Fresh artifact directory; retain earlier failed setup attempts')
    parser.add_argument('--ram-image-store',action='store_true',
                        help='Disposable image snapshots in existing tmpfs; functional test only, never capacity evidence')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',args.output_name):
        parser.error('output-name must be a safe single directory name')
    assert os.geteuid() == 0
    root = args.root.resolve()
    repo = root/'repo'
    parent = root/'artifacts'/args.output_name
    parent.mkdir(exist_ok=True)
    phase_file=os.environ.get('SENTINEL_VALIDATION_PHASE_FILE')
    attempt=Path(phase_file).name.split('.')[0] if phase_file else 'standalone-'+secrets.token_hex(4)
    assert re.fullmatch(r'attempt-[1-9][0-9]*|standalone-[0-9a-f]{8}',attempt)
    out=parent/attempt
    out.mkdir(exist_ok=False)
    harness=Path(__file__).read_bytes()
    (out/'executed-kubernetes-helper.py').write_bytes(harness)
    private = root/('kubernetes-private-'+secrets.token_hex(4))
    private.mkdir(mode=0o700)
    scratch = Path('/tmp')/private.name
    scratch.mkdir(mode=0o700)
    runtime = Path('/run')/private.name
    runtime.mkdir(mode=0o700)
    kubelet_root=isolated_kubelet_root(runtime)
    snapshots=private/'snapshots'
    if args.ram_image_store:
        assert subprocess.check_output(['findmnt','-n','-o','FSTYPE','-T','/dev/shm'],text=True).strip()=='tmpfs'
        snapshots=Path('/dev/shm')/('sentry-'+private.name)
        snapshots.mkdir(mode=0o700)
    token = secrets.token_hex(32)
    report = {'scope': __doc__, 'image_config_id': args.image, 'passed': False,
        'harness_sha256':hashlib.sha256(harness).hexdigest(),
        'capacity_qualified': False, 'events': [], 'phases': [],
        'resource_budget': {'root_preparation_min_free_GiB':2,
            'root_CV_load_min_free_GiB':6,'private_snapshot_fs_min_free_GiB':2},
        'private_paths':{'data':str(private),'content':str(scratch),'runtime':str(runtime)},
        'control_plane': 'Separate containerd root/state/socket and K3s data/kubeconfig/ports; original runtime never addressed'}
    report['private_paths']['snapshots']=str(snapshots)
    report['private_paths']['kubelet']=str(kubelet_root)
    report['image_snapshot_store']='RAM tmpfs; node RAM includes disposable image pages'if args.ram_image_store else 'private disk snapshots'
    children = []
    stop = threading.Event()
    load = threading.Event()
    sampler = None
    node_password = Path('/etc/rancher/node/password')
    original_password = None
    original_stat = None
    server = None
    daemon = None
    kubeconfig = private/'kubeconfig.yaml'
    address = runtime/'containerd.sock'
    cri = ['k3s','crictl','--runtime-endpoint','unix://'+str(address),
           '--image-endpoint','unix://'+str(address)]
    ctr = ['ctr','--address',str(address),'--namespace','k8s.io']
    namespace = 'sentry-validation-c15'
    slice_base='scvtest'+private.name.rsplit('-',1)[-1]
    slice_unit=slice_base+'.slice'
    keeper_unit=slice_base+'keeper.service'
    slice_created=False
    pod_uid = None
    blocked = [None]
    health_stream = gzip.open(out/'health.jsonl.gz','wt')

    def save():
        (out/'report.json').write_text(json.dumps(safe(report,token),indent=2)+'\n')
        (parent/'report.json').write_text(json.dumps(safe({**report,'attempt_directory':str(out)},token),indent=2)+'\n')

    def cancel(*_):
        raise InterruptedError(blocked[0] or 'Test interrupted')

    for sig in [signal.SIGTERM,signal.SIGINT,signal.SIGUSR1]:
        signal.signal(sig,cancel)

    def resources():
        hot = 0
        with gzip.open(out/'resources.jsonl.gz','wt') as stream:
            while not stop.is_set():
                try:
                    readings = read_linux_sensors()
                    maximum = max(x.celsius for x in readings)
                    hot = hot+1 if maximum >= 90 else 0
                    root_free = shutil.disk_usage('/tmp').free
                    home_free = shutil.disk_usage(snapshots if snapshots.exists()else snapshots.parent).free
                    private_free=shutil.disk_usage(private).free
                    available = psutil.virtual_memory().available
                    row = {'utc':datetime.now(timezone.utc).isoformat(),
                        'temperature_C':maximum,'root_free_bytes':root_free,
                        'snapshot_fs_free_bytes':home_free,'available_RAM_bytes':available,
                        'private_data_fs_free_bytes':private_free,
                        'CV_load_active':load.is_set()}
                    stream.write(json.dumps(row)+'\n');stream.flush()
                    guarded=bool(os.environ.get('SENTINEL_VALIDATION_RUN_ID'))
                    if ((not guarded and (hot>=9 or maximum>=103)) or available<4*2**30 or home_free<2*2**30 or private_free<2*2**30
                            or root_free<(6 if load.is_set() else 2)*2**30):
                        raise RuntimeError('Temperature/RAM/disk budget reached')
                except Exception as exc:
                    blocked[0]=type(exc).__name__+': '+str(exc)
                    os.kill(os.getpid(),signal.SIGUSR1)
                    return
                stop.wait(.5)

    def run(name, command, timeout=60, check=True):
        result=subprocess.run(command,capture_output=True,text=True,timeout=timeout)
        (out/(name+'.stdout')).write_text(safe_output(result.stdout,token))
        (out/(name+'.stderr')).write_text(safe_output(result.stderr,token))
        report['events'].append({'name':name,'command':safe(command,token),
                                'exit_code':result.returncode})
        save()
        if check and result.returncode:
            raise RuntimeError(name+' exited '+str(result.returncode))
        return result

    def kube(name,*arguments,check=True,timeout=60):
        return run(name,['k3s','kubectl','--kubeconfig',str(kubeconfig),*arguments],timeout,check)

    def health(phase):
        try:
            with urlopen('http://127.0.0.1:'+str(args.port)+'/healthz',timeout=5) as response:
                value=json.load(response)
        except Exception:
            value={}
        health_stream.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),
            'monotonic':time.monotonic(),'phase':phase,'health':safe(value,token)})+'\n')
        health_stream.flush()
        return value

    def wait_ready(phase,revision,seconds=240):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if server.poll() is not None:
                raise RuntimeError('Disposable K3s exited')
            value=health(phase)
            cameras=value.get('telemetry',{}).get('cameras',[])
            if (value.get('ready') and value.get('revision')==revision and len(cameras)==1
                    and cameras[0].get('fresh') is True
                    and cameras[0].get('processing',{}).get('totals',{}).get('inference_frames',0)>=10):
                report['phases'].append({'phase':phase,'revision':revision,
                    'stream_session_id':cameras[0].get('decoder',{}).get('stream_session_id')})
                return value
            time.sleep(3)
        raise RuntimeError('No healthy image output in phase '+phase)

    try:
        for unit in ['k3s.service','apexfabric-control.service']:
            assert subprocess.run(['systemctl','is-active','--quiet',unit]).returncode!=0
        assert Path('/sys/devices/system/cpu/intel_pstate/no_turbo').read_text().strip()=='1'
        assert not subprocess.check_output(['docker','ps','-q','--filter',
            'label=sentinel.validation_run='+args.run_id],text=True).strip()
        inspected=json.loads(subprocess.check_output(['docker','image','inspect',args.image],text=True))[0]
        assert inspected['Id']==args.image
        tags=inspected.get('RepoTags') or []
        image=next(t for t in tags if t.startswith('localhost/sentinel-cv:'))
        report['pod_image_tag']=image
        for port in [args.port,26443,26445,26446,10260,10262]:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',port))
        assert not node_password.is_symlink(),'Shared node credential is a symlink; refusing alteration'
        if node_password.exists():
            original_stat=node_password.stat();original_password=node_password.read_bytes()
            backup=private/'original-node-password.backup'
            backup.write_bytes(original_password);backup.chmod(0o600)
        assert shutil.disk_usage(snapshots if snapshots.exists()else snapshots.parent).free > inspected['Size']+2*2**30
        if args.ram_image_store:
            assert psutil.virtual_memory().available > inspected['Size']+8*2**30
        sampler=threading.Thread(target=resources,daemon=True);sampler.start()
        config=private/'containerd.toml'
        config.write_text(isolated_containerd_config(scratch/'content',snapshots,
            runtime/'state',address))
        decoded=tomllib.loads(config.read_text())
        assert decoded['grpc']['address']==str(address) and decoded['root']==str(scratch/'content')
        with (private/'containerd.raw.log').open('w') as log:
            daemon=subprocess.Popen(['taskset','-c','0-15','containerd','--config',str(config)],
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        children.append(daemon)
        deadline=time.monotonic()+30
        while not address.exists() and time.monotonic()<deadline:
            assert daemon.poll() is None,'Private containerd exited'
            time.sleep(.2)
        assert address.exists(),'Private CRI socket missing'
        # Import without retaining duplicate uncompressed layer blobs after
        # unpack. Content and snapshots have separate filesystem budgets.
        with (private/'docker-save.stderr').open('w') as err:
            exporter=subprocess.Popen(['docker','save',image],stdout=subprocess.PIPE,stderr=err)
        children.append(exporter)
        with (private/'image-import.raw.log').open('w') as log:
            importer=subprocess.Popen(isolated_image_import_command(ctr),
                stdin=exporter.stdout,stdout=log,stderr=subprocess.STDOUT)
        children.append(importer);exporter.stdout.close()
        assert importer.wait(timeout=600)==0,'Private containerd image import failed'
        assert exporter.wait(timeout=30)==0,'Docker export failed'
        run('pause-image-pull',[*ctr,'images','pull','registry.k8s.io/pause:3.10'],timeout=180)
        deadline=time.monotonic()+90
        while shutil.disk_usage('/tmp').free < 6*2**30 and time.monotonic()<deadline:
            time.sleep(1)
        assert shutil.disk_usage('/tmp').free >= 6*2**30,'Private image GC did not restore CV load disk headroom'
        runtime_image=json.loads(run('runtime-image-identity',[*cri,'inspecti',image]).stdout)
        identity_index=[0]
        def read_identity_content(digest):
            identity_index[0]+=1
            return run('runtime-content-identity-'+str(identity_index[0]),
                [*ctr,'content','get',digest]).stdout
        report['imported_image_identity']=verified_config_digest(
            runtime_image['status']['id'],args.image,read_identity_content,
            architecture=inspected['Architecture'],
            expected_kind=('descriptor'if inspected.get('Descriptor',{}).get('digest')==args.image
                and inspected['Descriptor'].get('mediaType')in (
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json')else'config'))
        report['imported_image_identity']['frozen_docker_engine_id']=args.image
        report['imported_image_identity']['docker_descriptor']=inspected.get('Descriptor')
        save()
        # Kubelet cgroupRoot is a logical cgroupfs name, mapped by its systemd
        # driver to this slice. Never reconcile the original /kubepods.slice.
        run('private-cgroup-holder',['systemd-run','--unit',keeper_unit,'--slice',slice_unit,
            '--property=Type=oneshot','--property=RemainAfterExit=yes',
            '--property=CPUAccounting=yes','--property=MemoryAccounting=yes',
            '--property=TasksAccounting=yes','/usr/bin/true'])
        slice_created=True
        deadline=time.monotonic()+10
        while not (Path('/sys/fs/cgroup')/slice_unit).exists() and time.monotonic()<deadline:
            time.sleep(.1)
        assert (Path('/sys/fs/cgroup')/slice_unit).is_dir(),'Private cgroup slice unavailable'
        report['private_cgroup_root']='/'+slice_base
        kconfig=private/'k3s-config.yaml';kconfig.write_text('{}\n')
        token_file=private/'server-token';token_file.write_text(token);token_file.chmod(0o600)
        environment={k:v for k,v in os.environ.items() if not k.startswith('K3S_')}
        command=['taskset','-c','0-15','k3s','server','--config',str(kconfig),
            '--token-file',str(token_file),'--data-dir',str(private/'k3s-data'),
            '--write-kubeconfig',str(kubeconfig),'--write-kubeconfig-mode','600',
            '--https-listen-port','26443','--lb-server-port','26446',
            '--container-runtime-endpoint','unix://'+str(address),
            '--image-service-endpoint','unix://'+str(address),
            '--kubelet-arg','root-dir='+str(kubelet_root),
            '--kubelet-arg','cgroup-root=/'+slice_base,
            '--kubelet-arg','protect-kernel-defaults=true',
            '--kubelet-arg','port=10260','--kubelet-arg','healthz-port=10262',
            '--kubelet-arg','eviction-hard=memory.available<512Mi,nodefs.available<1Gi,imagefs.available<1Gi',
            '--node-name','sentry-validation-c15','--node-ip','192.168.1.64',
            '--cluster-cidr','10.62.0.0/16','--service-cidr','10.63.0.0/16',
            '--flannel-backend','none','--disable-network-policy','--disable-kube-proxy',
            '--disable','traefik,servicelb,metrics-server,local-storage,coredns']
        with (private/'k3s.raw.log').open('w') as log:
            server=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
                start_new_session=True,env=environment)
        children.append(server);report['server_command']=command;save()
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            assert server.poll() is None,'Private K3s exited during startup'
            if kubeconfig.exists():
                result=subprocess.run(['k3s','kubectl','--kubeconfig',str(kubeconfig),
                    'get','--raw=/readyz'],capture_output=True,timeout=10)
                if result.returncode==0:break
            time.sleep(3)
        else:raise RuntimeError('Private K3s API startup timeout')
        sys.path.insert(0,str(repo/'scripts'))
        from v8_capacity_probe import desired_state
        desired=desired_state(1,8,full_usecases=True)
        namespace_manifest={'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}}
        metadata={'namespace':namespace}
        service_account={'apiVersion':'v1','kind':'ServiceAccount',
            'metadata':dict(metadata,name='cv-check'),'automountServiceAccountToken':False}
        cm={'apiVersion':'v1','kind':'ConfigMap','metadata':dict(metadata,name='inputs'),
            'data':{'desired_state.json':json.dumps(desired),
                    'agent.py':(repo/'scripts/isolated_validation/v8_capture_agent.py').read_text()}}
        secret={'apiVersion':'v1','kind':'Secret','metadata':dict(metadata,name='cameras'),
            'type':'Opaque','data':{'capacity-01.url':base64.b64encode((args.source_url+'\n').encode()).decode()}}
        groups=sorted({p.stat().st_gid for p in [*Path('/dev/dri').glob('renderD*'),
            Path('/dev/accel/accel0')] if p.exists()})
        mounts=[{'name':'inputs','mountPath':'/configs','readOnly':True},
            {'name':'cameras','mountPath':'/run/secrets/sentinel/cameras','readOnly':True},
            {'name':'delivery','mountPath':'/run/sentinel-delivery'},
            {'name':'dri','mountPath':'/dev/dri'},{'name':'accel','mountPath':'/dev/accel'}]
        pod={'apiVersion':'v1','kind':'Pod','metadata':dict(metadata,name='cv-check'),
            'spec':{'hostNetwork':True,'dnsPolicy':'Default','nodeName':'sentry-validation-c15',
                'serviceAccountName':'cv-check','automountServiceAccountToken':False,
                'restartPolicy':'Never','securityContext':{'runAsUser':10001,'runAsGroup':10001,
                    'fsGroup':10001,'supplementalGroups':groups},
                'volumes':[{'name':'inputs','configMap':{'name':'inputs'}},
                    {'name':'cameras','secret':{'secretName':'cameras'}},
                    {'name':'delivery','emptyDir':{}},{'name':'capture','emptyDir':{}},
                    {'name':'dri','hostPath':{'path':'/dev/dri','type':'Directory'}},
                    {'name':'accel','hostPath':{'path':'/dev/accel','type':'Directory'}}],
                'containers':[{'name':'agent','image':image,'imagePullPolicy':'Never',
                    'resources':{'requests':{'cpu':'100m','memory':'128Mi'},
                                 'limits':{'cpu':'1','memory':'1Gi'}},
                    'command':['python3','/configs/agent.py','--socket','/run/sentinel-delivery/agent.sock',
                        '--schemas','/opt/traffic-pilot/contracts/sentry-v8','--log','/capture/packages.jsonl',
                        '--status','/capture/status.json'],
                    'volumeMounts':[{'name':'inputs','mountPath':'/configs','readOnly':True},
                        {'name':'delivery','mountPath':'/run/sentinel-delivery'},
                        {'name':'capture','mountPath':'/capture'}]},
                    {'name':'cv','image':image,'imagePullPolicy':'Never',
                     'env':[{'name':'APEX_API_PORT','value':str(args.port)}],
                     'securityContext':{'privileged':True},'volumeMounts':mounts,
                     'resources':{'requests':{'cpu':'1','memory':'3Gi'},'limits':{'cpu':'4','memory':'8Gi'}},
                     'readinessProbe':{'httpGet':{'path':'/readyz','port':args.port},'periodSeconds':3},
                     'livenessProbe':{'httpGet':{'path':'/healthz','port':args.port},
                         'initialDelaySeconds':10,'periodSeconds':5}}]}}
        manifest=out/'inputs-and-pod.json'
        manifest.write_text(json.dumps({'apiVersion':'v1','kind':'List',
            'items':[namespace_manifest,service_account,cm,secret,pod]},indent=2))
        load.set()
        kube('apply','apply','-f',str(manifest))
        first=wait_ready('pod-startup',1)
        cgroup=first.get('telemetry',{}).get('system',{}).get('container_cgroup',{})
        assert cgroup.get('measurement_status')=='measured','Kubernetes cgroup metrics unavailable'
        assert (cgroup.get('memory_limit_mb')==8192.0 and cgroup.get('cpu_limit_cores')==4.0), (
            'Health does not report actual Kubernetes CV resource limits')
        placement=first['telemetry']['cameras'][0].get('device_placement',{})
        assert placement.get('decode','').startswith('GPU/VAAPI')
        assert placement.get('vehicle_person_detection','').startswith('GPU/')
        assert all(placement.get(k,'').startswith('NPU') for k in
                   ['face_embedding','face_pose','body_embedding','gait_embedding'])
        pod_data=json.loads(kube('pod-initial','-n',namespace,'get','pod','cv-check','-o','json').stdout)
        pod_uid=pod_data['metadata']['uid'];report['initial_pod_uid']=pod_uid
        for _ in range(12):time.sleep(5);health('steady')
        desired['revision']=2;desired['cameras'][0]['geometry']['zones'][0]['poly'][0]=[.02,.02]
        kube('project-config','-n',namespace,'patch','configmap','inputs','--type=merge','-p',
            json.dumps({'data':{'desired_state.json':json.dumps(desired)}}))
        changed=wait_ready('projected-revision',2,180)
        report['session_preserved_on_geometry_update']=(first['telemetry']['cameras'][0]['decoder']['stream_session_id']
            == changed['telemetry']['cameras'][0]['decoder']['stream_session_id'])
        assert report['session_preserved_on_geometry_update'],'Geometry-only update changed stream session'
        time.sleep(8)
        captured=json.loads(kube('agent-status-before-replacement','-n',namespace,'exec','cv-check',
            '-c','agent','--','cat','/capture/status.json').stdout)
        assert captured.get('evidence_bytes',0)>0 and captured.get('embeddings',0)>0
        profiles=captured.get('per_camera',{}).get('capacity-01',{}).get('embedding_profiles',{})
        assert all(profiles.get(k,0)>0 for k in
            ['siglip2-base-v1','adaface-ir101-v18.1','transreid-ssl-v18.1','gaitbase-v18.1']), (
            'Configured identity/scene positive paths not all exercised')
        packets=kube('packages-before-replacement','-n',namespace,'exec','cv-check','-c','agent',
            '--','cat','/capture/packages.jsonl')
        assert any(json.loads(line).get('config_revision')==2 for line in packets.stdout.splitlines()), (
            'New revision is healthy but not represented in accepted packages')
        kube('delete-first-pod','-n',namespace,'delete','pod','cv-check','--wait=true','--timeout=45s')
        pod_path=out/'replacement-pod.json';pod_path.write_text(json.dumps(pod,indent=2))
        kube('apply-replacement','apply','-f',str(pod_path))
        replacement=wait_ready('pod-replacement',2)
        new_pod=json.loads(kube('pod-final','-n',namespace,'get','pod','cv-check','-o','json').stdout)
        assert new_pod['metadata']['uid']!=pod_uid,'Replacement reused old pod UID'
        pod_uid=new_pod['metadata']['uid'];report['replacement_pod_uid']=pod_uid
        assert (replacement['telemetry']['cameras'][0]['decoder']['stream_session_id']
            !=first['telemetry']['cameras'][0]['decoder']['stream_session_id'])
        for _ in range(4):time.sleep(5);health('replacement-steady')
        report['final_agent']=json.loads(kube('agent-status-final','-n',namespace,'exec','cv-check',
            '-c','agent','--','cat','/capture/status.json').stdout)
        assert report['final_agent'].get('evidence_bytes',0)>0 and report['final_agent'].get('embeddings',0)>0
        kube('packages-final','-n',namespace,'exec','cv-check','-c','agent','--','cat','/capture/packages.jsonl')
        kube('cv-log','-n',namespace,'logs','cv-check','-c','cv')
        report['final_health']=replacement
        report['passed']=True
    except BaseException as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        report['blocked_resource_or_sensor']=blocked[0]
    finally:
        for sig in [signal.SIGTERM,signal.SIGINT,signal.SIGUSR1]:signal.signal(sig,signal.SIG_IGN)
        stop.set()
        if sampler is not None:sampler.join(timeout=2)
        # Stop only this private CRI's pods, even if the API is unavailable.
        if daemon is not None and daemon.poll() is None:
            try:
                containers=json.loads(subprocess.check_output([*cri,'ps','-a','-o','json'],text=True,timeout=10))
                for item in containers.get('containers') or []:
                    labels=item.get('labels',{})
                    if labels.get('io.kubernetes.pod.namespace')!=namespace:continue
                    assert labels.get('io.kubernetes.pod.name')=='cv-check'
                    stopped=subprocess.run([*cri,'stop','--timeout','2',item['id']],capture_output=True,timeout=10)
                    removed=subprocess.run([*cri,'rm','--force',item['id']],capture_output=True,timeout=10)
                    assert stopped.returncode==0 and removed.returncode==0,'Owned CRI container cleanup failed'
                pods=json.loads(subprocess.check_output([*cri,'pods','-o','json'],text=True,timeout=10))
                for item in pods.get('items') or []:
                    if item.get('metadata',{}).get('namespace')!=namespace:continue
                    assert item['metadata']['name']=='cv-check'
                    stopped=subprocess.run([*cri,'stopp',item['id']],capture_output=True,timeout=10)
                    removed=subprocess.run([*cri,'rmp','--force',item['id']],capture_output=True,timeout=10)
                    assert stopped.returncode==0 and removed.returncode==0,'Owned CRI sandbox cleanup failed'
                assert not subprocess.check_output([*ctr,'tasks','list','--quiet'],text=True,timeout=5).strip(), (
                    'Private runtime still has live tasks after pod cleanup')
            except Exception as exc:
                report['CRI_cleanup_error']=str(exc)
                # This is a fresh private runtime: kill any remaining private
                # tasks before terminating its daemon, never the original CRI.
                try:
                    ids=subprocess.check_output([*ctr,'tasks','list','--quiet'],text=True,timeout=5).split()
                    for cid in ids:
                        assert re.fullmatch(r'[0-9a-f]{64}',cid)
                        subprocess.run([*ctr,'tasks','kill','--all','--signal','SIGKILL',cid],
                            capture_output=True,timeout=5,check=True)
                        subprocess.run([*ctr,'tasks','rm','--force',cid],
                            capture_output=True,timeout=5,check=True)
                except Exception as fallback:report['private_task_kill_error']=str(fallback)
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait()
        if slice_created:
            subprocess.run(['systemctl','stop',keeper_unit,slice_unit],capture_output=True,timeout=20)
            subprocess.run(['systemctl','reset-failed',keeper_unit,slice_unit],capture_output=True,timeout=10)
        for name in ['k3s.raw.log','containerd.raw.log','image-import.raw.log','docker-save.stderr']:
            source=private/name
            if source.exists():(out/name.replace('.raw','')).write_text(safe(source.read_text(errors='replace'),token))
        try:
            if original_password is not None:
                if not node_password.exists() or node_password.read_bytes()!=original_password:
                    node_password.write_bytes(original_password)
                os.chmod(node_password,original_stat.st_mode&0o777)
                os.chown(node_password,original_stat.st_uid,original_stat.st_gid)
                os.utime(node_password,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
                report['original_node_credential_preserved']=node_password.read_bytes()==original_password
            elif server is not None:
                if node_password.exists():node_password.unlink()
                report['original_node_credential_preserved']=not node_password.exists()
        except Exception as exc:report['original_credential_restore_error']=type(exc).__name__
        health_stream.close()
        report['passed']=bool(report['passed'] and report.get('original_node_credential_preserved')
            and not report.get('CRI_cleanup_error'))
        report['ended_utc']=datetime.now(timezone.utc).isoformat()
        save()
        # Derived private runtime/image copies and credentials; originals untouched.
        if not report.get('CRI_cleanup_error') and not report.get('original_credential_restore_error'):
            cleanup_paths=[scratch,private,runtime]
            if args.ram_image_store:cleanup_paths.append(snapshots)
            for path in cleanup_paths:
                try:shutil.rmtree(path)
                except OSError as exc:report.setdefault('private_cleanup_errors',[]).append(type(exc).__name__)
        else:
            report['private_paths_retained_for_recovery']=True
        if report.get('private_cleanup_errors'):report['passed']=False
        save()
        for entry in [out,*out.rglob('*')]:
            if not entry.is_symlink():os.chown(entry,1000,1000)
    print(json.dumps({'passed':report['passed'],'error':report.get('error'),
                     'report':str(out/'report.json')}))
    return 0 if report['passed'] else 2 if blocked[0] else 1


if __name__=='__main__':
    raise SystemExit(main())

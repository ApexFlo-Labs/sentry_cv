"""Offline safety/configuration tests; do not start Kubernetes or a model."""
import importlib.util
import json
from pathlib import Path
import sys
import tomllib
import hashlib

import pytest


@pytest.fixture
def helper(monkeypatch):
    directory=Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(directory))
    spec=importlib.util.spec_from_file_location('isolated_kubernetes',directory/'kubernetes_current_image.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_containerd_paths_and_gc_are_explicit(helper,tmp_path):
    text=helper.isolated_containerd_config(tmp_path/'content',tmp_path/'snapshots',
        tmp_path/'state',tmp_path/'cri.sock')
    config=tomllib.loads(text)
    assert config['version']==3
    assert config['root']==str(tmp_path/'content')
    assert config['state']==str(tmp_path/'state')
    assert config['grpc']['address']==str(tmp_path/'cri.sock')
    assert config['plugins']['io.containerd.snapshotter.v1.overlayfs']['root_path']==str(tmp_path/'snapshots')
    assert config['plugins']['io.containerd.cri.v1.images']['discard_unpacked_layers'] is True
    assert config['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']['runc']['options']['SystemdCgroup'] is True
    assert '/run/k3s/containerd' not in text and '/var/lib/rancher' not in text


def test_private_import_discard_flag_requires_local_importer(helper,tmp_path):
    prefix=['ctr','--address',str(tmp_path/'private.sock'),'--namespace','k8s.io']
    assert helper.isolated_image_import_command(prefix)==[
        *prefix,'images','import','--local','--discard-unpacked-layers','-']
    assert prefix[-1]=='k8s.io'  # Caller-owned socket/namespace prefix is unchanged.


def test_redaction_preserves_nested_json_quoting_and_values(helper):
    original={'log':'source="rtsps://user:password@host/live"\nfailed token=SECRET',
              'nested':['tcp://private:9001',{'count':8,'ok':True}]}
    sanitized=helper.safe(original,'SECRET')
    assert sanitized['log']=='source="<redacted-url>"\nfailed token=<redacted-token>'
    assert sanitized['nested']==['<redacted-url>',{'count':8,'ok':True}]
    assert json.loads(json.dumps(sanitized))==sanitized
    assert original['nested'][0]=='tcp://private:9001'


def test_serialized_history_redaction_keeps_json_valid(helper):
    value={'history':[{'created_by':'RUN curl "https://private/path" && echo done\n'}]}
    sanitized=helper.safe_output(json.dumps(value))
    assert json.loads(sanitized)['history'][0]['created_by']=='RUN curl "<redacted-url>" && echo done\n'


def image_documents():
    blobs={}
    def add(value):
        data=json.dumps(value,separators=(',',':')).encode()
        digest='sha256:'+hashlib.sha256(data).hexdigest();blobs[digest]=data
        return digest
    config=add({'architecture':'amd64','os':'linux','rootfs':{'type':'layers','diff_ids':[]},'config':{'User':'10001'}})
    manifest=add({'schemaVersion':2,'config':{'digest':config},'layers':[]})
    index=add({'schemaVersion':2,'manifests':[{'digest':manifest,'platform':{'architecture':'amd64','os':'linux'}}]})
    return blobs,config,manifest,index


@pytest.mark.parametrize('reported_kind',[1,2,3])
def test_image_identity_verifies_config_bytes_through_cri_descriptors(helper,reported_kind):
    blobs,config,manifest,index=image_documents()
    reported=[None,config,manifest,index][reported_kind]
    result=helper.verified_config_digest(reported,config,blobs.__getitem__)
    assert result['verified_config_digest']==config
    assert result['verified_descriptor_chain'][-1]==config
    assert result['reported_cri_id']==reported


def test_image_identity_rejects_substitution_and_corrupt_content(helper):
    blobs,config,manifest,index=image_documents()
    with pytest.raises(ValueError,match='config differs'):
        helper.verified_config_digest(manifest,'sha256:'+'0'*64,blobs.__getitem__)
    with pytest.raises(ValueError,match='bytes do not match'):
        helper.verified_config_digest(manifest,config,lambda _:b'{}')
    with pytest.raises(ValueError,match='platform differs'):
        helper.verified_config_digest(config,config,blobs.__getitem__,architecture='arm64')
    with pytest.raises(ValueError,match='missing platform'):
        helper.verified_config_digest(index,config,blobs.__getitem__,architecture='arm64')


@pytest.mark.parametrize('frozen_kind',[2,3])
def test_docker_manifest_id_is_verified_to_cri_config_without_weakening_pin(helper,frozen_kind):
    blobs,config,manifest,index=image_documents()
    frozen=[None,None,manifest,index][frozen_kind]
    result=helper.verified_config_digest(config,frozen,blobs.__getitem__,expected_kind='descriptor')
    assert result['verified_config_digest']==config
    assert result['verified_descriptor_chain'][0]==frozen
    with pytest.raises(ValueError,match='outside frozen'):
        helper.verified_config_digest('sha256:'+'0'*64,frozen,blobs.__getitem__,expected_kind='descriptor')


def test_private_kubelet_socket_root_fits_linux_namespace_limit(helper):
    runtime=Path('/run/kubernetes-private-2997df03')
    assert helper.isolated_kubelet_root(runtime)==runtime/'kubelet'
    with pytest.raises(ValueError,match='Unix socket limit'):
        helper.isolated_kubelet_root('/home/admin1/Documents/sentry-validation-20261111-c15/kubernetes-private-2997df03')

import io
import importlib.util
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('pacing',Path(__file__).with_name('mpegts_pacing.py'))
pacing=importlib.util.module_from_spec(spec);spec.loader.exec_module(pacing)


def stamp(n, flag=2):
    return bytes([(flag<<4)|(((n>>30)&7)<<1)|1,(n>>22)&255,
                  (((n>>15)&127)<<1)|1,(n>>7)&255,((n&127)<<1)|1])


def packet(pts,dts=None):
    payload=b'\x00\x00\x01\xe0\x00\x00\x80'+bytes([0xc0 if dts is not None else 0x80,10 if dts is not None else 5])
    payload+=stamp(pts,3 if dts is not None else 2)
    if dts is not None:payload+=stamp(dts,1)
    return (b'\x47\x41\x00\x10'+payload).ljust(188,b'\xff')


def test_pes_timestamps_and_byte_exact_groups_including_psi():
    prefix=b'\x47\x00\x00\x10'+b'\xff'*184
    packets=prefix+packet(18000,0)+packet(9000,9000)+packet(27000,18000)
    groups=list(pacing.frame_groups(io.BytesIO(packets)))
    assert [s for s,_ in groups]==[(18000,0),(9000,9000),(27000,18000)]
    assert b''.join(body for _,body in groups)==packets


def test_actual_send_clock_prevents_catchup_after_pause_or_blocked_socket():
    clock=[0.]
    def wait(seconds):clock[0]+=seconds
    pacer=pacing.FramePacer(lambda:clock[0],wait)
    assert pacer.pace(0)==0
    assert pacer.pace(9000)==pytest.approx(.1)
    clock[0]+=10
    assert pacer.pace(18000)==pytest.approx(10.1)
    assert pacer.pace(27000)==pytest.approx(10.2)
    assert pacer.pace(36000)==pytest.approx(10.3)


def test_decode_order_not_presentation_order_and_timestamp_wrap():
    clock=[0.]
    pacer=pacing.FramePacer(lambda:clock[0],lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    pacer.pace((1<<33)-4500)
    assert pacer.pace(4500)==pytest.approx(.1)


@pytest.mark.parametrize('ticks',[0,-9000,90000*6])
def test_unsupported_timestamp_discontinuity_is_explicit(ticks):
    p=pacing.FramePacer(lambda:0,lambda _:None)
    p.pace(0)
    with pytest.raises(ValueError):p.pace(ticks % (1<<33))


def test_truncated_packets_and_oversized_frame_groups_rejected():
    with pytest.raises(ValueError):list(pacing.frame_groups(io.BytesIO(packet(0)+b'x')))
    with pytest.raises(ValueError):list(pacing.frame_groups(io.BytesIO(packet(0)),100))


def test_source_audit_distinguishes_burst_and_missing_capture():
    spec=importlib.util.spec_from_file_location('source_audit',Path(__file__).with_name('analyze_source_pacing.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    rows=[{'first_bytes_submitted_mono':t,'dts90k':i*9000,'epoch':1,'producer_pid':12}
          for i,t in enumerate([0,.1,.2,.3])]
    assert module.analyze(rows,0,1)['burst_free']
    rows[2]['first_bytes_submitted_mono']=.11
    assert not module.analyze(rows,0,1)['burst_free']
    assert not module.analyze([],0,1)['burst_free']

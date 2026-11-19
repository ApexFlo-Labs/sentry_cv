"""Bounded pre-admission shedding must never block the native streaming thread."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services' / 'worker'))
from pipeline.dlstreamer_bridge import DlsMotionPolicyGate, AdmittedFrame


def gate(capacity=2):
    counters=[]
    telemetry=SimpleNamespace(set_gauge=lambda *a:None,count=lambda *a:counters.append(a))
    g=DlsMotionPolicyGate(None,telemetry,object(),8,10**9,maximum_pending=capacity)
    g.test_counts=counters
    return g


def record(n):
    return AdmittedFrame(n,n*100,1000.+n,1000.+n,'TRACKING','active')


def test_full_gate_returns_without_wait_and_never_discards_accepted_outputs():
    g=gate();assert g._reserve(record(1)) and g._reserve(record(2))
    result=[];thread=threading.Thread(target=lambda:result.append(g._reserve(record(3))),daemon=True)
    thread.start();thread.join(.5)
    try:
        assert not thread.is_alive(), 'a pad callback must not wait for a consumer'
        assert result==[False] and g.pending_admissions==2
        assert g.rejected_before_admission==1
        assert g.test_counts==[('primary_pre_admission_rejected_frames',)]
        assert g.take(100)==record(1)
        assert g._reserve(record(4))
        assert g.take(200)==record(2) and g.take(400)==record(4)
        with pytest.raises(RuntimeError,match='no admission'):g.take(300)
    finally:g.close()


def test_epoch_reset_cannot_relabel_or_resurrect_old_admissions():
    g=gate();g._reserve(record(1));g._reserve(record(2))
    assert not g._reserve(record(3))
    g.reset_session(object())
    assert g.pending_admissions==0 and g.oldest_pending_age_seconds==0
    with pytest.raises(RuntimeError,match='no admission'):g.take(100)
    assert g._reserve(record(4)) and g.take(400).observed_at==1004.


def test_closed_or_suspended_gate_rejects_without_overload_or_new_work():
    g=gate();assert g._reserve(record(1))
    g.suspend();assert not g._reserve(record(2))
    assert g.rejected_before_admission==0 and g.pending_admissions==1
    with pytest.raises(RuntimeError,match='unfinished'):g.resume(object())
    assert g.take(100)==record(1)
    g.resume(object());assert g._reserve(record(3));g.close()
    assert not g._reserve(record(4)) and g.take(300)==record(3)


def test_recovery_decision_uses_atomic_fence_not_earlier_zero_snapshot():
    from pipeline.surface_stream import _fence_native_recovery
    g = gate()
    assert g.pending_admissions == 0  # The earlier diagnostic snapshot.
    assert g._reserve(record(1))      # Producer wins the race before fencing.
    requests = []
    coordinator = SimpleNamespace(request_rebuild=lambda *args, **kw: requests.append((args, kw)))
    assert _fence_native_recovery(g, coordinator, 'camera', {'stage': 'waiting'}) == 1
    assert requests == [(('camera', 'stalled_native_admissions'),
                         {'pending_admissions': 1, 'native_progress': {'stage': 'waiting'}})]
    assert not g._reserve(record(2))
    with pytest.raises(RuntimeError, match='unfinished'):
        g.resume(object())
    assert g.take(100) == record(1)  # No accepted work/timestamp was discarded.


def test_empty_fenced_recovery_keeps_camera_local_restart():
    from pipeline.surface_stream import _fence_native_recovery
    g = gate()
    coordinator = SimpleNamespace(request_rebuild=lambda *a, **kw: pytest.fail('no shared rebuild needed'))
    assert _fence_native_recovery(g, coordinator, 'camera', {}) == 0
    g.resume(object())
    assert g._reserve(record(1))


def test_concurrent_fence_returns_exact_count_of_all_winning_reservations():
    g = gate(3)
    barrier = threading.Barrier(17)
    admitted = []
    def reserve(n):
        barrier.wait()
        if g._reserve(record(n)):
            admitted.append(n)
    threads = [threading.Thread(target=reserve, args=(n,)) for n in range(1, 17)]
    for thread in threads:
        thread.start()
    barrier.wait()
    pending = g.suspend()
    for thread in threads:
        thread.join(1)
    assert all(not thread.is_alive() for thread in threads)
    assert pending == g.pending_admissions == len(admitted)
    assert not g._reserve(record(100))
    for n in admitted:
        assert g.take(n*100) == record(n)


def test_native_output_age_uses_oldest_admission_not_wall_time_or_new_input(monkeypatch):
    import pipeline.dlstreamer_bridge as module
    now=[10.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    g=gate();assert g._reserve(record(1));now[0]=11.;assert g._reserve(record(2))
    now[0]=25.;assert not g._reserve(record(3))
    assert g.oldest_pending_age_seconds==15.
    assert g.take(100)==record(1) and g.oldest_pending_age_seconds==14.
    assert g.take(200)==record(2) and g.oldest_pending_age_seconds==0


def test_duplicate_pts_preserve_frozen_records_and_individual_deadlines(monkeypatch):
    import pipeline.dlstreamer_bridge as module
    now=[10.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    g=gate();a=record(1);b=AdmittedFrame(2,a.pts,1002.,1002.,'TRACKING','active')
    assert g._reserve(a);now[0]=11.;assert g._reserve(b);now[0]=15.
    assert g.take(a.pts)==a and g.oldest_pending_age_seconds==4.
    assert g.take(a.pts)==b and not g._reservation_times


def test_concurrent_producers_never_exceed_bound_or_drop_admitted_records():
    g=gate(3);results=[];lock=threading.Lock();barrier=threading.Barrier(17)
    def reserve(n):
        barrier.wait();accepted=g._reserve(record(n))
        with lock:results.append((n,accepted))
    threads=[threading.Thread(target=reserve,args=(n,))for n in range(1,17)]
    for thread in threads:thread.start()
    barrier.wait()
    for thread in threads:thread.join(1)
    assert all(not thread.is_alive()for thread in threads)
    admitted=[n for n,accepted in results if accepted]
    assert len(admitted)==g.pending_admissions==3 and g.rejected_before_admission==13
    for n in admitted:assert g.take(n*100)==record(n)
    assert g.pending_admissions==0


def test_overload_probe_is_unevaluated_not_quiet_or_negative(monkeypatch):
    from pipeline.frame_time import DecodedFrameClock
    import sys
    statuses=SimpleNamespace(OK='OK',DROP='DROP')
    gst=SimpleNamespace(PadProbeReturn=statuses)
    monkeypatch.setitem(sys.modules,'gi.repository',SimpleNamespace(Gst=gst))
    monkeypatch.setitem(sys.modules,'gstgva',SimpleNamespace(VideoFrame=lambda *a,**kw:SimpleNamespace(regions=lambda:[])))
    calls=[]
    telemetry=SimpleNamespace(set_gauge=lambda *a:None,count=lambda *a:calls.append(('count',a)),
                              frame=lambda **kw:calls.append(('frame',kw)))
    clock=DecodedFrameClock();clock.record(100,1001.)
    switch=SimpleNamespace(tracking=True,update=lambda *a:('TRACKING',True),
                           observe_models=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('not evaluated')))
    g=DlsMotionPolicyGate(switch,telemetry,clock,8,10**9,maximum_pending=1)
    assert g._reserve(record(1))
    pad=SimpleNamespace(get_current_caps=lambda:None)
    info=SimpleNamespace(get_buffer=lambda:SimpleNamespace(pts=100))
    assert g.probe(pad,info)=='DROP'
    assert g.pending_admissions==1 and g.quiet==0 and g.drain_quiet()==[]
    assert calls==[('count',('primary_pre_admission_rejected_frames',)),('frame',{'inferred':None})]
    assert g.take(100)==record(1)

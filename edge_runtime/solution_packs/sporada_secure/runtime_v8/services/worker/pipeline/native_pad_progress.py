"""One native snapshot call replaces per-buffer Python observer callbacks."""
import ctypes
import os
from pathlib import Path


class Entry(ctypes.Structure):
    _fields_ = [('count',ctypes.c_uint64),('last_ns',ctypes.c_uint64),('pts',ctypes.c_uint64)]


_LIBRARIES = {}


def load_library(path):
    path = str(Path(path).resolve())
    if path not in _LIBRARIES:
        # Pad callbacks may outlive a Python wrapper during native teardown.
        # Keep their code resident until process exit, in addition to C state
        # reference counting and explicit probe removal.
        mode = os.RTLD_LOCAL | os.RTLD_NOW | getattr(os,'RTLD_NODELETE',0)
        lib = ctypes.CDLL(path,mode=mode)
        lib.sentinel_progress_abi.restype = ctypes.c_uint
        lib.sentinel_progress_entry_size.restype = ctypes.c_uint
        if lib.sentinel_progress_abi()!=1 or lib.sentinel_progress_entry_size()!=ctypes.sizeof(Entry):
            raise RuntimeError('incompatible native progress ABI')
        lib.sentinel_progress_new.argtypes=[]
        lib.sentinel_progress_new.restype=ctypes.c_void_p
        lib.sentinel_progress_attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint]
        lib.sentinel_progress_attach.restype=ctypes.c_uint64
        lib.sentinel_progress_snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(Entry),ctypes.c_uint]
        lib.sentinel_progress_snapshot.restype=ctypes.c_uint
        for name in ('sentinel_progress_stop','sentinel_progress_free'):
            method=getattr(lib,name);method.argtypes=[ctypes.c_void_p];method.restype=None
        _LIBRARIES[path]=lib
    return _LIBRARIES[path]


class NativePadProgress:
    """Owned by NativeProgressTrace, whose lock serializes public calls.

    C callbacks hold their own state references and never enter Python. Only
    real Gst.Pad objects may be passed to attach, after the caller type-checks
    them. No uintptr is accepted from desired state or network input.
    """
    MAX_STAGES=64

    def __init__(self,path=None):
        self.lib=load_library(path or os.environ.get('SENTINEL_GST_PROGRESS_LIB',
                            '/opt/finalcombo/libsentinel_gst_progress.so'))
        self.pointer=self.lib.sentinel_progress_new()
        if not self.pointer:raise MemoryError('could not allocate native progress state')
        self.names=[]
        self.saved={}

    def attach(self,pad,stage):
        if not self.pointer:raise RuntimeError('native progress is closed')
        if len(self.names)>=self.MAX_STAGES:raise RuntimeError('native progress stage bound exceeded')
        if not self.lib.sentinel_progress_attach(self.pointer,hash(pad),len(self.names)):
            raise RuntimeError('could not attach native progress pad')
        self.names.append(stage)

    def snapshot(self):
        if not self.pointer:return dict(self.saved)
        entries=(Entry*self.MAX_STAGES)()
        if self.lib.sentinel_progress_snapshot(self.pointer,entries,self.MAX_STAGES)!=self.MAX_STAGES:
            raise RuntimeError('native progress snapshot failed')
        return {name:(entries[i].count,entries[i].last_ns/1e9 if entries[i].count else None,
                      entries[i].pts if entries[i].count else None)
                for i,name in enumerate(self.names)}

    def close(self):
        if not self.pointer:return
        self.lib.sentinel_progress_stop(self.pointer)
        try:self.saved=self.snapshot()
        finally:
            self.lib.sentinel_progress_free(self.pointer)
            self.pointer=None

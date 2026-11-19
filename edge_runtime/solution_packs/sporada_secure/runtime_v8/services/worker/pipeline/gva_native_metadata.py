"""Read-only native metadata adapter; activation requires separate qualification."""
import ctypes
import os
import threading

_LOCAL = threading.local()


class _Row(ctypes.Structure):
    _fields_ = [('x1',ctypes.c_int64),('y1',ctypes.c_int64),
                ('x2',ctypes.c_int64),('y2',ctypes.c_int64),
                ('track_id',ctypes.c_uint64),('confidence',ctypes.c_double),
                ('class_id',ctypes.c_int32),('reserved',ctypes.c_uint32)]


class NativeDetectionReader:
    def __init__(self,class_names):
        from gi.repository import GLib
        self.class_names = dict(class_names)
        labels = {('person' if name=='pedestrian' else name):identifier
                  for identifier,name in class_names.items()}
        self.labels = (ctypes.c_uint32*len(labels))(*(GLib.quark_from_string(name)for name in labels))
        self.classes = (ctypes.c_int32*len(labels))(*labels.values())
        self.rows = (_Row*512)()
        self.library = ctypes.CDLL(os.getenv('SENTINEL_GST_METADATA_LIBRARY',
            '/opt/finalcombo/libsentinel_gst_metadata.so'))
        lib = self.library
        lib.sentinel_detection_abi.restype = ctypes.c_uint
        lib.sentinel_detection_row_size.restype = ctypes.c_uint
        if lib.sentinel_detection_abi()!=1 or lib.sentinel_detection_row_size()!=ctypes.sizeof(_Row):
            raise RuntimeError('native detection reader ABI mismatch')
        lib.sentinel_detection_read.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_int32),ctypes.c_uint,
            ctypes.POINTER(_Row),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint)]
        lib.sentinel_detection_read.restype = ctypes.c_int

    def read(self,sample):
        from pipeline.types import Detection
        # Keep both owning objects live until C has copied all scalar fields.
        buffer = sample.get_buffer()
        if buffer is None:
            raise RuntimeError('native detection sample has no buffer')
        if not self.class_names:
            return []
        count = ctypes.c_uint()
        error = self.library.sentinel_detection_read(hash(buffer),self.labels,self.classes,
            len(self.labels),self.rows,len(self.rows),ctypes.byref(count))
        if error:
            raise RuntimeError('native detection metadata read failed: '+str(error))
        result = []
        for row in self.rows[:count.value]:
            box = [row.x1,row.y1,row.x2,row.y2]
            result.append(Detection(bbox=box,class_id=row.class_id,
                class_name=self.class_names[row.class_id],confidence=row.confidence,
                model_name='vehicle',metadata=dict(track_id=row.track_id,raw_bbox=list(box),
                    predicted=False,tracker_backend='gvatrack')))
        return result


def detections_from_sample(sample,class_names):
    # Match GstGVA's custom-type registration before iterating metadata.
    import gstgva.meta_registry  # noqa:F401
    key = tuple(class_names.items())
    readers = getattr(_LOCAL,'readers',None)
    if readers is None:
        readers = _LOCAL.readers = {}
    if key not in readers:
        readers[key] = NativeDetectionReader(class_names)
    return readers[key].read(sample)

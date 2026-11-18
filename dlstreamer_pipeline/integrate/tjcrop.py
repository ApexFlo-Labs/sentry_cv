"""Region JPEG decode with libjpeg-turbo 3 (TurboJPEG API): decode only the rows/columns of a box."""
import ctypes, numpy as np
_lib = ctypes.CDLL("/opt/intel/dlstreamer/gstreamer/lib/libturbojpeg.so.0.4.0")
class _Region(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("w", ctypes.c_int), ("h", ctypes.c_int)]
_lib.tj3Init.restype = ctypes.c_void_p; _lib.tj3Init.argtypes = [ctypes.c_int]
_lib.tj3DecompressHeader.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.tj3Get.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.tj3SetCroppingRegion.argtypes = [ctypes.c_void_p, _Region]
_lib.tj3Decompress8.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_lib.tj3GetErrorStr.restype = ctypes.c_char_p; _lib.tj3GetErrorStr.argtypes = [ctypes.c_void_p]
_lib.tj3GetErrorCode.argtypes = [ctypes.c_void_p]
TJPARAM_SUBSAMP, TJPARAM_JPEGWIDTH, TJPARAM_JPEGHEIGHT, TJPF_BGR = 4, 5, 6, 1
_MCUW = {0: 8, 1: 16, 2: 16, 3: 8, 4: 8, 5: 32, 6: 16}  # TJSAMP_444,422,420,GRAY,440,411,441 -> MCU width
_h = _lib.tj3Init(2)  # TJINIT_DECOMPRESS

def size(jpeg):
    if _lib.tj3DecompressHeader(_h, jpeg, len(jpeg)) != 0:
        raise RuntimeError(_lib.tj3GetErrorStr(_h))
    return _lib.tj3Get(_h, TJPARAM_JPEGWIDTH), _lib.tj3Get(_h, TJPARAM_JPEGHEIGHT)

def decode_box(jpeg, x1, y1, x2, y2):
    """BGR pixels of [y1:y2, x1:x2] (already clipped to the frame), decoding only what that box needs."""
    if _lib.tj3DecompressHeader(_h, jpeg, len(jpeg)) != 0:
        raise RuntimeError(_lib.tj3GetErrorStr(_h))
    W, H = _lib.tj3Get(_h, TJPARAM_JPEGWIDTH), _lib.tj3Get(_h, TJPARAM_JPEGHEIGHT)
    m = _MCUW.get(_lib.tj3Get(_h, TJPARAM_SUBSAMP), 16)
    # one extra MCU on every side so chroma upsampling at the box edges sees the same neighbours as a full decode
    ax = max(0, (x1 // m - 1) * m); ay = max(0, (y1 // 16 - 1) * 16)
    ex = min(W, -(-x2 // m) * m + m); ey = min(H, -(-y2 // 16) * 16 + 16)
    aw, ah = ex - ax, ey - ay
    if _lib.tj3SetCroppingRegion(_h, _Region(ax, ay, aw, ah)) != 0:
        raise RuntimeError(_lib.tj3GetErrorStr(_h))
    out = np.empty((ah, aw, 3), np.uint8)
    r = _lib.tj3Decompress8(_h, jpeg, len(jpeg), out.ctypes.data, aw * 3, TJPF_BGR)
    _lib.tj3SetCroppingRegion(_h, _Region(0, 0, 0, 0))
    if r != 0 and _lib.tj3GetErrorCode(_h) != 0:  # 0 = warning only
        raise RuntimeError(_lib.tj3GetErrorStr(_h))
    return np.ascontiguousarray(out[y1 - ay:y2 - ay, x1 - ax:x2 - ax])

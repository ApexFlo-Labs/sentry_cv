"""TEST: package JPEG bytes through shared memory instead of pickling them through the camera->handoff pipe.
Her V8HandoffClient / V8HandoffScheduler are unchanged; only the connection objects they are given are wrapped.
persist() sends one request and waits for the reply, and the handoff side copies the bytes out before it replies,
so one buffer per camera is never overwritten while still in use."""
import os
from multiprocessing import shared_memory

SIZE = 16 << 20


class WorkerConn:
    def __init__(self, conn, tag):
        self.c = conn
        self.shm = shared_memory.SharedMemory(create=True, size=SIZE, name=f"v8pkg_{os.getpid()}_{tag}")
        import atexit
        atexit.register(self._unlink)  # fallback if no package ever reached the handoff side
        self.stats = {"pkg_requests": 0, "pkg_parts": 0, "pkg_bytes": 0, "pkg_inline": 0}

    def send(self, req):
        parts = req.get("evidence_parts") if isinstance(req, dict) else None
        if parts:
            total = sum(len(b) for _, b in parts)
            self.stats["pkg_requests"] += 1
            self.stats["pkg_parts"] += len(parts)
            self.stats["pkg_bytes"] += total
            if total <= SIZE:
                buf, off, refs = self.shm.buf, 0, []
                for name, body in parts:
                    n = len(body)
                    buf[off:off + n] = body
                    refs.append((name, off, n))
                    off += n
                req = dict(req)
                req["evidence_parts"] = None
                req["_shm"] = (self.shm.name, refs)
            else:
                self.stats["pkg_inline"] += 1
        self.c.send(req)

    def recv(self):
        return self.c.recv()

    def _unlink(self):
        try:
            self.shm.unlink()
        except OSError:
            pass

    def close(self):
        try:
            self.c.close()
        finally:
            self._unlink()

    def __getattr__(self, k):
        return getattr(self.c, k)


class HostConn:
    def __init__(self, conn):
        self.c = conn
        self.maps = {}

    def fileno(self):
        return self.c.fileno()

    def recv(self):
        req = self.c.recv()
        if isinstance(req, dict) and req.get("_shm"):
            name, refs = req.pop("_shm")
            m = self.maps.get(name)
            if m is None:
                m = self.maps[name] = shared_memory.SharedMemory(name=name)
                try:  # remove the name now: both sides keep their mapping, nothing is left in /dev/shm after the run
                    m.unlink()
                except OSError:
                    pass
            req["evidence_parts"] = [(part, bytes(m.buf[o:o + n])) for part, o, n in refs]
        return req

    def send(self, x):
        self.c.send(x)

    def __getattr__(self, k):
        return getattr(self.c, k)


class Body(list):
    """Multipart body as its pieces: http.client sends each piece, no joined copy. len() = total bytes."""
    def __len__(self):
        return self.total


def patch_multipart(hv):
    orig = hv._multipart

    def chunks(request):
        boundary = "sentry-v8-" + request["manifest"]["package_id"].replace("-", "")
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"manifest\"\r\n"
                 "Content-Type: application/json\r\n\r\n".encode("ascii"),
                 hv.canonical_json_bytes(request["manifest"]), b"\r\n"]
        for part_name, body in request["evidence_parts"]:
            parts.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{part_name}\"; "
                          f"filename=\"{part_name}.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("ascii"),
                          body, b"\r\n"])
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        b = Body(parts)
        b.total = sum(map(memoryview.__len__, map(memoryview, parts)))
        return b, f"multipart/form-data; boundary={boundary}"
    hv._multipart = chunks
    hv._multipart_orig = orig

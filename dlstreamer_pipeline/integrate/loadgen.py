"""Artificial CPU load for the cause test (no GStreamer, no GPU). usage: loadgen.py cpu|mem|memrate|burst N SECONDS [MB_PER_S | DUTY_%]
cpu: N processes doing pure arithmetic (power/heat, almost no memory traffic)
mem: N processes copying 64 MB arrays as fast as they can (memory bandwidth)
memrate: N processes, each copying 64 MB arrays at most MB_PER_S megabytes/s (read + write counted), then sleeping.
Each process prints the rate it achieved at the end."""
import multiprocessing as mp, sys, time

def work(mode, end, rate):
    if mode == "burst":  # like the rules processes: wake, compute ~rate % of the time in 2-10 ms bursts, sleep; almost no memory
        import random
        x, duty = 0, rate / 100.0
        while time.time() < end:
            run_s = random.uniform(0.002, 0.010)
            t = time.perf_counter()
            while time.perf_counter() - t < run_s:
                for i in range(200):
                    x = (x * 1103515245 + 12345) & 0xFFFFFFFF
            time.sleep(run_s * (1 - duty) / duty * random.uniform(0.5, 1.5))
        return
    if mode == "cpu":
        x = 0
        while time.time() < end:
            for i in range(100000):
                x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        return
    import numpy as np
    chunk = 4 * 1024 * 1024 // 8 if mode == "memrate" else 64 * 1024 * 1024 // 8
    a = np.ones(64 * 1024 * 1024 // 8); b = np.empty_like(a)
    moved, start, off = 0, time.time(), 0
    per = chunk * 8 * 2  # bytes read + written per copy
    while time.time() < end:
        s = slice(off, off + chunk)
        np.copyto(b[s], a[s]); off = (off + chunk) % a.size
        moved += per
        if mode == "memrate":
            ahead = moved / (rate * 1e6) - (time.time() - start)
            if ahead > 0:
                time.sleep(ahead)
    print(f"loadgen {mode}: {moved / (time.time() - start) / 1e6:.0f} MB/s", flush=True)

if __name__ == "__main__":
    mode, n, secs = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    rate = float(sys.argv[4]) if len(sys.argv) > 4 else 0
    end = time.time() + secs
    ps = [mp.Process(target=work, args=(mode, end, rate)) for _ in range(n)]
    [p.start() for p in ps]; [p.join() for p in ps]

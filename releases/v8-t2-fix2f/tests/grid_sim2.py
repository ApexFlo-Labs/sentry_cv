"""Scene grid with pixel gating: a frame has pixels only if the pixel-due rule says so (scene-only camera in T2).
mode 2d: old pixel rule (interval since last sample) + due advanced before sampling (FIX2d)
mode 2e: grid pixel rule + due advanced only after a sample (FIX2e)."""
import random, statistics
I=2.0; n=8
def run(c, start, mode, seed):
    rnd=random.Random(seed); slot=c/n; due=None; last=0.0; out=[]; t=start
    while t<start+142:
        now=t
        if due is None:
            due=(int(now/I-slot)+1+slot)*I
            while due-I>=now: due-=I
        pixels = (now>=due) if mode=='2e' else (now-last>=I)
        if now>=due:
            if mode=='2d':
                while due<=now: due+=I
                if now-last>=I/2 and pixels: out.append(now); last=now
            else:
                if now-last>=I/2 and pixels: out.append(now); last=now
                if last==now:
                    while due<=now: due+=I
        t+=0.125+rnd.uniform(-0.01,0.03)
    return out
for mode in ('2d','2e'):
    random.seed(3); starts=[1_794_963_700+random.uniform(0,5) for _ in range(n)]
    per=[run(c,starts[c],mode,c) for c in range(n)]
    ph=[round(statistics.median((x/I)%1 for x in v),3) for v in per]
    print(mode,'samples/cam',[len(v) for v in per],'phases',ph)

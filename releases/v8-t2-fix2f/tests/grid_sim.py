import random, statistics
I=2.04; n=8; random.seed(1)
def run(c, start):
    slot=c/n; due=None; last=0; out=[]
    t=start
    while t<start+120:
        now=t
        if due is None:
            due=(int(now/I-slot)+1+slot)*I
            while due-I>=now: due-=I
        if now>=due:
            while due<=now: due+=I
            if now-last>=I/2: out.append(now); last=now
        t+=0.125+random.uniform(-0.02,0.04)
    return out
T0=1_763_430_000.0
S=[(x,c) for c in range(n) for x in run(c, T0+random.uniform(0,5))]
per={c:[x for x,cc in S if cc==c] for c in range(n)}
ph=sorted(statistics.median(((x/I)%1) for x in v) for v in per.values())
g=[b-a for a,b in zip(ph,ph[1:])]+[1-ph[-1]+ph[0]]
print('samples/cam',[len(v) for v in per.values()],'interval med',round(statistics.median([b-a for v in per.values() for a,b in zip(v,v[1:])]),3))
print('phases',[round(p,3) for p in ph],'gap min',round(min(g),3),'max',round(max(g),3))

"""GDB read-only pool snapshot for the verified 2026.2.0 amd64 binary only.

Offsets come from deployed-va-pool-disassembly-host.txt. No inferior function
calls/writes. Not portable across builds; evidence must include binary SHA256.
"""
import gdb,json
for thread in gdb.selected_inferior().threads():
 thread.switch();frame=gdb.newest_frame()
 while frame:
  if 'VaApiImagePool::AcquireBuffer' in (frame.name()or''):
   frame.select()
   try:
    pool=int(frame.read_register('rbp'))
    inferior=gdb.selected_inferior()
    word=lambda at:int.from_bytes(bytes(inferior.read_memory(at,8)),'little')
    start,end=word(pool),word(pool+8);count=(end-start)//8
    if not 1<=count<=1024 or (end-start)%8:raise ValueError('pool layout not recognized')
    images=[word(start+8*i)for i in range(count)]
    flags=[int.from_bytes(bytes(inferior.read_memory(at+0x98,1)),'little')for at in images]
    print('SENTINEL_READ_ONLY_VA_POOL '+json.dumps({'thread':thread.num,'pool':hex(pool),'count':count,'completed':flags,'available_while_acquire_waits':sum(v==1 for v in flags),'layout_basis':'verified image intel-dlstreamer2026.2.0 amd64 disassembly; no memory writes'}))
   except Exception as exc:print('SENTINEL_POOL_SNAPSHOT_UNAVAILABLE',str(exc))
   break
  frame=frame.older()

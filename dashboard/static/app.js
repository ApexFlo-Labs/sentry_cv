const $=id=>document.getElementById(id),fmt=b=>b?`${(b/1073741824).toFixed(1)} GB`:'—';
const pct=(v,max=100)=>`${Math.max(0,Math.min(100,(v||0)/max*100))}%`;
async function json(url,opts){const r=await fetch(url,opts),d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}

function buildVideoRows(videos,state){
  const selected=new Map((state?.selections||[]).map(x=>[x.video,x.copies]));
  const host=$('videoRows');host.replaceChildren();
  for(const video of videos){
    const row=document.createElement('div');row.className='video-row';row.dataset.video=video.name;
    const check=document.createElement('input');check.type='checkbox';check.className='video-check';
    const name=document.createElement('span');name.className='video-name';name.textContent=`${video.name} · ${(video.size/1048576).toFixed(1)} MB`;name.title=video.name;
    const copies=document.createElement('input');copies.type='number';copies.className='video-copies';copies.min='1';copies.max='32';copies.value=selected.get(video.name)||1;
    check.checked=selected.has(video.name);copies.disabled=!check.checked;row.classList.toggle('selected',check.checked);
    check.addEventListener('change',()=>{copies.disabled=!check.checked;row.classList.toggle('selected',check.checked)});
    row.append(check,name,copies);host.append(row);
  }
}

function selections(){return [...document.querySelectorAll('.video-row')].filter(r=>r.querySelector('.video-check').checked).map(r=>({video:r.dataset.video,copies:Number(r.querySelector('.video-copies').value)}))}
function selectEvery(value){document.querySelectorAll('.video-row').forEach(r=>{const c=r.querySelector('.video-check'),n=r.querySelector('.video-copies');c.checked=value;n.disabled=!value;r.classList.toggle('selected',value)})}

function render(s){
  $('serviceBadge').className=`badge ${s.service.active?'ok':''}`;$('serviceBadge').innerHTML=`<span></span>${s.service.state}`;$('updated').textContent=new Date(s.timestamp*1000).toLocaleTimeString();
  $('ram').textContent=`${s.memory.percent}%`;$('ramBar').style.width=pct(s.memory.percent);$('ramDetail').textContent=`${fmt(s.memory.used)} / ${fmt(s.memory.total)}`;
  const c=s.cpu||{};
  $('cpu').textContent=c.system_percent==null?'sampling…':`${c.system_percent}%`;$('cpuBar').style.width=pct(c.system_percent);$('cpuDetail').textContent=`${c.logical_cpus||'—'} logical CPUs`;
  $('appCpu').textContent=c.sentry_percent==null?'sampling…':`${c.sentry_percent}%`;$('appCpuBar').style.width=pct(c.sentry_percent);$('appCpuDetail').textContent=c.sentry_cores==null?`PID ${s.service.pid} · ${fmt(s.service.rss)}`:`${c.sentry_cores.toFixed(2)} cores · inference ${(c.inference_cores||0).toFixed(2)} + ingest ${(c.ingest_cores||0).toFixed(2)}`;
  const g=s.gpu.compute_percent||0,m=s.gpu.decode_percent||0,n=s.npu.utilization_percent||0;$('gpu').textContent=`${g.toFixed(1)}%`;$('gpuBar').style.width=pct(g);$('media').textContent=`${m.toFixed(1)}%`;$('mediaBar').style.width=pct(m);$('npu').textContent=s.npu.available?`${n.toFixed(1)}%`:'unavailable';$('npuBar').style.width=pct(n);
  const rate=s.pipeline.results_per_second||0,smokeRate=s.pipeline.smoke_fire_detections_per_second||0;$('rate').textContent=`${rate.toFixed?.(1)||rate}/s`;$('rateBar').style.width=pct(rate,50);$('resultCount').textContent=`${s.pipeline.generated_results||0} frame inferences · smoke/fire ${smokeRate.toFixed?.(1)||smokeRate}/s (${s.pipeline.smoke_fire_detections||0} total)`;
  const secondary=s.pipeline.secondary||{},depth=secondary.queue_depth||0,capacity=secondary.queue_capacity||0;
  $('secondaryQueue').textContent=secondary.enabled?`${depth}/${capacity}`:'disabled';$('secondaryQueueBar').style.width=pct(depth,capacity||1);$('secondaryDetail').textContent=secondary.enabled?`plate ${secondary.plates_per_second||0}/s · ANPR ${secondary.anpr_per_second||0}/s · face ${secondary.faces_per_second||0}/s · reid ${secondary.body_reid_per_second||0}/s · gait ${secondary.gait_per_second||0}/s · scene ${secondary.scene_per_second||0}/s · NV12→BGR ${secondary.average_frame_conversion_ms||0} ms · secondary ${secondary.average_processing_ms||0} ms · dropped ${secondary.dropped||0}`:'No secondary inference';
  $('streamCount').textContent=`${s.streams.publishers}/${s.streams.configured} live`;
  const mapped=s.streams.selection?.streams||s.streams.urls.map((url,i)=>({url,video:`Camera ${i+1}`,copy:1}));
  $('streams').innerHTML='';mapped.forEach((x,i)=>{const row=document.createElement('div');row.className='stream';const id=document.createElement('span');id.textContent=`CAM ${String(i+1).padStart(2,'0')}`;const url=document.createElement('span');url.textContent=x.url;const source=document.createElement('small');source.textContent=`${x.video} · copy ${x.copy}`;row.append(id,url,source);$('streams').append(row)});
  $('placement').innerHTML=s.placement.map(x=>`<div class="stage"><span>${x.stage}</span><span>${x.device}</span><span>${x.rate}</span></div>`).join('');$('restarts').textContent=s.service.restarts;$('disconnects').textContent=s.health.disconnects_10m;$('errors').textContent=s.health.errors_10m;$('warning').textContent=s.health.last_warning||'No warnings reported.';
}

async function refresh(){try{render(await json('/api/status'))}catch(e){$('serviceBadge').innerHTML='<span></span>Dashboard API error'}}
async function initialize(){const [videos,status]=await Promise.all([json('/api/videos'),json('/api/status')]);$('port').value=status.streams.selection?.port||8654;buildVideoRows(videos,status.streams.selection);render(status)}
$('selectAll').addEventListener('click',()=>selectEvery(true));$('deselectAll').addEventListener('click',()=>selectEvery(false));
$('generate').addEventListener('click',async()=>{const b=$('generate'),msg=$('actionMessage'),port=Number($('port').value),chosen=selections();msg.style.color='var(--green)';msg.hidden=false;
  if(port<1024||port>65535){msg.style.color='var(--red)';msg.textContent='Port must be between 1024 and 65535.';return}if(!chosen.length){msg.style.color='var(--red)';msg.textContent='Select at least one video.';return}
  b.disabled=true;b.textContent='Validating port & generating…';msg.textContent=`Checking port ${port}, replacing publishers, and waiting for ${chosen.reduce((a,x)=>a+x.copies,0)} streams…`;
  try{const d=await json('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({port,selections:chosen})});msg.textContent=`Ready: ${d.stream_count}/${d.stream_count} streams on port ${d.port}.`;await refresh()}catch(e){msg.style.color='var(--red)';msg.textContent=`Generation failed: ${e.message}`}finally{b.disabled=false;b.textContent='Stop previous & generate'}});
initialize().catch(()=>{$('serviceBadge').innerHTML='<span></span>Dashboard API error'});setInterval(refresh,2000);

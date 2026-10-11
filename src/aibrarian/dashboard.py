"""Single-file status dashboard served at /dashboard (no build step, no deps)."""

DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>aibrarian status</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font:14px system-ui,sans-serif;margin:1.5rem;background:#111;color:#ddd}
 h1{font-size:1.3rem} h2{font-size:1.05rem;margin-top:1.6rem}
 .cards{display:flex;flex-wrap:wrap;gap:.6rem}
 .card{background:#1d1d1d;border-radius:8px;padding:.7rem 1rem;min-width:110px;cursor:pointer}
 .card b{display:block;font-size:1.5rem} .failed b{color:#f66} .needs_review b{color:#fc6}
 .cataloged b{color:#6c6} table{border-collapse:collapse;width:100%}
 td,th{border-bottom:1px solid #333;padding:.3rem .5rem;text-align:left;vertical-align:top}
 tr.click{cursor:pointer} tr.click:hover{background:#222} code{color:#9cf}
 .muted{color:#888} pre{white-space:pre-wrap;margin:0}
</style></head><body>
<h1>aibrarian &middot; media pipeline</h1>
<div id="summary" class="muted">loading&hellip;</div>
<div class="cards" id="stages"></div>
<h2 id="listTitle">Failures</h2><table id="list"></table>
<h2>Timeline <span id="tlTitle" class="muted"></span></h2><table id="timeline"></table>
<h2>Recent events</h2><table id="events"></table>
<script>
const ORDER=["discovered","probed","audio_extracted","transcribed","embedded","cataloged",
             "needs_review","skipped","failed"];
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const j=u=>fetch(u).then(r=>r.json());
function fmt(s){if(s==null)return"unknown";s=Math.round(s);const d=Math.floor(s/86400),
 h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+"d ":"")+h+"h "+m+"m";}
async function loadStatus(){
 const st=await j("status");
 document.getElementById("summary").textContent=
  `backlog ${st.backlog} items · ETA ${fmt(st.eta.eta_s)} (median ${st.eta.median_gap_s??"?"}s/item)`;
 document.getElementById("stages").innerHTML=ORDER.map(s=>
  `<div class="card ${s}" onclick="loadStage('${s}')"><b>${st.stages[s]||0}</b>${s}</div>`).join("");
}
async function loadStage(s){
 document.getElementById("listTitle").textContent="Stage: "+s;
 const rows=await j("status/stages/"+s+"?limit=200");
 document.getElementById("list").innerHTML="<tr><th>id</th><th>title</th><th>attempts</th>"+
  "<th>failed at</th><th>error</th><th>since</th></tr>"+rows.map(r=>
  `<tr class="click" onclick="loadTimeline(${r.id})"><td>${r.id}</td><td>${esc(r.title||r.path)}</td>`+
  `<td>${r.stage_attempts}</td><td>${esc(r.failed_stage)}</td><td><pre>${esc(r.last_error)}</pre></td>`+
  `<td>${esc(r.stage_changed_at)}</td></tr>`).join("");
}
async function loadTimeline(id){
 const t=await j("media/"+id+"/timeline");
 document.getElementById("tlTitle").textContent="#"+id+" "+(t.media.title||t.media.path);
 document.getElementById("timeline").innerHTML="<tr><th>when</th><th>action</th><th>from → to</th>"+
  "<th>outcome</th><th>try</th><th>ms</th><th>error / details</th><th>prefect run</th></tr>"+
  t.events.map(e=>`<tr><td>${esc(e.finished_at)}</td><td>${e.action}</td>`+
  `<td>${esc(e.from_stage)} → ${e.to_stage}</td><td>${e.outcome}</td><td>${e.attempt}</td>`+
  `<td>${e.duration_ms??""}</td><td><pre>${esc(e.error||JSON.stringify(e.details))}</pre></td>`+
  `<td><code>${esc(e.flow_run_id)}</code></td></tr>`).join("");
}
async function loadEvents(){
 const ev=await j("status/events?limit=30");
 document.getElementById("events").innerHTML=ev.map(e=>
  `<tr class="click" onclick="loadTimeline(${e.media_id})"><td>${esc(e.finished_at)}</td>`+
  `<td>${e.action}</td><td>${e.outcome}</td><td>${esc(e.path)}</td></tr>`).join("");
}
function tick(){loadStatus();loadEvents();}
tick();loadStage("failed");setInterval(tick,15000);
</script></body></html>
"""

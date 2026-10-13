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
<p><a href="search/ui">Quote search</a> · <a href="duplicates/ui">Duplicates report</a> · <a href="dedup/ui">Dedup actions</a> · <a href="library-log/ui">Library log (books)</a></p>
<p id="calval" class="muted"></p>
<h2>Audio layouts</h2><div class="cards" id="layouts"></div>
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
async function loadCalibre(){try{const v=await j("library-log/validation");if(!v.id)return;
 const ok=(v.details||{}).ok;document.getElementById("calval").innerHTML=
 `Calibre validation ${esc(String(v.ts).slice(0,16))}: <b style="color:${ok?"#6c6":"#f66"}">${ok?"OK":"ISSUES"}</b> - ${esc(v.reason)} (<a href="library-log/ui?action=validate">details</a>)`;}catch(e){}}
loadCalibre();
async function loadStatus(){
 const st=await j("status");
 document.getElementById("summary").textContent=
  `backlog ${st.backlog} items · ETA ${fmt(st.eta.eta_s)} (median ${st.eta.median_gap_s??"?"}s/item)`;
 document.getElementById("stages").innerHTML=ORDER.map(s=>
  `<div class="card ${s}" onclick="loadStage('${s}')"><b>${st.stages[s]||0}</b>${s}</div>`).join("");
 document.getElementById("layouts").innerHTML=Object.entries(st.audio_layouts||{}).map(([l,n])=>
  `<div class="card" onclick="loadLayout('${esc(l)}')"><b>${n}</b>${esc(l)}</div>`).join("");
}
async function loadLayout(l){
 document.getElementById("listTitle").textContent="Audio layout: "+l;
 const rows=l==="unknown"?[]:await j("media?audio_layout="+encodeURIComponent(l)+"&limit=200");
 document.getElementById("list").innerHTML="<tr><th>id</th><th>title</th><th>stage</th><th>layout</th><th>lang</th></tr>"+
  rows.map(r=>`<tr class="click" onclick="loadTimeline(${r.id})"><td>${r.id}</td><td>${esc(r.title||r.path)}</td>`+
  `<td>${r.stage}</td><td>${esc(r.audio_layout)}</td><td>${esc(r.audio_language)}</td></tr>`).join("");
}
async function loadStage(s){
 document.getElementById("listTitle").textContent="Stage: "+s;
 const rows=await j("status/stages/"+s+"?limit=200");
 document.getElementById("list").innerHTML="<tr><th>id</th><th>title</th><th>attempts</th>"+
  "<th>failed at</th><th>error</th><th>since</th><th>audio</th></tr>"+rows.map(r=>
  `<tr class="click" onclick="loadTimeline(${r.id})"><td>${r.id}</td><td>${esc(r.title||r.path)}</td>`+
  `<td>${r.stage_attempts}</td><td>${esc(r.failed_stage)}</td><td><pre>${esc(r.last_error)}</pre></td>`+
  `<td>${esc(r.stage_changed_at)}</td><td>${esc(r.audio_layout)} ${esc(r.audio_language)}</td></tr>`).join("");
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


SEARCH_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>aibrarian search</title><style>
body{font-family:system-ui,sans-serif;margin:1.5rem;max-width:1000px}
input,select,button{font-size:1rem;padding:.35rem}#title{width:22rem}#q{width:28rem}
table{border-collapse:collapse;width:100%;margin-top:1rem}td,th{border-bottom:1px solid #ddd;padding:.3rem;text-align:left}
.msg{margin-top:.6rem;color:#444}.err{color:#b00}.ok{color:#070}
</style></head><body>
<h1>Quote search</h1><p><a href="../dashboard">dashboard</a></p>
<form id="f">
<input id="title" list="titles" placeholder="Movie (optional) - type to pick" autocomplete="off">
<datalist id="titles"></datalist>
<input id="q" placeholder="Quote" required minlength="2">
<select id="mode"><option>hybrid</option><option>keyword</option><option>semantic</option></select>
<button>Search</button></form>
<div id="msg" class="msg"></div>
<table id="res"></table>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ts=s=>{s=Math.max(0,s|0);return [s/3600|0,(s%3600)/60|0,s%60].map(n=>String(n).padStart(2,"0")).join(":")};
let picks={},timer;
const T=document.getElementById("title"),M=document.getElementById("msg");
T.addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(async()=>{
 const r=await fetch("../titles/autocomplete?limit=15&q="+encodeURIComponent(T.value));
 const rows=await r.json();picks={};
 document.getElementById("titles").innerHTML=rows.map(x=>{const l=x.label||(x.year?`${x.title} (${x.year})`:x.title);
  picks[l]=x.media_id;return `<option value="${esc(l)}">`}).join("");},150)});
function err(d){const s=(d.detail&&d.detail.suggestions)||[];
 M.className="msg err";M.innerHTML=esc((d.detail&&d.detail.error)||JSON.stringify(d.detail))+
 (s.length?"<br>Did you mean: "+s.map(x=>`<a href="#" data-t="${esc(x.year?x.title+" ("+x.year+")":x.title)}">${esc(x.title)}${x.year?" ("+x.year+")":""}</a>`).join(", "):"");
 M.querySelectorAll("a[data-t]").forEach(a=>a.onclick=e=>{e.preventDefault();T.value=a.dataset.t;document.getElementById("f").requestSubmit()});}
document.getElementById("f").onsubmit=async e=>{e.preventDefault();
 const q=document.getElementById("q").value,p=new URLSearchParams({q,mode:document.getElementById("mode").value,limit:"20"});
 const t=T.value.trim();if(picks[t])p.set("movie_id",picks[t]);else if(t)p.set("title",t);
 M.className="msg";M.textContent="searching...";
 const r=await fetch("../search?"+p);const d=await r.json();
 if(!r.ok){err(d);document.getElementById("res").innerHTML="";return}
 const rt=r.headers.get("X-Resolved-Title");
 M.textContent=(rt?"Movie: "+decodeURIComponent(rt)+" - ":"")+d.length+" hits";
 document.getElementById("res").innerHTML="<tr><th>movie</th><th>time</th><th>text</th><th>score</th><th></th></tr>"+
  d.map((h,i)=>`<tr><td>${esc(h.title)}</td><td>${ts(h.start)}-${ts(h.end)}</td><td>${esc(h.text)}</td>`+
  `<td>${(+h.score).toFixed(3)}</td><td><button data-i="${i}">Make clip</button></td></tr>`).join("");
 document.querySelectorAll("#res button").forEach(b=>b.onclick=async()=>{const h=d[b.dataset.i];
  b.disabled=true;b.textContent="cutting...";
  const r=await fetch("../clip",{method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({phrase:h.text,movie_id:h.media_id})});const c=await r.json();
  if(r.ok){b.textContent="done";M.className="msg ok";M.textContent=`Clip ${ts(c.start)}-${ts(c.end)}: ${c.output}`}
  else{b.disabled=false;b.textContent="Make clip";err(c)}});};
</script></body></html>"""


DUPLICATES_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>aibrarian duplicates</title><style>
body{font-family:system-ui,sans-serif;margin:1.5rem}
.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{border:1px solid #ccc;border-radius:6px;padding:.6rem 1rem}
.card b{display:block;font-size:1.4rem}table{border-collapse:collapse;width:100%;margin:.4rem 0 1.2rem}
td,th{border-bottom:1px solid #ddd;padding:.25rem .4rem;text-align:left;font-size:.9rem}
tr.keep{background:#eaffea}.grp{margin-top:1rem;font-weight:600}.warn{color:#a60}
</style></head><body>
<h1>Duplicates (read-only)</h1>
<p><a href="../dashboard">dashboard</a> · <a href="../dedup/ui">dedup actions log</a> · Nothing is deleted or moved. "keep" is a suggestion only.
<button onclick="load(false)">Re-scan now</button> <label><input type="checkbox" id="exact"> identical only</label></p>
<div class="cards" id="sum">loading...</div><div id="qs"></div><div id="qp"></div><div id="out"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const gb=b=>(b/1e9).toFixed(2)+" GB";
function section(title,groups){
 if(!groups.length)return `<h2>${title}</h2><p>none</p>`;
 return `<h2>${title}</h2>`+groups.map((g,i)=>`<div class="grp">#${i+1} ${esc(g.reason)} - keep: ${esc(g.suggested_keep)}
 (${esc(g.keep_reason)}) - frees ${gb(g.reclaimable_bytes)}</div>`+
 (g.sidecars_only_on_others.length?`<div class="warn">Sidecars only on other copies: `+
   g.sidecars_only_on_others.map(o=>esc(o.sidecar)).join(", ")+`</div>`:"")+
 `<table><tr><th></th><th>path</th><th>size</th><th>container</th><th>video</th><th>res</th><th>HDR</th>`+
 `<th>audio</th><th>#aud</th><th>#subs</th><th>sidecars</th><th>stage</th></tr>`+
 g.copies.map(c=>`<tr class="${c.path===g.suggested_keep?"keep":""}"><td>${c.path===g.suggested_keep?"KEEP":""}</td>`+
  `<td>${esc(c.path)}</td><td>${gb(c.size_bytes)}</td><td>${esc(c.container)}</td><td>${esc(c.video_codec)}</td>`+
  `<td>${c.width?c.width+"x"+c.height:""}</td><td>${esc(c.hdr)}</td><td>${esc(c.audio_layout)}</td>`+
  `<td>${c.audio_streams}</td><td>${c.subtitle_streams}</td><td>${esc(c.sidecars.join(" "))}</td><td>${esc(c.stage)}</td></tr>`).join("")+
 `</table>`).join("");}
async function load(cached){
 document.getElementById("sum").textContent="scanning (first run hashes candidate files)...";
 const p=new URLSearchParams({cached:String(cached),exact_only:String(document.getElementById("exact").checked)});
 const r=await (await fetch("../duplicates?"+p)).json(),s=r.summary;
 document.getElementById("sum").innerHTML=[["identical groups",s.identical_groups],["identical savings",gb(s.identical_reclaimable_bytes)],
  ["review groups",s.review_groups],["review (if all extra copies removed)",gb(s.review_reclaimable_bytes)],["files",s.files_scanned]]
  .map(([k,v])=>`<div class="card"><b>${v}</b>${k}</div>`).join("");
 document.getElementById("out").innerHTML=section("Identical (byte-for-byte)",r.identical)+section("Likely duplicates - review",r.review)+
  (r.identical_books?section("Identical books (group numbers start at 10001)",r.identical_books):"");
 try{const z=await (await fetch("../duplicates/quarantine")).json();
 document.getElementById("qs").innerHTML=`<h2>In quarantine (read-only)</h2><p>${z.files} files, ${z.total_gb} GB in <code>${esc(z.root)}</code>.
 Retention ${z.retention_days} days; next purge run ${esc(z.next_purge_run)} (deployment quarantine-purge).</p>`+
 (z.items.length?`<table><tr><th>original path</th><th>size</th><th>moved</th><th>age (days)</th><th>purged on</th></tr>`+
 z.items.map(i=>`<tr><td>${esc(i.path)}</td><td>${gb(i.size_bytes)}</td><td>${esc(i.moved_at)}</td><td>${i.age_days}</td><td>${esc(i.purge_on)}</td></tr>`).join("")+`</table>`:"");}catch(e){}
 const q=await (await fetch("../duplicates/quarantine-plan?cached=true")).json();
 document.getElementById("qp").innerHTML=`<h2>Quarantine preview (dry run, read-only)</h2><p>${q.files} extra copies in ${q.groups} identical groups, ${q.total_gb} GB would move to <code>${esc(q.batch_dir)}</code>. Run <code>aibrarian duplicates quarantine --limit 3</code> on the server to review, then add <code>--execute</code>. This page never moves files.</p>`;}
load(true);
</script></body></html>"""


DEDUP_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>aibrarian dedup actions</title><style>
body{font-family:system-ui,sans-serif;margin:1.5rem}
.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{border:1px solid #ccc;border-radius:6px;padding:.6rem 1rem}
.card b{display:block;font-size:1.4rem}table{border-collapse:collapse;width:100%;margin:.6rem 0}
td,th{border-bottom:1px solid #ddd;padding:.25rem .4rem;text-align:left;font-size:.85rem;vertical-align:top}
.a-quarantine{color:#06c}.a-purge{color:#a00}.a-restore{color:#080}.a-skip{color:#a60}.a-error{color:#d00;font-weight:600}
form>*{margin-right:.5rem}
</style></head><body>
<h1>Dedup actions (read-only)</h1>
<p><a href="../dashboard">dashboard</a> · <a href="../duplicates/ui">duplicates report</a> ·
<a href="../library-log/ui">library log (books)</a> ·
This page only shows the log; quarantine/restore/purge run from the CLI or Prefect.</p>
<div class="cards" id="sum">loading...</div>
<form id="f" onsubmit="go(0);return false">
<select id="action"><option value="">all actions</option><option>quarantine</option><option>skip</option>
<option>restore</option><option>purge</option><option>error</option></select>
from <input type="date" id="since"> to <input type="date" id="until">
<input id="q" placeholder="title / path contains"> <button>Filter</button></form>
<div id="out"></div><p id="pg"></p>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const gb=b=>b==null?"":(b/1e9).toFixed(2)+" GB";const N=50;
function nextDay(d){if(!d)return "";const t=new Date(d+"T00:00:00Z");t.setUTCDate(t.getUTCDate()+1);return t.toISOString().slice(0,10);}
async function go(off){
 const v=id=>document.getElementById(id).value;
 const p=new URLSearchParams({limit:N,offset:off});
 if(v("action"))p.set("action",v("action"));if(v("since"))p.set("since",v("since"));
 if(v("until"))p.set("until",nextDay(v("until")));if(v("q"))p.set("q",v("q"));
 const r=await (await fetch("../dedup/actions?"+p)).json(),s=r.summary;
 document.getElementById("sum").innerHTML=[["in quarantine now",s.quarantined_files+" files"],["quarantined size",s.quarantined_gb+" GB"],
  ["purged to date",s.purged_files+" files"],["purged size",s.purged_gb+" GB"],["next purge run (UTC)",esc(s.next_purge_run).slice(0,16)],
  ["retention",s.retention_days+" days"]].map(([k,x])=>`<div class="card"><b>${x}</b>${k}</div>`).join("");
 document.getElementById("out").innerHTML=r.actions.length?`<table><tr><th>time (UTC)</th><th>action</th><th>group</th><th>file</th><th>keeper</th>`+
  `<th>quarantine path</th><th>size</th><th>sha256</th><th>sidecars copied</th><th>reason</th><th>source</th></tr>`+
  r.actions.map(a=>`<tr><td>${esc(a.ts).slice(0,19)}</td><td class="a-${esc(a.action)}">${esc(a.action)}</td><td>${esc(a.group_no)}</td>`+
  `<td>${esc(a.file_path)}</td><td>${esc(a.keeper_path)}</td><td>${esc(a.quarantine_path)}</td><td>${gb(a.size_bytes)}</td>`+
  `<td title="${esc(a.sha256)}">${esc((a.sha256||"").slice(0,12))}</td><td>${esc((a.sidecars_copied||[]).join(", "))}</td>`+
  `<td>${esc(a.reason)}</td><td>${esc(a.source)}</td></tr>`).join("")+`</table>`:"<p>no actions</p>";
 const pg=document.getElementById("pg");pg.innerHTML=`${r.total?off+1:0}-${off+r.actions.length} of ${r.total} `;
 if(off>0){const b=document.createElement("a");b.href="#";b.textContent="< newer";b.onclick=e=>{e.preventDefault();go(Math.max(0,off-N))};pg.append(b," ");}
 if(off+N<r.total){const b=document.createElement("a");b.href="#";b.textContent="older >";b.onclick=e=>{e.preventDefault();go(off+N)};pg.append(b);}}
go(0);
</script></body></html>"""


LIBRARY_LOG_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>aibrarian library log</title><style>
body{font-family:system-ui,sans-serif;margin:1.5rem}
.cards{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:.6rem}.card{border:1px solid #ccc;border-radius:6px;padding:.5rem .9rem}
.card b{display:block;font-size:1.3rem}h3{margin:.6rem 0 .3rem}
table{border-collapse:collapse;width:100%;margin:.6rem 0}
td,th{border-bottom:1px solid #ddd;padding:.25rem .4rem;text-align:left;font-size:.85rem;vertical-align:top}
.a-move,.a-rename{color:#06c}.a-quarantine{color:#a0a}.a-needs_review{color:#a60}.a-error{color:#d00;font-weight:600}
.a-undo{color:#080}.a-skip{color:#777}form>*{margin:.15rem .3rem .15rem 0}code{background:#f4f4f4;padding:0 .2rem}
</style></head><body>
<h1>Library log (read-only)</h1>
<p><a href="../dashboard">dashboard</a> · <a href="../dedup/ui">dedup actions</a> ·
Automatic book renames/moves/quarantines. Nothing here changes files; "undo" shows the CLI command.</p>
<h3>Today</h3><div class="cards" id="today"></div><h3>All time</h3><div class="cards" id="total"></div>
<form id="f" onsubmit="go(0);return false">
<input type="date" id="day" title="single day"> or <input type="date" id="since"> to <input type="date" id="until">
<input id="title" list="l-title" placeholder="book title"><datalist id="l-title"></datalist>
<input id="author" list="l-author" placeholder="author"><datalist id="l-author"></datalist>
<input id="series" list="l-series" placeholder="series"><datalist id="l-series"></datalist>
<select id="action"><option value="">all actions</option><option>move</option><option>rename</option>
<option>quarantine</option><option>needs_review</option><option>skip</option><option>error</option>
<option>undo</option><option>folder_removed</option><option>calibre_add</option>
<option>add_format</option><option>copy</option><option>validate</option></select>
<select id="domain"><option value="">all domains</option><option>book</option><option>video</option></select>
<input id="run" placeholder="run id"> <button>Filter</button></form>
<div id="val"></div><p id="undo"></p><div id="out"></div><p id="pg"></p>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const N=50,$=id=>document.getElementById(id);
const nextDay=d=>{const t=new Date(d+"T00:00:00Z");t.setUTCDate(t.getUTCDate()+1);return t.toISOString().slice(0,10);};
const cards=o=>Object.keys(o).length?Object.entries(o).map(([k,v])=>`<div class="card"><b>${v}</b>${esc(k)}</div>`).join(""):"<i>nothing</i>";
for(const f of ["title","author","series"]){$(f).addEventListener("input",async()=>{
 const r=await (await fetch("../library-log/facets?"+new URLSearchParams({field:f,q:$(f).value}))).json();
 $("l-"+f).innerHTML=r.map(v=>`<option value="${esc(v)}">`).join("");});}
function showUndo(cmd){$("undo").innerHTML=`Run on the server (dry run first, then add --execute): <code>${esc(cmd)}</code>`;}
async function go(off){
 const p=new URLSearchParams({limit:N,offset:off});
 if($("day").value){p.set("since",$("day").value);p.set("until",nextDay($("day").value));}
 else{if($("since").value)p.set("since",$("since").value);if($("until").value)p.set("until",nextDay($("until").value));}
 for(const k of ["title","author","series","action","domain"])if($(k).value)p.set(k,$(k).value);
 if($("run").value)p.set("run_id",$("run").value);
 const r=await (await fetch("../library-log?"+p)).json();
 const v=r.summary.validation;
 if(v&&v.id){const d=v.details||{},iss=d.issues||{};$("val").innerHTML=`<h3>Latest Calibre validation: <span style="color:${d.ok?"#080":"#c00"}">${d.ok?"OK":"issues"}</span></h3>`+
  `<p>${esc(String(v.ts).slice(0,19))} - ${esc(v.reason)}</p>`+Object.entries(iss).filter(([k,x])=>x.length).map(([k,x])=>
  `<details><summary>${esc(k)} (${x.length})</summary><pre>${esc(JSON.stringify(x.slice(0,200),null,1))}</pre></details>`).join("");}
 $("today").innerHTML=cards(r.summary.today);$("total").innerHTML=cards(r.summary.total)+
  `<div class="card"><b>${r.summary.threshold}</b>auto threshold${r.summary.auto_enabled?"":" (auto OFF)"}</div>`;
 $("out").innerHTML=r.actions.length?`<table><tr><th>time (UTC)</th><th>action</th><th>title</th><th>author</th><th>series</th>`+
  `<th>conf.</th><th>from</th><th>to</th><th>reason</th><th>run</th><th>undo</th></tr>`+r.actions.map(a=>{
  const can=["move","rename","copy","quarantine","calibre_add","add_format"].includes(a.action)&&!a.undone_by;
  const det=a.details&&Object.keys(a.details).length?`<details><summary>details</summary><pre>${esc(JSON.stringify(a.details,null,1))}</pre></details>`:"";
  return `<tr><td>${esc(a.ts).slice(0,19)}</td><td class="a-${esc(a.action)}">${esc(a.action)}${a.undone_by?" (undone)":""}</td>`+
  `<td>${esc(a.title)}</td><td>${esc(a.author)}</td><td>${esc(a.series)}${a.series_index?" #"+esc(a.series_index):""}</td>`+
  `<td>${a.confidence==null?"":Number(a.confidence).toFixed(2)}</td><td>${esc(a.old_path)}</td><td>${esc(a.new_path)}</td>`+
  `<td>${esc(a.reason)}${det}</td><td><a href="#" data-run="${esc(a.run_id)}">${esc(a.run_id)}</a></td>`+
  `<td>${can?`<a href="#" data-undo="${a.id}">show command</a>`:""}</td></tr>`}).join("")+`</table>`:"<p>no actions</p>";
 $("out").querySelectorAll("[data-undo]").forEach(x=>x.onclick=e=>{e.preventDefault();showUndo(`docker compose exec aibrarian-api aibrarian books undo --id ${x.dataset.undo}`);});
 $("out").querySelectorAll("[data-run]").forEach(x=>x.onclick=e=>{e.preventDefault();$("run").value=x.dataset.run;
  showUndo(`docker compose exec aibrarian-api aibrarian books undo --run ${x.dataset.run}`);go(0);});
 const pg=$("pg");pg.innerHTML=`${r.total?off+1:0}-${off+r.actions.length} of ${r.total} `;
 if(off>0){const b=document.createElement("a");b.href="#";b.textContent="< newer";b.onclick=e=>{e.preventDefault();go(Math.max(0,off-N))};pg.append(b," ");}
 if(off+N<r.total){const b=document.createElement("a");b.href="#";b.textContent="older >";b.onclick=e=>{e.preventDefault();go(off+N)};pg.append(b);}}
for(const [k,v] of new URLSearchParams(location.search))if($(k))$(k).value=v;
go(0);
</script></body></html>"""


EMBED_EVAL_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>aibrarian embedding A/B</title>
<style>body{font-family:system-ui,sans-serif;margin:1.5em;max-width:1400px}
table{border-collapse:collapse;margin:.5em 0}td,th{border:1px solid #ccc;padding:3px 7px;
font-size:13px;vertical-align:top}th{background:#f3f3f3}.cols{display:flex;gap:1em}
.cols>div{flex:1;min-width:0}small{color:#666}input{width:40em;padding:4px}</style></head>
<body><h2>Embedding A/B (read-only)</h2>
<p><small>Eval collections only (<code>eval_*</code>); production search is untouched.
Build/run with <code>aibrarian embed-eval</code>.</small></p>
<form id="f"><input id="q" placeholder="type a quote or 'scene where ...'">
<select id="ix"><option>windows</option><option>segments</option></select>
<button>Search both</button></form><div id="res" class="cols"></div>
<h3>Latest report</h3><div id="rep">loading…</div>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const t=s=>s==null?'':new Date(s*1000).toISOString().substr(11,8);
function md(m){ // tiny markdown: tables + headings + paragraphs
  let h='',rows=[];const flush=()=>{if(rows.length){h+='<table>'+rows.map((r,i)=>
   '<tr>'+r.map(c=>i?'<td>'+esc(c)+'</td>':'<th>'+esc(c)+'</th>').join('')+'</tr>').join('')
   +'</table>';rows=[]}};
  for(const l of m.split('\n')){if(l.startsWith('|')){if(!/^\\|[-| ]+\\|$/.test(l))
   rows.push(l.slice(1,-1).split('|').map(x=>x.trim().replace(/`/g,'')));continue}
   flush();if(l.startsWith('# '))h+='<h4>'+esc(l.slice(2))+'</h4>';else if(l)h+='<p>'+esc(l)+'</p>'}
  flush();return h}
fetch('latest').then(r=>r.json()).then(d=>{document.getElementById('rep').innerHTML=
 d.markdown?md(d.markdown):'No report yet. Models built: '+esc(d.models.join(', ')||'none')});
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
 const q=document.getElementById('q').value,ix=document.getElementById('ix').value;
 const box=document.getElementById('res');box.textContent='searching…';
 const d=await (await fetch('query?q='+encodeURIComponent(q))).json();
 box.innerHTML=Object.entries(d).map(([m,r])=>'<div><h3>'+esc(m)+' <small>'+
  esc(r.latency_ms)+' ms</small></h3>'+(r.error?'<p>'+esc(r.error)+'</p>':'<table><tr><th>#</th>'+
  '<th>movie</th><th>time</th><th>score</th><th>text</th></tr>'+r[ix].map((x,i)=>'<tr><td>'+
  (i+1)+'</td><td>'+esc(x.title)+'</td><td>'+t(x.start)+'</td><td>'+x.score+'</td><td>'+
  esc((x.text||'').slice(0,300))+'</td></tr>').join('')+'</table>')+'</div>').join('')};
</script></body></html>"""


TRANSCRIPT_LANG_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>aibrarian · transcript languages</title>
<style>
body{font:14px system-ui,sans-serif;margin:1.5em;background:#111;color:#ddd}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #333;padding:4px 6px;
vertical-align:top;text-align:left}th{background:#222}.snip{color:#aaa;max-width:40em}
.flag{background:#533;border-radius:3px;padding:0 4px;margin-right:3px;font-size:12px}
button{cursor:pointer}.ok{color:#7c7}.bad{color:#e77}input,select{background:#222;color:#ddd}
.path{color:#777;font-size:11px}</style></head><body>
<h2>Transcript language check</h2>
<p id="counts"></p>
<p>Show <select id="flt"><option value="flagged">needs review</option>
<option value="nonen">non-English only</option><option value="all">all</option></select>
review <select id="rev"><option value="">any</option><option value="none" selected>not reviewed</option>
<option value="correct">correct</option><option value="wrong">wrong</option></select>
<input id="q" placeholder="title / path"> <button onclick="load()">reload</button></p>
<p style="color:#999">&#10003; correct = the transcript really is non-English (foreign film) /
fine. &#10007; wrong = bad transcription (misdetected language, hallucination).
Nothing is changed on disk.</p>
<table><thead><tr><th>title</th><th>detected</th><th>conf</th><th>whisper</th><th>flags</th>
<th>snippet</th><th>review</th></tr></thead><tbody id="rows"></tbody></table>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){
  const f=document.getElementById('flt').value,p=new URLSearchParams();
  p.set('flagged',f==='flagged');p.set('non_english',f==='nonen');
  const r=document.getElementById('rev').value;if(r)p.set('review',r);
  const q=document.getElementById('q').value;if(q)p.set('q',q);p.set('limit',500);
  const d=await (await fetch('/transcripts/lang?'+p)).json(),c=d.counts||{};
  document.getElementById('counts').textContent=`${c.total||0} checked, ${c.needs_review||0} need review,
 ${c.non_english||0} non-English, ${c.correct||0} marked correct, ${c.wrong||0} wrong; showing ${d.items.length}
 of ${d.total}; last check ${c.checked_at||'never'}`;
  document.getElementById('rows').innerHTML=d.items.map(i=>`<tr>
<td>${esc(i.title)}<div class="path">${esc(i.path)}</div></td><td>${esc(i.detected_lang)}</td>
<td>${i.confidence==null?'':i.confidence.toFixed(2)}</td><td>${esc(i.stored_lang)}</td>
<td>${(i.flags||[]).map(x=>`<span class="flag">${esc(x)}</span>`).join('')}
${i.repeat_ratio?`<div class="path">repeat ${(i.repeat_ratio*100).toFixed(0)}%</div>`:''}</td>
<td class="snip">${esc(i.snippet)}</td>
<td>${i.review?`<b class="${i.review==='correct'?'ok':'bad'}">${i.review}</b>
<button onclick="mark(${i.id},null)">undo</button>`:
`<button onclick="mark(${i.id},'correct')">&#10003; correct</button>
<button onclick="mark(${i.id},'wrong')">&#10007; wrong</button>`}</td></tr>`).join('');
}
async function mark(id,review){
  const r=await fetch(`/transcripts/lang/${id}/review`,{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({review})});
  if(!r.ok)alert(await r.text());load();
}
load();
</script></body></html>"""

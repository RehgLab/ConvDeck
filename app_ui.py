"""Single-page front-end for the ConvDeck user-study app (served by app.py)."""

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ConvDeck — Slide Study</title>
<style>
:root{
  --bg:#0f1220; --panel:#171a2b; --panel2:#1f2338; --line:#2b3050;
  --ink:#eef1fb; --muted:#a2a9c9; --brand:#7c5cff; --brand2:#00d4b1;
  --ok:#3ddc97; --err:#ff5d6c; --radius:16px;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  background:radial-gradient(1200px 700px at 15% -10%,#241a4d 0,transparent 55%),
             radial-gradient(1000px 600px at 110% 10%,#093f3a 0,transparent 50%),var(--bg);
  color:var(--ink);font:15px/1.55 "Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
}
a{color:var(--brand2)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 80px}
header.top{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.logo{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,var(--brand),var(--brand2));
  display:grid;place-items:center;font-weight:800;color:#0b0e19;font-size:20px}
.top h1{font-size:20px;margin:0;font-weight:700;letter-spacing:.2px}
.top .sub{color:var(--muted);font-size:13px}

/* stepper */
.steps{display:flex;gap:8px;margin:10px 0 22px;flex-wrap:wrap}
.step{display:flex;align-items:center;gap:8px;padding:7px 13px;border:1px solid var(--line);
  border-radius:999px;color:var(--muted);font-size:12.5px;background:var(--panel)}
.step .dot{width:18px;height:18px;border-radius:50%;background:var(--panel2);display:grid;
  place-items:center;font-size:11px;color:var(--muted);border:1px solid var(--line)}
.step.active{border-color:var(--brand);color:var(--ink);box-shadow:0 0 0 1px var(--brand) inset}
.step.active .dot{background:var(--brand);color:#0b0e19;border-color:var(--brand)}
.step.done{color:var(--ink)}
.step.done .dot{background:var(--ok);color:#0b0e19;border-color:var(--ok)}

.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:22px;margin-bottom:18px;box-shadow:0 10px 40px rgba(0,0,0,.25)}
.card h2{margin:0 0 4px;font-size:18px}
.card p.hint{color:var(--muted);margin:0 0 18px;font-size:13.5px}

label.fld{display:block;margin-bottom:16px}
label.fld .lab{display:block;font-size:12.5px;color:var(--muted);margin-bottom:6px;font-weight:600;letter-spacing:.3px;text-transform:uppercase}
input[type=text],input[type=number],textarea,select{
  width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--ink);
  border-radius:12px;padding:11px 13px;font:inherit;outline:none}
input:focus,textarea:focus,select:focus{border-color:var(--brand)}
textarea{resize:vertical;min-height:64px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}

.btn{appearance:none;border:1px solid var(--line);background:var(--panel2);color:var(--ink);
  padding:11px 18px;border-radius:12px;font:inherit;font-weight:600;cursor:pointer;transition:.15s}
.btn:hover{border-color:var(--brand)}
.btn.primary{background:linear-gradient(135deg,var(--brand),#5b7bff);border:none;color:#fff}
.btn.primary:hover{filter:brightness(1.08)}
.btn.ok{background:linear-gradient(135deg,var(--ok),var(--brand2));border:none;color:#062018}
.btn.ghost{background:transparent}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.lg{padding:13px 26px;font-size:15px}

/* file drop */
.drop{border:1.5px dashed var(--line);border-radius:14px;padding:20px;text-align:center;
  color:var(--muted);background:var(--panel2);cursor:pointer;transition:.15s}
.drop.hover{border-color:var(--brand);color:var(--ink)}
.drop b{color:var(--ink)}
.filechip{display:inline-flex;gap:8px;align-items:center;background:var(--panel2);
  border:1px solid var(--line);border-radius:999px;padding:6px 12px;margin-top:10px;font-size:13px}

@media(max-width:820px){.grid2{grid-template-columns:1fr}}

/* outline cards */
.outline{display:grid;gap:10px;max-height:56vh;overflow:auto;padding-right:6px}
.oslide{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--panel2)}
.oslide .n{display:inline-grid;place-items:center;min-width:26px;height:26px;border-radius:8px;
  background:var(--brand);color:#0b0e19;font-weight:700;font-size:12.5px;margin-right:10px}
.oslide .t{font-weight:600}
.oslide .idea{color:var(--muted);font-size:13px;margin-top:6px}

/* slide viewer */
.viewer{display:flex;flex-direction:column;align-items:center;gap:12px}
.viewer .stage{width:100%;background:#0b0e19;border:1px solid var(--line);border-radius:12px;
  min-height:340px;display:grid;place-items:center;overflow:hidden}
.viewer .stage img{width:100%;max-height:62vh;object-fit:contain;display:block}
.nav{display:flex;align-items:center;gap:14px}
.nav .count{color:var(--muted);font-variant-numeric:tabular-nums;min-width:96px;text-align:center}
.thumbs{display:flex;gap:8px;overflow-x:auto;width:100%;padding-bottom:6px}
.thumbs img{height:66px;border-radius:6px;border:2px solid var(--line);cursor:pointer;background:#0b0e19}
.thumbs img.sel{border-color:var(--brand)}

/* feedback bar */
.fbbar{margin-top:16px;display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:end}
@media(max-width:720px){.fbbar{grid-template-columns:1fr}}

/* processing */
.proc{display:flex;flex-direction:column;align-items:center;gap:18px;padding:24px 0}
.spinner{width:52px;height:52px;border-radius:50%;border:5px solid var(--panel2);
  border-top-color:var(--brand);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.proc .msg{font-size:16px;font-weight:600}
.proc .sub{color:var(--muted);font-size:13px;text-align:center;max-width:520px}

/* log */
.logbox{margin-top:16px}
.logbox summary{cursor:pointer;color:var(--muted);font-size:13px}
pre.log{background:#0b0e19;border:1px solid var(--line);border-radius:10px;padding:12px;
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#c7cdf0;max-height:280px;
  overflow:auto;white-space:pre-wrap;margin-top:10px}

.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600}
.badge.ok{background:rgba(61,220,151,.15);color:var(--ok)}
.hidden{display:none!important}
.center{text-align:center}
.big{font-size:22px;font-weight:700}
.muted{color:var(--muted)}
.spread{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--panel2);
  border:1px solid var(--line);padding:12px 18px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.4);
  opacity:0;transition:.2s;pointer-events:none;z-index:50}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="logo">A</div>
    <div>
      <h1>ConvDeck &middot; Presentation Study</h1>
      <div class="sub">Turn a paper into slides — and steer the result with your feedback.</div>
    </div>
  </header>

  <div class="steps" id="steps"></div>
  <div id="root"></div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (s,el=document)=>el.querySelector(s);
const el = (t,props={},...kids)=>{const e=document.createElement(t);
  for(const k in props){ if(k==='class')e.className=props[k]; else if(k==='html')e.innerHTML=props[k];
    else if(k.startsWith('on'))e.addEventListener(k.slice(2),props[k]); else e.setAttribute(k,props[k]); }
  for(const c of kids){ if(c==null)continue; e.append(c.nodeType?c:document.createTextNode(c)); } return e;};
const root = $('#root');
let CFG=null, SID=null, POLL=null, STATE=null;
let selSlide=0, lastToken=-1, lastState=null;

function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2200);}

const STEP_DEFS=[
  ['setup','Setup'],['outline','Outline'],
  ['slides','Slides'],['done','Done']
];
function stepForState(s){
  return ({setup:'setup',
    outline_feedback:'outline',
    generation_feedback:'slides',processing:'proc',
    done:'done',error:'error'})[s]||'setup';
}
function renderSteps(cur){
  const order=STEP_DEFS.map(x=>x[0]);
  // map transient states to a step for progress display
  const map={setup:'setup',outline:'outline',slides:'slides',
    done:'done',proc:null,error:null};
  const curStep=map[cur];
  const idx=order.indexOf(curStep);
  const box=$('#steps');box.innerHTML='';
  STEP_DEFS.forEach(([key,label],i)=>{
    let cls='step';
    if(curStep){ if(i<idx)cls+=' done'; else if(i===idx)cls+=' active'; }
    box.append(el('div',{class:cls},
      el('span',{class:'dot'}, (curStep&&i<idx)?'✓':String(i+1)), label));
  });
}

/* ───────────────────────── SETUP ───────────────────────── */
async function boot(){
  CFG=await (await fetch('/api/config')).json();
  renderSetup();
}
function renderSetup(){
  renderSteps('setup');
  const paperOpts=CFG.papers.map(p=>el('option',{value:p},p));
  const state={file:null};

  const drop=el('div',{class:'drop'},el('div',{html:'<b>Drop a PDF here</b> or click to browse'}));
  const fileInput=el('input',{type:'file',accept:'.pdf',style:'display:none'});
  const chip=el('div',{class:'filechip hidden'});
  function setFile(f){ state.file=f;
    if(f){chip.classList.remove('hidden');chip.textContent='📄 '+f.name;paperSel.value='';}
    else chip.classList.add('hidden'); }
  drop.onclick=()=>fileInput.click();
  fileInput.onchange=e=>setFile(e.target.files[0]||null);
  ;['dragover','dragenter'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('hover');}));
  ;['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('hover');}));
  drop.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(f)setFile(f);});

  const paperSel=el('select',{onchange:()=>{if(paperSel.value)setFile(null);}},
    el('option',{value:''}, CFG.papers.length?'— choose a provided paper —':'(no provided papers found)'),
    ...paperOpts);

  const modelSel=el('select',{}, ...CFG.models.map(m=>el('option',{value:m},m)));
  modelSel.value=CFG.default_model;
  const audSel=el('select',{}, ...CFG.audiences.map(a=>el('option',{value:a},a)));
  const durInput=el('input',{type:'number',min:CFG.duration.min,max:CFG.duration.max,
    step:CFG.duration.step,value:CFG.duration.default});
  const runName=el('input',{type:'text',placeholder:'e.g. my-run-01 (optional)'});
  const instr=el('textarea',{placeholder:"e.g. Emphasize the experimental results and keep it high-level for a broad ML audience. (optional)"});

  const startBtn=el('button',{class:'btn primary lg',onclick:start},'Generate my slides →');

  async function start(){
    if(!state.file && !paperSel.value){toast('Upload a PDF or choose a paper first.');return;}
    startBtn.disabled=true;startBtn.textContent='Starting…';
    const fd=new FormData();
    fd.append('model',modelSel.value);
    fd.append('duration',durInput.value);
    fd.append('audience',audSel.value);
    fd.append('instructions',instr.value);
    fd.append('run_name',runName.value);
    if(state.file) fd.append('file',state.file);
    else fd.append('paper',paperSel.value);
    try{
      const r=await fetch('/api/start',{method:'POST',body:fd});
      if(!r.ok){throw new Error((await r.json()).detail||'start failed');}
      SID=(await r.json()).session_id;
      startPolling();
    }catch(e){toast(e.message);startBtn.disabled=false;startBtn.textContent='Generate my slides →';}
  }

  root.innerHTML='';
  root.append(
    el('div',{class:'card'},
      el('h2',{},'1 · Choose your paper'),
      el('p',{class:'hint'},'Upload a research PDF, or pick one of the provided papers.'),
      drop, fileInput, chip,
      el('div',{style:'margin-top:14px'}, el('label',{class:'fld'},
        el('span',{class:'lab'},'…or provided paper'), paperSel))
    ),
    el('div',{class:'card'},
      el('h2',{},'2 · Presentation settings'),
      el('div',{class:'grid2'},
        el('label',{class:'fld'},el('span',{class:'lab'},'Model'),modelSel),
        el('label',{class:'fld'},el('span',{class:'lab'},'Target audience'),audSel),
        el('label',{class:'fld'},el('span',{class:'lab'},'Duration (minutes)'),durInput),
        el('label',{class:'fld'},el('span',{class:'lab'},'Run name'),runName)
      ),
      el('label',{class:'fld'},el('span',{class:'lab'},'High-level instructions'),instr)
    ),
    el('div',{class:'center'},startBtn)
  );
}

/* ───────────────────────── POLLING ───────────────────────── */
function startPolling(){
  if(POLL)clearInterval(POLL);
  tick();
  POLL=setInterval(tick,1500);
}
async function tick(){
  if(!SID)return;
  try{
    const s=await (await fetch('/api/status/'+SID)).json();
    STATE=s; render(s);
  }catch(e){/* transient */}
}

// A "render key" identifies the current interaction so polling only rebuilds
// the DOM when the stage really changes — otherwise a user's half-typed
// feedback or star pick would be wiped every poll tick.
let lastRenderKey=null;
function renderKey(s){
  switch(s.state){
    case 'outline_feedback': return 'of:'+(s.round||0);
    case 'generation_feedback': return 'gf:'+(s.round||0)+':'+s.slides_token+':'+s.num_slides;
    case 'processing': return 'pr:'+(s.phase||'');
    case 'done': return 'done:'+(s.num_slides||0);
    case 'error': return 'err';
    default: return s.state;
  }
}
function render(s){
  renderSteps(stepForState(s.state));
  const key=renderKey(s);
  if(key===lastRenderKey){                 // same stage → just refresh the live log
    const lg=$('pre.log'); if(lg) lg.textContent=s.log_tail||'';
    return;
  }
  lastRenderKey=key; lastState=s.state;
  if(s.state==='outline_feedback') renderOutline(s);
  else if(s.state==='generation_feedback') renderSlides(s);
  else if(s.state==='processing') renderProcessing(s);
  else if(s.state==='done') renderDone(s);
  else if(s.state==='error') renderError(s);
}

function logBox(s){
  return el('details',{class:'logbox'},
    el('summary',{},'Show pipeline log'),
    el('pre',{class:'log'}, s.log_tail||''));
}

/* ───────────────────────── OUTLINE ───────────────────────── */
function renderOutline(s){
  const list=el('div',{class:'outline'});
  (s.outline||[]).forEach(o=>{
    list.append(el('div',{class:'oslide'},
      el('div',{}, el('span',{class:'n'},String(o.n)), el('span',{class:'t'},o.title||'(untitled)')),
      o.idea?el('div',{class:'idea'},o.idea):null));
  });
  const fb=el('textarea',{placeholder:"What should change? e.g. 'Add a slide on limitations', 'Merge slides 2 and 3', 'Add background on prior work'."});
  const send=el('button',{class:'btn',onclick:async()=>{
    if(!fb.value.trim()){toast('Type your feedback first — or click Approve.');return;}
    send.disabled=approve.disabled=true;
    await fetch('/api/feedback/'+SID,{method:'POST',body:new URLSearchParams({text:fb.value})});
    toast('Revising the outline…');
  }},'Request changes');
  const approve=el('button',{class:'btn ok',onclick:async()=>{
    send.disabled=approve.disabled=true;
    await fetch('/api/approve/'+SID,{method:'POST'});
    toast('Outline approved!');
  }},'Approve outline ✓');

  root.innerHTML='';
  root.append(el('div',{class:'card'},
    el('div',{class:'spread'},
      el('div',{}, el('h2',{},'Review the outline'),
        el('p',{class:'hint'},`Round ${(s.round||0)+1} · ${(s.outline||[]).length} slides. Skim the story, then approve or ask for changes.`)),
      el('span',{class:'badge ok'},'awaiting your input')),
    list,
    el('div',{class:'fbbar'}, fb, send, approve),
    logBox(s)
  ));
}

/* ───────────────────────── SLIDES ───────────────────────── */
// Shared deck viewer used for both the generation-feedback stage and the
// final "done" screen. `srcFor(i)` builds the image URL; nav paints locally
// so polling never resets the browsed slide.
function deckViewer(n, srcFor){
  selSlide=Math.min(selSlide, Math.max(0,n-1));
  const stage=el('div',{class:'stage'});
  const count=el('span',{class:'count'});
  const thumbs=el('div',{class:'thumbs'});
  function paint(){
    stage.innerHTML='';
    if(n>0) stage.append(el('img',{src:srcFor(selSlide)}));
    else stage.append(el('div',{class:'muted'},'Rendering slide preview…'));
    count.textContent=`${n?selSlide+1:0} / ${n}`;
    [...thumbs.children].forEach((c,i)=>c.classList.toggle('sel',i===selSlide));
  }
  for(let i=0;i<n;i++){
    thumbs.append(el('img',{src:srcFor(i),onclick:()=>{selSlide=i;paint();}}));
  }
  const prev=el('button',{class:'btn ghost',onclick:()=>{if(selSlide>0){selSlide--;paint();}}},'‹ Prev');
  const next=el('button',{class:'btn ghost',onclick:()=>{if(selSlide<n-1){selSlide++;paint();}}},'Next ›');
  paint();
  return el('div',{class:'viewer'}, stage, el('div',{class:'nav'}, prev, count, next), thumbs);
}

function renderSlides(s){
  const n=s.num_slides||0;
  const tok=s.slides_token;
  if(tok!==lastToken){ selSlide=0; lastToken=tok; }   // new render → back to slide 1

  const fb=el('textarea',{placeholder:"Refine the deck: e.g. 'Shorten the bullets on slide 4', 'Move the figure to slide 2', 'Add a takeaways slide'."});
  const send=el('button',{class:'btn',onclick:async()=>{
    if(!fb.value.trim()){toast('Type your feedback first — or click Approve.');return;}
    send.disabled=approve.disabled=true;
    await fetch('/api/feedback/'+SID,{method:'POST',body:new URLSearchParams({text:fb.value})});
    toast('Applying your changes…');
  }},'Request changes');
  const approve=el('button',{class:'btn ok',onclick:async()=>{
    send.disabled=approve.disabled=true;
    await fetch('/api/approve/'+SID,{method:'POST'});
    toast('Slides approved!');
  }},'Approve slides ✓');

  root.innerHTML='';
  root.append(el('div',{class:'card'},
    el('div',{class:'spread'},
      el('div',{}, el('h2',{},'Review your slides'),
        el('p',{class:'hint'},`Round ${(s.round||0)+1} · ${n} slides. Browse the deck, then approve or request edits.`)),
      el('span',{class:'badge ok'},'awaiting your input')),
    deckViewer(n,(i)=>`/api/slide/${SID}/${i}?v=${tok}`),
    el('div',{class:'fbbar'}, fb, send, approve),
    logBox(s)
  ));
}

/* ───────────────────────── PROCESSING ───────────────────────── */
function renderProcessing(s){
  root.innerHTML='';
  root.append(el('div',{class:'card'},
    el('div',{class:'proc'},
      el('div',{class:'spinner'}),
      el('div',{class:'msg'}, s.phase||'Working…'),
      el('div',{class:'sub'},'This step runs language & vision models — it can take a couple of minutes. You can watch the log below.')),
    logBox(s)
  ));
}

/* ───────────────────────── DONE ───────────────────────── */
function resetSession(){ if(POLL){clearInterval(POLL);POLL=null;}
  SID=null;lastState=null;lastRenderKey=null;lastToken=-1;selSlide=0;STATE=null;boot(); }

function renderDone(s){
  if(POLL){clearInterval(POLL);POLL=null;}
  selSlide=0;
  const n=s.num_slides||0;
  root.innerHTML='';
  root.append(el('div',{class:'card center'},
    el('div',{class:'big'},'🎉 Your presentation is ready'),
    el('p',{class:'hint'},`${n} slides`),
    el('a',{class:'btn primary lg',href:s.download,style:'text-decoration:none;display:inline-block'},'⬇ Download .pptx'),
    n>0?el('div',{style:'margin-top:20px'}, deckViewer(n,(i)=>`/api/final-slide/${SID}/${i}`)):null,
    el('div',{style:'margin-top:22px'}, el('button',{class:'btn ghost',onclick:resetSession},'Start another'))
  ));
}

function renderError(s){
  if(POLL){clearInterval(POLL);POLL=null;}
  root.innerHTML='';
  root.append(el('div',{class:'card'},
    el('h2',{},'Something went wrong'),
    el('p',{class:'hint'}, s.message||'The pipeline exited unexpectedly.'),
    el('pre',{class:'log'}, s.log_tail||''),
    el('div',{style:'margin-top:16px'}, el('button',{class:'btn',onclick:resetSession},'Back to start'))
  ));
}

boot();
</script>
</body>
</html>
"""

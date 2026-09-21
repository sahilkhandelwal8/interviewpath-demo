"use strict";
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl = v => {try {const u=new URL(v);return ["https:","http:"].includes(u.protocol)?u.href:"#";}catch{return "#";}};
const domain = v => {try{return new URL(v).hostname;}catch{return "Source";}};
const dateText = v => {if(!v)return "Date not supplied";const d=new Date(v);return isNaN(d)?v:d.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"numeric"});};
const stages = ["identify","research","draft","review"];
const labels = {identify:"Identify",research:"Research",draft:"Draft",review:"Review"};
const areas = {professional_profile:"Professional background",company_developments:"Company developments",recruiting_context:"Recruiting context"};
const statuses = {running:"In progress",needs_input:"Needs your input",failed:"Needs attention",ready:"Ready to review"};
let current=null, view=null, currentId=null, missing=[], sources=[], editFrom=null, busy=false, pollBusy=false, saveTimer=null, editingDraft=false, demoProspects=[], productBrief="";
let noticeTimer;
let researchTab="professional_profile";

async function api(path, data) {
  const response=await fetch(path,data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  const result=await response.json();if(!response.ok)throw new Error(result.error||"Request failed");return result;
}
async function deleteRun(id){const response=await fetch(`/api/runs/${id}`,{method:"DELETE"});if(!response.ok){const result=await response.json();throw new Error(result.error||"Could not delete this run");}}
function notify(message){$("#notice").textContent=message;$("#notice").classList.add("visible");clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$("#notice").classList.remove("visible"),5000);}
function badge(status){const confirmed=current?.draft_confirmed_at&&status==="ready";const display=confirmed?"confirmed":status;return `<span class="badge ${display}"><span class="dot ${display}"></span>${confirmed?"Confirmed":esc(statuses[status]||status)}</span>`;}
function shell(body, breadcrumb="New prospect"){return `<header class="topbar"><div class="breadcrumb">Outreach <span>/</span> ${esc(breadcrumb)}</div><div class="top-note">Grounded in evidence. Reviewed by you.</div></header><div class="content">${body}</div>`;}
function buttons(){return `<div class="actions">${current.status==="failed"?`<button class="primary" data-action="retry">Retry ${current.stage==="draft"?"drafting":current.stage==="research"?"research":"lookup"}</button>`:""}<button class="secondary" data-action="edit">Edit prospect</button></div>`;}
function sourceButton(source){let index=sources.push(source)-1;return `<button class="source-button" data-source="${index}">${esc(source.title||domain(source.url))} ↗</button>`;}
function pendingLocal(id){try{return JSON.parse(localStorage.getItem("outreach-edit-"+id)||"null");}catch{return null;}}

async function history(){
  const runs=await api("/api/runs");$("#run-count").textContent=runs.length;
  $("#history").innerHTML=runs.length?runs.map(r=>`<div class="history-row ${r.id===currentId?"active":""}"><button class="history-item" data-run="${r.id}" ${r.id===currentId?'aria-current="page"':""}><strong>${esc(r.prospect.name)}</strong><span class="company">${esc(r.prospect.company)}</span><span class="run-status"><span class="dot ${r.draft_confirmed_at?"confirmed":r.status}"></span>${r.draft_confirmed_at?"Confirmed":esc(statuses[r.status])}</span></button><button class="icon-button history-delete" data-delete-run="${r.id}" aria-label="Delete ${esc(r.prospect.name)}" title="Delete run">🗑</button></div>`).join(""):`<div class="empty-history">Your prospects will appear here.<br>Start a run to build your first draft.</div>`;
}
function newView(prefill=null){
  currentId=null;current=null;view=null;editFrom=prefill;location.hash="new";
  $("#main").innerHTML=shell(`<div class="selection-heading"><div class="eyebrow">NEW OUTREACH</div><h1>Who will you reach out to?</h1><p>Choose a demo prospect or add your own. We’ll check their identity first.</p></div>${prefill?'<div class="editing-banner">These details start a new run. Your previous run stays in history.</div>':""}<div class="selection-layout"><section class="prospect-library" aria-labelledby="library-title"><div class="library-heading"><h2 id="library-title">Demo prospects</h2><span class="small muted">${demoProspects.length} prepared cases</span></div><div id="demo-list" class="prospect-options"></div><button class="custom-prospect" type="button" id="custom-prospect" aria-pressed="false"><span class="prospect-avatar" aria-hidden="true">+</span><span><strong>Add your own prospect</strong><small>Start with a name, company and role</small></span><span class="option-arrow" aria-hidden="true">→</span></button><p class="library-note">Based on research collected 20 Sep 2026. Live results may differ.</p></section><form id="prospect-form" class="card selection-card"><div id="prospect-preview"></div><details id="prospect-fields" class="prospect-fields"><summary>Edit prospect details</summary><div class="field-pair"><label class="field" for="name">Full name<input id="name" name="name" required maxlength="160" placeholder="Taylor Morgan" autocomplete="off"></label><label class="field" for="company">Company<input id="company" name="company" required maxlength="160" placeholder="Northstar" autocomplete="off"></label></div><label class="field" for="role">Role<input id="role" name="role" required maxlength="200" placeholder="Head of Talent Acquisition" autocomplete="off"></label><label class="field" for="profile">LinkedIn URL <span class="optional">Optional</span><input id="profile" name="profile_url" type="url" maxlength="2000" placeholder="https://www.linkedin.com/in/…"></label></details>${missing.length?`<div class="warning"><strong>Setup needed</strong>Add ${esc(missing.join(", "))} to the repository’s .env file and restart the server.</div>`:""}<div id="form-error" class="inline-error" role="alert"></div><div class="selection-next"><button class="primary full" type="submit" ${missing.length?"disabled":""}>Research prospect <span aria-hidden="true">→</span></button></div></form></div><div class="entry-footer"><span>YOUR NEXT STEPS</span><ol><li>Identify</li><li>Research</li><li>Draft</li><li>Review</li></ol></div>`);
  mountDemoPicker();
  if(prefill||!demoProspects.length)selectCustomProspect(prefill);else selectDemoProspect(0);
  history().catch(()=>{});
}
async function openRun(id){
  currentId=id;view=null;editFrom=null;editingDraft=false;researchTab="professional_profile";location.hash=id;
  try {const r=await api("/api/runs/"+id);if(currentId!==id)return;current=r;view=r.stage;render();await history();}catch(e){notify(e.message);}
}
function render(){
  if(!current)return;sources=[];const r=current;const reached=stages.indexOf(r.stage);
  $("#main").innerHTML=shell(`<div class="run-heading"><div><div class="eyebrow">PROSPECT WORKSPACE</div><h1>${esc(r.prospect.name)}</h1><p>${esc(r.prospect.company)}${r.prospect.role?" · "+esc(r.prospect.role):""}</p></div><div class="run-heading-actions">${badge(r.status)}<button class="icon-button danger-icon" data-delete-run="${r.id}" aria-label="Delete this prospect run" title="Delete run">🗑</button></div></div><nav class="steps" aria-label="Run stages">${stages.map((s,i)=>`<button class="stage ${view===s?"active":""} ${i<reached?"done":""}" data-stage="${s}" ${i>reached?"disabled":""} ${view===s?'aria-current="step"':""}><span class="num">${i<reached?"✓":i+1}</span>${labels[s]}</button>`).join("")}</nav><section id="stage-content">${view==="identify"?identifyView():view==="research"?researchView():view==="draft"?draftView():reviewView()}</section>`,r.prospect.name);
  tick();
}
function running(title,description){return `<div class="card status-panel"><div class="spinner" aria-hidden="true"></div><h2>${title}</h2><p class="muted">${description}</p><p class="elapsed" data-elapsed="${esc(current.stage_started_at)}"></p></div>`;}
function errorPanel(){return `<div class="warning" role="alert"><strong>This stage needs attention</strong>${esc(current.error)}</div>${buttons()}`;}
function identitySources(identity){
  const grouped=new Map();
  for(const source of identity.sources){
    const key=safeUrl(source.url).replace(/#.*$/,"");
    if(!grouped.has(key))grouped.set(key,{...source,claims:[],passages:[]});
    const item=grouped.get(key);
    if(!item.passages.includes(source.content))item.passages.push(source.content);
    const refs=[];
    for(const candidate of identity.candidates){
      for(const field of ["name","company","role"]){
        for(const ref of candidate[field].evidence)refs.push({...ref,claim:`${candidate.name.value||"Candidate"} · ${field}: ${candidate[field].value||"Unknown"}`});
      }
      for(const ref of candidate.company_conflict_evidence)refs.push({...ref,claim:"Employer discrepancy"});
    }
    for(const ref of identity.explanation_evidence)refs.push({...ref,claim:"Match explanation"});
    for(const ref of refs.filter(ref=>ref.source_id===source.page_id)){
      if(!item.claims.some(c=>c.quote===ref.quote&&c.claim===ref.claim))item.claims.push({claim:ref.claim,quote:ref.quote});
    }
  }
  return [...grouped.values()].map(item=>({...item,content:item.passages.join("\n\n")}));
}
function identifyView(){
  const r=current,identity=r.identity;
  if(r.stage==="identify"&&r.status==="running")return running("Checking identity and current role","We’ll show the match and supporting sources for you to confirm.");
  if(r.stage==="identify"&&r.status==="failed")return errorPanel();
  if(!identity)return "";
  const evidence=identitySources(identity);
  return `<div class="section-heading"><div><h2>${identity.confirmation_label?"Confirmed prospect":"Confirm the right person"}</h2><p>${identity.confirmation_label?"Confirmed by you · identity and employment context only":"Research starts only after you confirm a match."}</p></div>${!identity.confirmation_label?'<button class="secondary" data-action="edit">Edit details</button>':""}</div>
    <p class="muted small">${esc(identity.summary)}</p>
    ${identity.candidates.length?identity.candidates.map(c=>`<article class="card candidate">
      <div class="candidate-top"><h3>${esc(c.name.value||"Name unclear")}</h3>${identity.confirmed_candidate_index===c.index?'<span class="badge ready">✓ Confirmed by you</span>':""}</div>
      <dl><dt>Current role</dt><dd>${esc(c.role.value||"Not established")}</dd><dt>Company</dt><dd>${esc(c.company.value||"Not established")}</dd></dl>
      ${c.note?`<p class="small muted">${esc(c.note)}</p>`:""}
      ${c.company_conflict_evidence.length?'<div class="warning"><strong>Clarification needed</strong>The sources name different employers. Edit the details or add a profile URL so we can check again.</div>':!c.can_confirm?'<div class="warning">Some identity details could not be established. Edit the details or add a profile URL.</div>':""}
      ${!identity.confirmation_label?`<div class="actions"><button class="primary" data-confirm="${c.index}" ${!c.can_confirm?"disabled":""}>Confirm and continue →</button></div>`:""}
    </article>`).join(""):`<div class="card"><h3>We need a little more information</h3><p class="muted">Check the name, company and role, or add a LinkedIn URL to distinguish this person.</p>${buttons()}</div>`}
    ${evidence.length?`<div class="identity-sources"><h3>Sources · ${evidence.length}</h3>${evidence.map(sourceButton).join("")}</div>`:""}`;
}
function researchView(){
  const r=current,key=researchTab,label=areas[key],a=r.areas[key]||{status:"searching",results:[]};
  const status=area=>area.status==="searching"?"Searching…":area.status==="failed"?"Search failed":"Complete";
  return `<div class="section-heading"><div><h2>Public research</h2><p>Explore the sources collected for each research area.</p></div>${r.stage==="research"&&r.status==="running"?`<span class="elapsed" data-elapsed="${esc(r.stage_started_at)}"></span>`:""}</div>
    ${r.stage==="research"&&r.status==="failed"?errorPanel():""}
    <div class="research-tabs" role="tablist" aria-label="Research areas">${Object.entries(areas).map(([area,title])=>`<button role="tab" id="tab-${area}" aria-controls="research-panel" aria-selected="${area===key}" tabindex="${area===key?0:-1}" class="research-tab ${area===key?"active":""}" data-research-tab="${area}"><span>${title}</span><small>${status(r.areas[area]||{status:"searching"})}</small></button>`).join("")}</div>
    <article class="card area-card" id="research-panel" role="tabpanel" aria-labelledby="tab-${key}" tabindex="0">
      <div class="area-head"><h3>${label}</h3><span class="badge">${a.results.length} results</span></div>
      ${a.status==="searching"?'<p class="small muted">Searching for public sources…</p>':""}
      ${a.status==="complete"&&!a.results.length?'<p class="small muted">No results returned.</p>':""}
      ${a.status==="failed"?'<p class="small muted">This search could not finish. Results from other areas remain available.</p>':""}
      ${a.results.map(source=>`<div class="search-result"><a href="${esc(safeUrl(source.url))}" target="_blank" rel="noopener noreferrer">${esc(source.title||domain(source.url))} ↗</a><div class="meta">${esc(domain(source.url))} · ${source.published_at?"Published "+esc(dateText(source.published_at)):"Publication date not supplied"} · Collected ${esc(dateText(a.collected_at))}</div><details><summary>View search passage</summary><p>${esc(source.snippet||"No passage returned.")}</p></details></div>`).join("")}
    </article>`;
}
function draftView(){
  if(current.stage==="draft"&&current.status==="failed")return errorPanel();
  if(current.stage==="draft")return running("Creating your outreach draft","Choosing a relevant angle and writing from the collected sources.");
  return `<div class="card status-panel"><div class="eyebrow">DRAFT COMPLETE</div><h2>Your outreach is ready to review</h2><p class="muted">The draft, selected angle, and supporting evidence are together in Review.</p><button class="primary" data-stage="review">Open review →</button></div>`;
}
function reviewView(){
  const r=current,o=r.output;if(!o)return "";const d=o.draft,m=o.review;
  let local=pendingLocal(r.id);if(local&&local.draft_version!==r.attempt){localStorage.removeItem("outreach-edit-"+r.id);local=null;}const edited=local||r.edits||d;
  const changed=d&&edited&&(edited.subject!==d.subject||edited.body!==d.body);
  if(d){
    const confirmation=r.draft_confirmed_at?`<span class="draft-confirmed">✓ Draft confirmed</span>`:`<button class="primary" data-action="approve">✓ Confirm draft</button>`;
    const draftPanel=editingDraft?`<div class="editor-top"><h3>Outreach draft</h3><button class="text-button" data-action="preview-draft">Preview</button></div><label class="field" for="subject">Subject<input id="subject" maxlength="500" value="${esc(edited.subject)}"></label><label class="field" for="body">Message<textarea id="body" maxlength="20000">${esc(edited.body)}</textarea></label><div id="edited-note" class="edited-note ${changed?"visible":""}">The evidence assessment applies to the generated draft. Review any claims you add or change against the sources.</div><div class="editor-footer"><span class="small muted" id="word-count"></span><div class="actions"><button class="secondary" data-action="save">Save edits</button><button class="primary" data-action="copy">Copy draft</button></div></div>`:`<button class="draft-preview" data-action="edit-draft" aria-label="Edit outreach draft"><div class="editor-top"><span><h3>Outreach draft</h3><small>Click anywhere to edit</small></span><span class="edit-pencil">✎ Edit</span></div><div class="email-meta"><span>To</span> ${esc(r.prospect.name)}</div><div class="email-subject"><span>Subject</span>${esc(edited.subject)}</div><div class="email-body">${esc(edited.body)}</div></button><div class="editor-footer preview-footer"><span class="small muted">${edited.body.trim().split(/\s+/).filter(Boolean).length} words</span><div class="actions"><button class="secondary" data-action="copy">Copy draft</button>${confirmation}</div></div>`;
    return `<div class="section-heading"><div><h2>A message ready for your review</h2><p>Read it as your recipient would, then click the draft to make changes.</p></div></div>${m.limitation?`<div class="warning"><strong>Limited evidence</strong>${esc(m.limitation)}</div>`:""}<div class="review-grid"><div class="card editor-card">${draftPanel}</div><aside class="review-side"><div class="card"><div class="angle-label">SELECTED ANGLE</div><div class="angle">${esc(m.angle)}</div><p>${esc(m.explanation)}</p></div><div class="card"><h3>Evidence strength</h3><span class="evidence-level">${esc(o.strength)}</span><h3 class="section-heading">Sources used · ${o.sources.length}</h3>${o.sources.length?o.sources.map(s=>{const index=sources.push({...s,content:s.passage,claims:m.claims.filter(c=>c.source_id===s.source_id)})-1;return `<button class="review-source" data-source="${index}">${esc(s.title||domain(s.url))} ↗<small>${esc(domain(s.url))} · ${esc(dateText(s.published_at))}</small></button>`;}).join(""):'<p>No personalized source claims used.</p>'}</div></aside></div>${regenerationForm()}`;
  }
  return `<div class="section-heading"><div><h2>${d?"A message ready for your review":"Review the research outcome"}</h2><p>${d?"Make it your own, then copy when you’re ready.":"The evidence did not support a defensible message."}</p></div></div>${m.limitation?`<div class="warning"><strong>Limited evidence</strong>${esc(m.limitation)}</div>`:""}<div class="review-grid"><div class="card editor-card">${d?`<div class="editor-top"><h3>Outreach draft</h3><span class="muted small" id="save-status">${local?"Unsaved edits on this device":r.edits?"Edits saved":"Original draft"}</span></div><label class="field" for="subject">Subject<input id="subject" maxlength="500" value="${esc(edited.subject)}"></label><label class="field" for="body">Message<textarea id="body" maxlength="20000">${esc(edited.body)}</textarea></label><div id="edited-note" class="edited-note ${changed?"visible":""}">The evidence assessment applies to the generated draft. Review any claims you add or change against the sources.</div><div class="editor-footer"><span class="small muted" id="word-count"></span><div class="actions"><button class="secondary" data-action="save">Save edits</button><button class="primary" data-action="copy">Copy draft</button></div></div>`:`<div class="empty-icon">↗</div><h3>No draft produced</h3><p class="muted">${esc(m.explanation)}</p><p class="small muted">You can add instructions below and ask for another draft.</p>`}</div><aside class="review-side"><div class="card"><div class="angle-label">SELECTED ANGLE</div><div class="angle">${esc(m.angle)}</div><p>${esc(m.explanation)}</p></div><div class="card"><h3>Evidence strength</h3><span class="evidence-level">${esc(o.strength)}</span><h3 class="section-heading">Sources used · ${o.sources.length}</h3>${o.sources.length?o.sources.map(s=>{const index=sources.push({...s,content:s.passage,claims:m.claims.filter(c=>c.source_id===s.source_id)})-1;return `<button class="review-source" data-source="${index}">${esc(s.title||domain(s.url))} ↗<small>${esc(domain(s.url))} · ${esc(dateText(s.published_at))}</small></button>`;}).join(""):'<p>No personalized source claims used.</p>'}</div></aside></div>${regenerationForm()}`;
}
function regenerationForm(){
  const comments=localStorage.getItem("outreach-regen-"+currentId)||"";
  const available=current.available_sources||[],selected=selectedSources();
  return `<div class="card regeneration"><div><h3>Try another version</h3><p class="muted small">Adjust the direction, choose the evidence, or add a source.</p></div><label class="field" for="regen-comments">Refinement notes <span class="optional">Optional</span><textarea id="regen-comments" maxlength="4000" placeholder="e.g. Make it shorter and focus on interview coordination.">${esc(comments)}</textarea></label><details class="source-picker" id="source-picker"><summary>Research sources <span id="source-count">${selected.length} of ${available.length} selected</span></summary><p class="muted small">The next draft can use only selected sources. It may not need every source you select.</p><button class="text-button" data-action="select-all-sources">Select all</button><div class="source-options">${available.map(s=>{const index=sources.push({...s,content:s.passage})-1;return `<div class="source-option"><label><input type="checkbox" data-draft-source="${esc(s.source_id)}" ${selected.includes(s.source_id)?"checked":""}><span><strong>${esc(s.title||domain(s.url))}</strong><small>${esc(domain(s.url))} · ${s.evidence_kind==="retrieved_page"?"Added URL":"Search passage"}${current.output?.draft?.source_ids.includes(s.source_id)?" · Used in current draft":""}</small></span></label><button class="text-button" data-source="${index}">View text</button></div>`;}).join("")}</div><div class="add-source"><label class="field" for="source-url">Add a public page URL <span class="optional">Up to 3 per run</span><input id="source-url" type="url" maxlength="2000" placeholder="https://company.com/article"></label><button class="secondary" data-action="add-source" ${(current.added_sources||[]).length>=3?"disabled":""}>Fetch source</button></div><p class="muted small">We fetch the page text so you can inspect it before regenerating.</p><div id="source-error" class="inline-error" role="alert"></div></details><div class="actions"><button class="secondary" data-action="regenerate" ${available.length&&!selected.length?"disabled":""}>↻ Regenerate draft</button></div></div>`;
}
function selectedSources(){
  const available=(current.available_sources||[]).map(s=>s.source_id);
  let saved;try{saved=JSON.parse(localStorage.getItem("outreach-sources-"+currentId)||"null");}catch{}
  const selected=saved?.version===current.attempt?saved.ids:current.regeneration?.selected_source_ids;
  return Array.isArray(selected)?selected.filter(id=>available.includes(id)):available;
}
function storeSourceSelection(ids){localStorage.setItem("outreach-sources-"+currentId,JSON.stringify({version:current.attempt,ids}));}
function updateSourceSelection(){
  const selected=[...document.querySelectorAll("[data-draft-source]:checked")].map(el=>el.dataset.draftSource);
  storeSourceSelection(selected);
  $("#source-count").textContent=`${selected.length} of ${(current.available_sources||[]).length} selected`;
  $('[data-action="regenerate"]').disabled=Boolean(current.available_sources?.length&&!selected.length);
}
function initials(name){return name.split(/\s+/).filter(Boolean).map(part=>part[0]).slice(0,2).join("");}
function mountDemoPicker(){
  $("#demo-list").innerHTML=demoProspects.map((p,i)=>`<button type="button" class="prospect-option" data-demo-prospect="${i}" aria-pressed="false"><span class="prospect-avatar" aria-hidden="true">${esc(initials(p.name))}</span><span class="prospect-option-copy"><strong>${esc(p.name)} <span>· ${esc(p.company)}</span></strong><small>${esc(p.role)}</small><span class="case-label">${esc(p.scenario||"Demo prospect")}</span></span><span class="option-arrow" aria-hidden="true">→</span></button>`).join("");
}
function fillProspect(prospect){
  $("#name").value=prospect?.name||"";$("#company").value=prospect?.company||"";$("#role").value=prospect?.role||"";$("#profile").value=prospect?.profile_url||"";
  $("#form-error").textContent="";
}
function selectDemoProspect(index){
  const p=demoProspects[index];if(!p)return;fillProspect(p);
  document.querySelectorAll(".prospect-option").forEach((item,i)=>{item.classList.toggle("selected",i===index);item.setAttribute("aria-pressed",String(i===index));});
  $("#custom-prospect").setAttribute("aria-pressed","false");
  $("#prospect-fields").open=false;$("#prospect-fields").classList.remove("custom-fields");
  $("#prospect-preview").innerHTML=`<div class="eyebrow">SELECTED PROSPECT</div><h2>${esc(p.name)}</h2><p class="prospect-position">${esc(p.role)}<br><strong>${esc(p.company)}</strong></p>${p.profile_url?`<a class="profile-link" href="${esc(safeUrl(p.profile_url))}" target="_blank" rel="noopener noreferrer">LinkedIn profile ↗</a>`:""}<div class="case-context"><span class="eyebrow">WHY THIS PROSPECT</span><h3>${esc(p.evidence||"Explore this prospect’s public context")}</h3><p>${esc(p.detail||"Check their identity, then research a relevant reason to reach out.")}</p>${p.demonstrates?`<div class="demo-takeaway"><span class="eyebrow">WHAT TO SHOW</span><p>${esc(p.demonstrates)}</p></div>`:""}</div>`;
}
function selectCustomProspect(prefill=null,preserveFields=false){
  if(!preserveFields)fillProspect(prefill);
  document.querySelectorAll(".prospect-option").forEach(item=>{item.classList.remove("selected");item.setAttribute("aria-pressed","false");});
  $("#custom-prospect").setAttribute("aria-pressed","true");
  $("#prospect-preview").innerHTML='<div class="eyebrow">YOUR PROSPECT</div><h2>Start with the basics</h2><p class="muted">Add the person you have in mind. A profile link helps us find the right match.</p>';
  $("#prospect-fields").open=true;$("#prospect-fields").classList.add("custom-fields");
}
function renderBrief(){
  return `<header class="brief-hero"><div class="eyebrow">THE PRODUCT BEHIND THE OUTREACH</div><h1 id="brief-title">InterviewPath</h1><p class="brief-tagline">A clearer path through the hiring process.</p><p>A recruiting platform that brings applicant tracking, interview coordination and hiring analytics together.</p><span class="brief-caption">Case-study product · used as context for every draft</span></header><div class="brief-audience"><div><span class="eyebrow">BUILT FOR</span><p>Growing software companies with internal recruiting teams and multi-stage interviews.</p></div><div><span class="eyebrow">THE CONVERSATION STARTS WITH</span><p>Heads of Talent Acquisition<br>Recruiting Operations leaders</p></div></div><section class="brief-section"><h2>Where it can help</h2><div class="capability-grid"><article><span class="capability-number">01</span><h3>Keep hiring moving</h3><p>Track applications and stages, coordinate interviews and rescheduling, and follow up on outstanding feedback.</p><span class="capability-value">Less manual coordination</span></article><article><span class="capability-number">02</span><h3>See where work stalls</h3><p>See candidate stages and next steps, with analytics on conversion, time in stage, and scheduling or feedback delays.</p><span class="capability-value">A clearer view of the funnel</span></article><article><span class="capability-number">03</span><h3>Learn from recorded feedback</h3><p>Surface recurring themes in interviewer feedback and recorded decision reasons, with references to the underlying records.</p><span class="capability-value">Better-informed process reviews</span></article></div></section><section class="brief-section usage-section"><h2>Two ways to use it</h2><div class="brief-usage"><article><h3>Run the full workflow</h3><p>Manage applications, coordination, feedback and analytics in one platform.</p></article><article><h3>Work with an existing ATS</h3><p>Connect for coordination and analytics. Functionality depends on supported data and permissions.</p></article></div></section><details class="brief-boundaries"><summary>Product boundaries <span>What the outreach should never promise</span></summary><p>The product does not source candidates, manage long-term candidate relationships, rank candidates, independently assess suitability, or make hiring decisions.</p><p>Feedback analysis reflects recorded reasons and patterns. It does not establish missing reasons or decide whether a hiring decision was correct.</p><p>Benefits are intended value, not guaranteed results. Customer references, quantified results, pricing, timelines and named integrations require separately verified information.</p></details>`;
}
function evidence(index){
  const s=sources[index];if(!s)return;
  $("#evidence-content").innerHTML=`<h2>${esc(s.title||domain(s.url))}</h2><a class="small" href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.url)} ↗</a><p class="small muted">Published: ${esc(dateText(s.published_at))}<br>Collected: ${esc(dateText(s.collected_at||s.retrieved_at))}</p><span class="badge">${s.evidence_kind==="retrieved_page"?"Added URL · retrieved page excerpt":"Search passage · full page not retrieved"}</span><div class="source-passage">${esc(s.content||s.passage||"")}</div>${s.claims?.length?`<h3>Supported claims</h3>${s.claims.map(c=>`<div class="claim"><p>${esc(c.claim)}</p><blockquote>${esc(c.quote)}</blockquote>${c.publisher?`<p class="small muted">Publisher / author: ${esc(c.publisher)}</p>`:""}<div class="claim-tags">${[c.support,c.source,c.timing,c.consistency].filter(Boolean).map(t=>`<span>${esc(t)}</span>`).join("")}</div></div>`).join("")}`:""}`;
  $("#evidence").showModal();
}
function tick(){
  document.querySelectorAll("[data-elapsed]").forEach(el=>{const seconds=Math.max(0,Math.floor((Date.now()-new Date(el.dataset.elapsed))/1000));el.textContent=`${seconds}s elapsed`;});
  if($("#body"))$("#word-count").textContent=`${$("#body").value.trim().split(/\s+/).filter(Boolean).length} words`;
}
async function saveEdits(id=currentId, values=null){
  values=values||($("#subject")?{subject:$("#subject").value,body:$("#body").value,draft_version:current.attempt}:{...(pendingLocal(id)||current.edits||current.output.draft),draft_version:current.attempt});
  const result=await api(`/api/runs/${id}/save`,values);
  const latest=pendingLocal(id);
  if(!latest||(latest.subject===values.subject&&latest.body===values.body))localStorage.removeItem("outreach-edit-"+id);
  if(currentId===id&&current.attempt===result.attempt&&current.stage==="review"){current.edits=result.edits;if($("#save-status"))$("#save-status").textContent=pendingLocal(id)?"Saving…":"Edits saved";}
  return values;
}
async function mutate(action,data={}){
  if(busy)return;busy=true;const id=currentId;
  document.querySelectorAll("[data-action],[data-confirm]").forEach(b=>b.disabled=true);
  try{const result=await api(`/api/runs/${id}/${action}`,data);if(currentId===id){current=result;view=result.stage;render();}await history();}catch(e){notify(e.message);if(currentId===id)render();}finally{busy=false;}
}
document.addEventListener("invalid",e=>{if(e.target.closest("#prospect-fields"))$("#prospect-fields").open=true;},true);
document.addEventListener("submit",async e=>{
  if(e.target.id!=="prospect-form")return;e.preventDefault();if(busy)return;busy=true;const button=e.target.querySelector("button[type=submit]");button.disabled=true;
  try{const r=await api("/api/runs",Object.fromEntries(new FormData(e.target)));await openRun(r.id);}catch(error){$("#form-error").textContent=error.message;button.disabled=false;}finally{busy=false;}
});
document.addEventListener("click",async e=>{
  const button=e.target.closest("button");if(!button)return;
  if(button.id==="new-run")return newView();
  if(button.id==="custom-prospect"){selectCustomProspect();$("#name").focus();return;}
  if(button.id==="product-brief"){ $("#brief-content").innerHTML=renderBrief(productBrief); return $("#brief").showModal(); }
  if(button.id==="close-brief")return $("#brief").close();
  if(button.id==="close-evidence")return $("#evidence").close();
  if(button.dataset.deleteRun){
    const id=button.dataset.deleteRun;
    if(!confirm("Delete this run and its saved research? This cannot be undone."))return;
    try{await deleteRun(id);localStorage.removeItem("outreach-edit-"+id);localStorage.removeItem("outreach-regen-"+id);if(currentId===id)newView();else await history();notify("Run deleted");}catch(error){notify(error.message);}
    return;
  }
  if(button.dataset.demoProspect!==undefined)return selectDemoProspect(Number(button.dataset.demoProspect));
  if(button.dataset.run)return openRun(button.dataset.run);
  if(button.dataset.researchTab){researchTab=button.dataset.researchTab;render();$("#tab-"+researchTab).focus();return;}
  if(button.dataset.stage){view=button.dataset.stage;return render();}
  if(button.dataset.source!==undefined)return evidence(Number(button.dataset.source));
  if(button.dataset.confirm!==undefined)return mutate("confirm",{candidate:Number(button.dataset.confirm),lookup_id:current.identity.lookup_id});
  const action=button.dataset.action;if(!action)return;
  if(action==="select-all-sources"){document.querySelectorAll("[data-draft-source]").forEach(el=>el.checked=true);updateSourceSelection();return;}
  if(action==="add-source"){
    if(busy)return;
    const id=currentId,version=current.attempt,selected=selectedSources(),url=$("#source-url").value.trim();
    if(!url||!$("#source-url").checkValidity()){$("#source-error").textContent="Enter a valid public https:// page URL.";return;}
    busy=true;button.disabled=true;button.textContent="Fetching…";$("#source-error").textContent="";
    document.querySelectorAll('[data-action="regenerate"],[data-draft-source],[data-action="select-all-sources"]').forEach(el=>el.disabled=true);
    try{
      if($("#subject"))await saveEdits();
      const result=await api(`/api/runs/${id}/sources`,{url,draft_version:version});
      if(currentId!==id)return;
      const oldIds=new Set((current.available_sources||[]).map(s=>s.source_id));
      current=result;storeSourceSelection([...selected,...result.available_sources.filter(s=>!oldIds.has(s.source_id)).map(s=>s.source_id)]);
      render();$("#source-picker").open=true;notify("Source added. Use View text to inspect it before regenerating.");
    }catch(error){if(currentId===id&&$("#source-error"))$("#source-error").textContent=error.message;}
    finally{busy=false;if(currentId===id){button.disabled=false;button.textContent="Fetch source";document.querySelectorAll('[data-draft-source],[data-action="select-all-sources"]').forEach(el=>el.disabled=false);if($('[data-action="regenerate"]'))$('[data-action="regenerate"]').disabled=Boolean(current.available_sources?.length&&!selectedSources().length);}}
    return;
  }
  if(action==="edit-draft"){editingDraft=true;render();$("#subject").focus();return;}
  if(action==="preview-draft"){editingDraft=false;render();return;}
  if(action==="edit")return newView(current.identity?.entered||current.prospect);
  if(action==="save"||action==="copy"){
    clearTimeout(saveTimer);button.disabled=true;
    try{const values=await saveEdits();if(action==="copy"){await navigator.clipboard.writeText(`Subject: ${values.subject}\n\n${values.body}`);notify("Draft copied");}else notify("Edits saved");}catch(error){notify(error.message||"Could not copy. Select the message and copy it manually.");}finally{button.disabled=false;}return;
  }
  if(action==="regenerate"){
    if(busy)return;
    clearTimeout(saveTimer);
    const id=currentId,version=current.attempt,comments=$("#regen-comments").value,selected=selectedSources();
    if(current.available_sources?.length&&!selected.length){notify("Select at least one research source.");return;}
    busy=true;button.disabled=true;
    try{
      if($("#subject"))await saveEdits();
      if(currentId!==id)return;
      busy=false;
      return await mutate("regenerate",{comments,draft_version:version,selected_source_ids:current.available_sources?.length?selected:null});
    }catch(error){notify(error.message);}finally{busy=false;button.disabled=false;}
    return;
  }
  return mutate(action);
});
document.addEventListener("input",e=>{
  if(e.target.closest("#prospect-fields")&&!$("#prospect-fields").classList.contains("custom-fields")){selectCustomProspect(null,true);return;}
  if(e.target.id==="regen-comments"){localStorage.setItem("outreach-regen-"+currentId,e.target.value);return;}
  if(!["subject","body"].includes(e.target.id))return;
  const id=currentId,values={subject:$("#subject").value,body:$("#body").value,draft_version:current.attempt};
  localStorage.setItem("outreach-edit-"+id,JSON.stringify(values));
  if($("#save-status"))$("#save-status").textContent="Saving…";$("#edited-note").classList.toggle("visible",values.subject!==current.output.draft.subject||values.body!==current.output.draft.body);tick();
  clearTimeout(saveTimer);saveTimer=setTimeout(()=>saveEdits(id,values).catch(()=>{if(currentId===id&&$("#save-status"))$("#save-status").textContent="Not saved to server · retry Save edits";}),700);
});
document.addEventListener("change",e=>{if(e.target.matches("[data-draft-source]"))updateSourceSelection();});
window.addEventListener("hashchange",()=>{const hash=location.hash.slice(1);if(hash==="new"&&currentId)newView();else if(/^[a-f0-9]{32}$/.test(hash)&&hash!==currentId)openRun(hash);});
setInterval(async()=>{
  tick();if(pollBusy||busy)return;pollBusy=true;
  try{await history();$("#connection-message").hidden=true;if(currentId&&current?.status==="running"){
    const id=currentId,r=await api("/api/runs/"+id);if(id!==currentId)return;
    if(r.updated_at!==current.updated_at){const following=view===current.stage;current=r;if(following)view=r.stage;render();}
  }}catch{$("#connection-message").hidden=false;}finally{pollBusy=false;}
},1500);
(async()=>{try{const [config,demo]=await Promise.all([api("/api/config"),api("/api/demo")]);missing=config.missing;demoProspects=demo.prospects;productBrief=demo.product_brief;const hash=location.hash.slice(1);if(/^[a-f0-9]{32}$/.test(hash))await openRun(hash);else newView();}catch(e){$("#main").innerHTML=shell('<div class="warning">Could not connect to the workspace. Start the local server and refresh.</div>');}})();

document.addEventListener("keydown",e=>{
  if(!e.target.matches('[role="tab"][data-research-tab]'))return;
  const keys=Object.keys(areas),index=keys.indexOf(researchTab);
  let next;
  if(e.key==="ArrowRight")next=(index+1)%keys.length;
  else if(e.key==="ArrowLeft")next=(index+keys.length-1)%keys.length;
  else if(e.key==="Home")next=0;
  else if(e.key==="End")next=keys.length-1;
  else return;
  e.preventDefault();researchTab=keys[next];render();$("#tab-"+researchTab).focus();
});

'use strict';

const $ = (id) => document.getElementById(id);
const app = {view:'inbox', offset:0, limit:30, request:0, selected:null, savedNote:'', toastTimer:null};
const titles = {
  inbox:['Literature inbox','Included and review papers, excluding hidden items.'],
  review:['Needs review','Papers whose relevance still needs scientific assessment.'],
  rated:['Rated & annotated','Papers with a star rating or a saved note, including hidden items.'],
  hidden:['Hidden papers','Papers removed from the inbox without being deleted.'],
  all:['All stored papers','Every retained record, including rejected and hidden papers.']
};

function el(tag, className='', text=null) {
  const node=document.createElement(tag);
  if(className) node.className=className;
  if(text!==null) node.textContent=String(text);
  return node;
}
function textDate(date, precision) {
  if(!date) return 'Publication date unknown';
  const parts=date.split('-');
  const d=new Date(`${parts[0]}-${parts[1]||'01'}-${parts[2]||'01'}T00:00:00Z`);
  if(Number.isNaN(d.getTime())) return date;
  if(precision==='year'||parts.length===1) return `${parts[0]} · year only`;
  if(precision==='month'||parts.length===2) return `${d.toLocaleString('en-US',{month:'short',timeZone:'UTC'})} ${parts[0]} · month only`;
  return d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'});
}
function seenDate(value){
  if(!value) return 'Unknown';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?value:date.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});
}
function doiUrl(doi){return 'https://doi.org/'+encodeURIComponent(doi);}
function pill(text,kind=''){return el('span','pill '+kind,text);}
function notify(message){
  const toast=$('toast');toast.textContent=message;toast.hidden=false;
  clearTimeout(app.toastTimer);app.toastTimer=setTimeout(()=>{toast.hidden=true;},4500);
}
async function api(path,options={}){
  const response=await fetch(path,{cache:'no-store',...options});
  let body;
  try{body=await response.json();}catch{throw new Error('The server returned an invalid response.');}
  if(!response.ok) throw new Error(body.error||`HTTP ${response.status}`);
  return body;
}
function filters(){
  const p=new URLSearchParams({view:app.view,offset:String(app.offset),limit:String(app.limit),sort:$('sortSelect').value});
  for(const [id,key] of [['searchInput','search'],['journalSelect','journal'],['decisionSelect','decision'],['abstractSelect','abstract'],['readSelect','read'],['ratingSelect','rating'],['recentSelect','recent']]){
    const value=$(id).value.trim();if(value)p.set(key,value);
  }
  return p;
}
function updateJournalOptions(journals){
  const select=$('journalSelect'),current=select.value;
  const present=[...select.options].slice(1).map(x=>x.value);
  if(JSON.stringify(present)===JSON.stringify(journals))return;
  select.replaceChildren(new Option('All journals',''));
  journals.forEach(name=>select.add(new Option(name,name)));
  select.value=current;
}
function updateStats(stats){
  $('statTotal').textContent=stats.total;
  $('statReview').textContent=stats.review;
  $('statRated').textContent=stats.rated;
  for(const [key,val] of Object.entries(stats)){
    const target=document.querySelector(`[data-count="${key}"]`);
    if(target)target.textContent=val;
  }
}
function articleCard(paper){
  const card=el('article','paper-card');
  const top=el('div','paper-card-top');
  top.append(el('span','paper-journal',paper.journal||'Unknown journal'),el('span','dot-sep','·'),el('span','',textDate(paper.published_date,paper.published_date_precision)));
  if(paper.decision==='review')top.append(pill('REVIEW','review'));
  if(paper.decision==='reject')top.append(pill('REJECTED','reject'));
  if(paper.abstract_status==='missing')top.append(pill('NO ABSTRACT','missing'));
  if(paper.manually_added)top.append(pill('MANUAL','manual'));
  if(paper.acknowledged)top.append(pill('ACKNOWLEDGED','ack'));
  const title=el('button','paper-title',paper.title||paper.doi);
  title.type='button';title.addEventListener('click',()=>openDetail(paper.doi));
  const authorText=paper.authors.length>4?paper.authors.slice(0,4).join(', ')+', et al.':paper.authors.join(', ');
  const authors=el('p','paper-authors',authorText||'Authors unavailable');
  card.append(top,title,authors);
  if(paper.abstract)card.append(el('p','paper-abstract',paper.abstract));
  const bottom=el('div','paper-card-bottom');
  const doi=el('a','paper-doi',paper.doi);doi.href=doiUrl(paper.doi);doi.target='_blank';doi.rel='noopener noreferrer';doi.title='Open DOI in a new tab';
  const actions=el('div','paper-actions');
  if(paper.rating)actions.append(el('span','star-display','★'.repeat(paper.rating)+'☆'.repeat(5-paper.rating)));
  const ack=el('button','tiny-button'+(paper.acknowledged?' active':''),paper.acknowledged?'Acknowledged':'Acknowledge');
  ack.type='button';ack.setAttribute('aria-label',paper.acknowledged?'Mark paper unacknowledged':'Acknowledge paper');
  ack.addEventListener('click',()=>patchState(paper.doi,{acknowledged:!Boolean(paper.acknowledged)}));
  const hide=el('button','tiny-button',paper.hidden?'Unhide':'Hide');hide.type='button';
  hide.addEventListener('click',()=>patchState(paper.doi,{hidden:!Boolean(paper.hidden)}));
  actions.append(ack,hide);bottom.append(doi,actions);card.append(bottom);
  return card;
}
async function loadArticles(){
  const token=++app.request;
  $('articleList').setAttribute('aria-busy','true');
  try{
    const data=await api('/api/articles?'+filters().toString());
    if(token!==app.request)return;
    updateStats(data.stats);updateJournalOptions(data.journals);
    const list=$('articleList');list.replaceChildren();
    if(data.items.length){
      const fragment=document.createDocumentFragment();
      data.items.forEach(p=>fragment.append(articleCard(p)));
      list.append(fragment);
    }else{
      const empty=el('div','empty-state');empty.append(el('h3','', 'No papers match these filters'),el('p','', 'Try a different search, clear the filters, or open All papers to see rejected and hidden records.'));list.append(empty);
    }
    $('resultCount').textContent=data.total.toLocaleString();
    $('resultContext').textContent=data.total===1?' paper':' papers';
    const start=data.total?data.offset+1:0;
    const end=Math.min(data.total,data.offset+data.limit);
    $('pageLabel').textContent=`${start}–${end} of ${data.total}`;
    $('prevBtn').disabled=data.offset===0;
    $('nextBtn').disabled=data.offset+data.limit>=data.total;
  }catch(error){
    if(token!==app.request)return;
    const list=$('articleList');list.replaceChildren();
    const empty=el('div','empty-state');empty.append(el('h3','','Unable to load papers'),el('p','',error.message));list.append(empty);
    $('resultCount').textContent='Unavailable';notify(error.message);
  }finally{if(token===app.request)$('articleList').removeAttribute('aria-busy');}
}
function changeView(view){
  if(!titles[view])return;
  if(app.selected&&!closeDetail())return;
  app.view=view;app.offset=0;
  $('viewTitle').textContent=titles[view][0];$('viewDescription').textContent=titles[view][1];
  document.querySelectorAll('.view-link').forEach(btn=>{
    const active=btn.dataset.view===view;btn.classList.toggle('active',active);
    if(active)btn.setAttribute('aria-current','page');else btn.removeAttribute('aria-current');
  });
  loadArticles();
}
function detailDirty(){const note=$('detailNote');return Boolean(app.selected&&note&&note.value!==app.savedNote);}
function closeDetail(force=false){
  if(!force&&detailDirty()&&!window.confirm('Discard unsaved note changes?'))return false;
  $('detailPanel').hidden=true;$('detailBackdrop').hidden=true;
  $('detailPanel').setAttribute('aria-hidden','true');app.selected=null;app.savedNote='';
  return true;
}
async function openDetail(doi){
  if(app.selected&&!closeDetail())return;
  try{
    const paper=await api('/api/article?doi='+encodeURIComponent(doi));
    app.selected=doi;app.savedNote=paper.note||'';
    renderDetail(paper);$('detailPanel').hidden=false;$('detailBackdrop').hidden=false;
    $('detailPanel').setAttribute('aria-hidden','false');$('closeDetailBtn').focus();
  }catch(error){notify(error.message);}
}
function detailField(label,value){const box=el('div');box.append(el('span','',label),document.createTextNode(value||'Not provided'));return box;}
function renderDetail(paper){
  const body=$('detailContent');body.replaceChildren();
  const meta=el('div','detail-meta');
  meta.append(el('span','paper-journal',paper.journal||'Unknown journal'),el('span','','·'),el('span','',textDate(paper.published_date,paper.published_date_precision)));
  if(paper.decision!=='include')meta.append(pill(paper.decision.toUpperCase(),paper.decision));
  if(paper.manually_added)meta.append(pill('MANUALLY ADDED','manual'));
  body.append(meta,el('h2','',paper.title||paper.doi),el('p','detail-authors',paper.authors.join(', ')||'Authors unavailable'));
  const doi=el('a','detail-doi','Open published paper · '+paper.doi);doi.href=doiUrl(paper.doi);doi.target='_blank';doi.rel='noopener noreferrer';body.append(doi);
  const dates=el('div','detail-grid');
  dates.append(detailField('First discovered',seenDate(paper.first_seen_at||paper.added_at)),
               detailField('Publication precision',paper.published_date_precision||'Unknown'),
               detailField('Published online',paper.published_online||'Unknown'),
               detailField('Crossref record created',paper.metadata_created||'Unknown'),
               detailField('Abstract source',paper.abstract_source||'Not available'),
               detailField('Source',paper.source||'Unknown'));
  body.append(dates,el('div','detail-divider'),el('div','section-title','ABSTRACT'));
  if(paper.abstract)body.append(el('div','detail-abstract',paper.abstract));
  else body.append(el('div','missing-abstract','An abstract was not available from the collected metadata. Open the DOI link to check the publisher’s article page.'));
  body.append(el('div','detail-divider'),el('div','section-title','YOUR READING STATE'));
  const toggles=el('div','detail-actions');
  const ackLabel=el('label','checkline');const ack=el('input');ack.type='checkbox';ack.checked=Boolean(paper.acknowledged);
  ack.addEventListener('change',()=>patchState(paper.doi,{acknowledged:ack.checked}));
  ackLabel.append(ack,document.createTextNode('Acknowledged'));
  const hiddenLabel=el('label','checkline');const hidden=el('input');hidden.type='checkbox';hidden.checked=Boolean(paper.hidden);
  hidden.addEventListener('change',()=>patchState(paper.doi,{hidden:hidden.checked}));
  hiddenLabel.append(hidden,document.createTextNode('Hidden from inbox'));
  toggles.append(ackLabel,hiddenLabel);body.append(toggles,el('div','section-title','RATING'));
  const stars=el('div','detail-rating');
  for(let i=1;i<=5;i++){
    const star=el('button','rating-star'+(paper.rating>=i?' filled':''),'★');star.type='button';
    star.setAttribute('aria-label',`Rate ${i} out of 5 stars`);star.title=`${i} star${i===1?'':'s'}`;
    star.addEventListener('click',()=>patchState(paper.doi,{rating:i}));stars.append(star);
  }
  const clear=el('button','clear-rating','Clear rating');clear.type='button';clear.addEventListener('click',()=>patchState(paper.doi,{rating:null}));stars.append(clear);body.append(stars);
  const noteLabel=el('label','note-label','Private notes');noteLabel.htmlFor='detailNote';
  const note=el('textarea','note-textarea');note.id='detailNote';note.value=paper.note||'';note.placeholder='Add a summary, experimental observation, or follow-up question…';
  const noteFooter=el('div','note-footer');const saveState=el('span','save-state','Saved locally in SQLite');
  note.addEventListener('input',()=>{saveState.textContent=detailDirty()?'Unsaved changes':'Saved locally in SQLite';});
  const save=el('button','button button-primary','Save note');save.type='button';
  save.addEventListener('click',()=>patchState(paper.doi,{note:note.value}));
  noteFooter.append(saveState,save);body.append(noteLabel,note,noteFooter);
  body.append(el('div','detail-divider'));
  const del=el('button','danger-button','Delete this paper permanently');del.type='button';
  del.addEventListener('click',()=>deletePaper(paper));body.append(del);
  body.append(el('p','paper-authors','Deletion blocks automatic re-import. Manually adding the DOI again restores the paper, but not its deleted notes or rating.'));
}
async function patchState(doi,changes){
  const note=$('detailNote');
  if(app.selected===doi&&note&&detailDirty()&&!Object.hasOwn(changes,'note'))changes={...changes,note:note.value};
  try{
    const result=await api('/api/state',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({doi,...changes})});
    if(app.selected===doi){app.savedNote=result.article.note||'';renderDetail(result.article);}
    await loadArticles();
  }catch(error){notify('Could not save: '+error.message);}
}
async function deletePaper(paper){
  if(!window.confirm(`Permanently delete “${paper.title}”?\n\nThis removes saved notes and ratings and blocks automatic re-import. Hiding is reversible.`))return;
  try{
    await api('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({doi:paper.doi,confirm:true})});
    closeDetail(true);notify('Paper deleted. It will not be automatically re-imported.');await loadArticles();
  }catch(error){notify('Deletion failed: '+error.message);}
}
function resetFilters(){
  for(const id of ['searchInput','journalSelect','decisionSelect','abstractSelect','readSelect','ratingSelect','recentSelect'])$(id).value='';
  $('sortSelect').value='discovered';app.offset=0;loadArticles();
}
function init(){
  document.querySelectorAll('.view-link').forEach(btn=>btn.addEventListener('click',()=>changeView(btn.dataset.view)));
  let searchTimer;
  $('searchInput').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{app.offset=0;loadArticles();},250);});
  for(const id of ['journalSelect','decisionSelect','abstractSelect','readSelect','ratingSelect','recentSelect','sortSelect']){
    $(id).addEventListener('change',()=>{app.offset=0;loadArticles();});
  }
  $('resetFiltersBtn').addEventListener('click',resetFilters);
  $('prevBtn').addEventListener('click',()=>{app.offset=Math.max(0,app.offset-app.limit);loadArticles();window.scrollTo({top:0,behavior:'smooth'});});
  $('nextBtn').addEventListener('click',()=>{app.offset+=app.limit;loadArticles();window.scrollTo({top:0,behavior:'smooth'});});
  $('closeDetailBtn').addEventListener('click',()=>closeDetail());
  $('detailBackdrop').addEventListener('click',()=>closeDetail());
  document.addEventListener('keydown',(event)=>{if(event.key==='Escape'&&!$('doiDialog').open&&app.selected){event.preventDefault();closeDetail();}});
  window.addEventListener('beforeunload',(event)=>{if(detailDirty()){event.preventDefault();event.returnValue='';}});
  $('addDoiBtn').addEventListener('click',()=>{$('doiError').hidden=true;$('doiDialog').showModal();$('doiInput').focus();});
  $('closeDoiBtn').addEventListener('click',()=>$('doiDialog').close());
  $('cancelDoiBtn').addEventListener('click',()=>$('doiDialog').close());
  $('doiForm').addEventListener('submit',async(event)=>{
    event.preventDefault();const input=$('doiInput').value.trim();if(!input)return;
    const submit=$('submitDoiBtn');submit.disabled=true;submit.textContent='Looking up DOI…';$('doiError').hidden=true;
    try{
      const result=await api('/api/add-doi',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({doi:input})});
      $('doiDialog').close();$('doiInput').value='';resetFilters();changeView('inbox');notify('Paper added to the local library.');
      await openDetail(result.article.doi);
    }catch(error){$('doiError').textContent=error.message;$('doiError').hidden=false;}
    finally{submit.disabled=false;submit.textContent='Add to library';}
  });
  loadArticles();
}
init();

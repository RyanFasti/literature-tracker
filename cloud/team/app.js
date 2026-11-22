import {makeBackup, validateBackup, planImport, MAX_BACKUP_BYTES} from './backup.mjs';
import {filterResearchPapers, missingAbstract, metadataCsv, metadataRis} from './research.mjs';
import {CATEGORIES, categoriesFor, isManualCategories} from './categories.mjs';
import {defaultConfig,DEFAULT_KEYWORDS,validIssn,validateConfig,collectorExport} from './workspace-config.mjs';
import {articleKind,kindLabel} from './kinds.mjs';
'use strict';
const $ = id => document.getElementById(id);
const make = (tag, cls, text) => {const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=String(text);return e;};
const state={catalog:[],manual:[],annotations:new Map(),overrides:new Map(),shared:[],abstracts:new Map(),pending:new Map(),drafts:new Map(),view:'browse',filtered:[],offset:0,limit:25,workspaceConfig:defaultConfig()};
const SETTINGS_KEY='lt-display-v8';
let settings={theme:'light',defaultAbstract:false};
try{const saved=JSON.parse(localStorage.getItem(SETTINGS_KEY)||'{}');if(['light','dark'].includes(saved.theme))settings.theme=saved.theme;if(typeof saved.defaultAbstract==='boolean')settings.defaultAbstract=saved.defaultAbstract;}catch{}
function applySettings(){document.body.dataset.theme=settings.theme;document.querySelectorAll('input[name="theme"]').forEach(x=>x.checked=x.value===settings.theme);$('defaultAbstract').checked=settings.defaultAbstract;}
function rememberSettings(){try{localStorage.setItem(SETTINGS_KEY,JSON.stringify(settings));}catch{}applySettings();}
applySettings();
document.querySelectorAll('input[name="theme"]').forEach(x=>x.addEventListener('change',()=>{settings.theme=x.value;rememberSettings();saveWorkspaceSettings().catch(e=>message('Theme save failed: '+e.message));}));
$('defaultAbstract').addEventListener('change',()=>{settings.defaultAbstract=$('defaultAbstract').checked;rememberSettings();saveWorkspaceSettings().catch(e=>message('Preference save failed: '+e.message));});
function message(text){$('message').textContent=text||'';}
function dateLabel(p){const d=p.published_date;if(!d)return 'Date unknown';return d+(p.published_date_precision==='year'?' · year only':p.published_date_precision==='month'?' · month only':'');}
function doiUrl(doi){return 'https://doi.org/'+encodeURIComponent(doi);}
function annotation(doi){return state.annotations.get(doi)||{doi,rating:null,note:'',acknowledged:0,hidden:0,share_note:0};}
function authorLabel(p){const a=p.authors||[];return a.length>6?a.slice(0,6).join(', ')+' et al.':a.join(', ');}
async function request(path,options={}){
  const response=await fetch(path,{cache:'no-store',...options});let data;
  try{data=await response.json();}catch{throw new Error('Invalid server response (HTTP '+response.status+')');}
  if(!response.ok)throw new Error(data.error||'HTTP '+response.status);return data;
}
async function mutation(path,method,body){return request(path,{method,headers:{'Content-Type':'application/json','X-LT-Request':'1'},body:JSON.stringify(body)});}
async function loadAll(path){let offset=0;const items=[];for(let i=0;i<80;i++){
  const data=await request(path+'?offset='+offset);if(!Array.isArray(data.items))throw new Error('Invalid pagination response');
  items.push(...data.items);if(data.next_offset===null)return items;
  if(!Number.isInteger(data.next_offset)||data.next_offset<=offset)throw new Error('Invalid next page');offset=data.next_offset;
}throw new Error('Too many results; contact the workspace owner');}
function paperList(){const byDoi=new Map(state.catalog.map(p=>[p.doi,p]));for(const p of state.manual){if(!byDoi.has(p.doi))byDoi.set(p.doi,{...p,decision:'manual',authors:[],first_seen_at:p.created_at,personal:true});}return [...byDoi.values()];}
function keywordConfig(){const custom=state.workspaceConfig.topic_keywords;return JSON.stringify(custom)===JSON.stringify(DEFAULT_KEYWORDS)?null:custom;}
function tags(p){return categoriesFor(p,state.overrides.get(p.doi),state.abstracts.get(p.doi)?.abstract||'',keywordConfig());}
function filterPapers(){return filterResearchPapers(paperList(),state.annotations,{
  view:state.view,query:$('search').value,journal:$('journal').value,
  abstract:$('abstract').value,days:Number($('recency').value),sort:$('sort').value,
  category:$('category').value,showHidden:$('showHidden').checked,overrides:state.overrides,
  kind:$('kind').value,readStatus:$('readStatus').value,minRating:Number($('minRating').value),
  dateFrom:$('dateFrom').value,dateTo:$('dateTo').value,kindFor:articleKind,
  categoryFor:(p,override)=>categoriesFor(p,override,state.abstracts.get(p.doi)?.abstract||'',keywordConfig()),
  searchTextFor:p=>state.abstracts.get(p.doi)?.abstract||''
});}
function savePatch(doi,patch,rerender=true){
  const previous=annotation(doi);state.annotations.set(doi,{...previous,...patch,doi});
  if(rerender)render();
  const before=state.pending.get(doi)||Promise.resolve();
  const job=before.catch(()=>{}).then(async()=>{
    const a=annotation(doi);
    const data={doi,rating:a.rating??null,note:a.note||'',acknowledged:Boolean(a.acknowledged),hidden:Boolean(a.hidden),share_note:Boolean(a.share_note)};
    const result=await mutation('/api/annotation','PUT',data);
    // Keep newer optimistic changes made while this request was in flight.
    const latest=state.annotations.get(doi);if(latest)latest.updated_at=result.updated_at;
  });
  state.pending.set(doi,job);
  job.catch(e=>{message('Could not save changes for '+doi+': '+e.message+'. Reload to reconcile with cloud.');});
  job.finally(()=>{if(state.pending.get(doi)===job)state.pending.delete(doi);}).catch(()=>{});
  return job;
}
function starBar(p,interactive){
  const wrap=make('div','stars');wrap.setAttribute('aria-label','Rating');
  const rating=annotation(p.doi).rating;
  if(!interactive){wrap.append(make('span','rating-label','Rated'));wrap.append(make('span','', '★'.repeat(rating||0)));return wrap;}
  wrap.append(make('span','rating-label','Rate'));
  for(let n=1;n<=5;n++){
    const button=make('button','star'+(n<=rating?' selected':''),n<=rating?'★':'☆');
    button.type='button';button.title='Rate '+n+' star'+(n===1?'':'s');button.setAttribute('aria-label',button.title);
    button.addEventListener('click',()=>{savePatch(p.doi,{rating:annotation(p.doi).rating===n?null:n},false);const next=annotation(p.doi).rating;wrap.querySelectorAll('button').forEach((b,i)=>{b.classList.toggle('selected',i<next);b.textContent=i<next?'★':'☆';});message('Rating saved automatically.');});
    wrap.append(button);
  }
  return wrap;
}
function createAbstractDetails(p){
  const details=make('details','card-details abstract-details');
  details.append(make('summary','', 'Abstract'));
  const body=make('div','detail-body');details.append(body);
  const cached=state.abstracts.get(p.doi);
  if(cached)showAbstract(body,cached);
  else body.append(make('p','abstract-empty','Open to retrieve the abstract from its metadata provider.'));
  details.addEventListener('toggle',()=>{
    if(!details.open){delete details.dataset.lazy;return;}
    if(details.dataset.lazy!=='1')loadAbstract(p.doi,body);
  });
  const shouldOpen=state.view==='triage'||settings.defaultAbstract;
  if(shouldOpen){details.dataset.lazy='1';details.open=true;observeAbstract(details,p.doi,body);}
  return details;
}
function showAbstract(body,data){body.replaceChildren();if(data.abstract){body.append(make('div','',data.abstract),make('span','abstract-source','Source: '+data.source+' · On-demand metadata; verify with publisher.'));}
  else body.append(make('div','abstract-empty','Abstract unavailable from Crossref and OpenAlex. Open the DOI to read the publisher version.'));}
async function loadAbstract(doi,body){
  if(state.abstracts.has(doi)){showAbstract(body,state.abstracts.get(doi));return;}
  if(body.dataset.loading==='1')return;body.dataset.loading='1';body.replaceChildren(make('div','abstract-empty','Retrieving abstract…'));
  try{const data=await request('/api/abstract?doi='+encodeURIComponent(doi));state.abstracts.set(doi,data);if(body.isConnected)showAbstract(body,data);}
  catch(e){if(body.isConnected)body.replaceChildren(make('div','abstract-empty','Could not retrieve abstract: '+e.message));}
  finally{delete body.dataset.loading;}
}
let abstractObserver=null;
function observeAbstract(details,doi,body){
  if(!('IntersectionObserver' in window)){loadAbstract(doi,body);return;}
  if(!abstractObserver)abstractObserver=new IntersectionObserver(entries=>{
    for(const entry of entries)if(entry.isIntersecting){
      abstractObserver.unobserve(entry.target);
      delete entry.target.dataset.lazy;
      if(entry.target.open)loadAbstract(entry.target.dataset.doi,entry.target.querySelector('.detail-body'));
    }
  },{rootMargin:'400px'});
  details.dataset.doi=doi;abstractObserver.observe(details);
}
function categoryEditor(p){
  const wrap=make('details','category-editor');wrap.append(make('summary','', 'Edit topics'));
  const grid=make('div','category-grid');const current=new Set(tags(p));
  for(const category of CATEGORIES){const label=make('label');const box=make('input');box.type='checkbox';box.checked=current.has(category.id);box.value=category.id;
    box.addEventListener('change',async()=>{
      const selected=[...grid.querySelectorAll('input:checked')].map(x=>x.value);
      const old=state.overrides.get(p.doi);
      state.overrides.set(p.doi,{...old,doi:p.doi,categories_json:JSON.stringify(selected)});
      try{await mutation('/api/paper-categories','PUT',{doi:p.doi,categories:selected});message('Topics updated for '+p.doi);render();}
      catch(e){if(old)state.overrides.set(p.doi,old);else state.overrides.delete(p.doi);message('Could not update topics: '+e.message);render();}
    });label.append(box,make('span','',category.label));grid.append(label);}
  wrap.append(grid);const reset=make('button','outline','Reset to automatic suggestions');reset.type='button';reset.addEventListener('click',async()=>{
    const old=state.overrides.get(p.doi);state.overrides.set(p.doi,{...old,doi:p.doi,categories_json:null});
    try{await mutation('/api/paper-categories','PUT',{doi:p.doi,categories:null});message('Automatic topic suggestions restored.');render();}
    catch(e){if(old)state.overrides.set(p.doi,old);else state.overrides.delete(p.doi);message('Could not reset topics: '+e.message);render();}
  });wrap.append(reset);return wrap;
}
function kindEditor(p){
  const row=make('label','kind-editor','Correct article type');const select=make('select');
  for(const [value,label] of [['','Automatic (provisional)'],['research','Research article'],['review','Review article'],['unknown','Unknown']]){
    const option=make('option','',label);option.value=value;select.append(option);
  }
  select.value=state.overrides.get(p.doi)?.article_kind||'';
  select.addEventListener('change',async()=>{
    const before=state.overrides.get(p.doi);const kind=select.value||null;
    try{await mutation('/api/article-kind','PUT',{doi:p.doi,kind});state.overrides.set(p.doi,{...before,doi:p.doi,article_kind:kind});message('Article type correction saved.');render();}
    catch(e){message('Could not update article type: '+e.message);select.value=before?.article_kind||'';}
  });row.append(select);return row;
}
function metadataDetails(p){
  const details=make('details','card-details');details.append(make('summary','', 'More metadata & topics'));
  const body=make('div','detail-body');const dl=make('dl','metadata-grid');
  const rows=[['Authors',authorLabel(p)||'Not indexed'],['DOI',p.doi],['Publication date',dateLabel(p)],['Discovered',p.first_seen_at?.slice(0,10)||'Unknown'],['Article type evidence',articleKind(p,state.overrides.get(p.doi)).source],['Abstract indexed',missingAbstract(p)?'No':'Yes']];
  for(const [name,value] of rows){const item=make('div');item.append(make('dt','',name),make('dd','',value));dl.append(item);}body.append(dl);
  const link=make('a','doi-link','Open publisher record ↗');link.href=doiUrl(p.doi);link.target='_blank';link.rel='noopener noreferrer';body.append(link,kindEditor(p),categoryEditor(p));details.append(body);return details;
}
function noteEditor(p){const a=annotation(p.doi);const editor=make('div','note-editor');const label=make('label','','Private note · saves on blur');const input=make('textarea');input.maxLength=4000;input.value=state.drafts.has(p.doi)?state.drafts.get(p.doi):a.note||'';input.placeholder='Add a research note…';input.setAttribute('aria-label','Research note for '+(p.title||p.doi));
  input.addEventListener('input',()=>state.drafts.set(p.doi,input.value));
  function save(){const value=input.value;if(value!==annotation(p.doi).note){savePatch(p.doi,{note:value},false);message('Saving note…');}state.drafts.delete(p.doi);}
  input.addEventListener('blur',save);input.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();input.blur();}});
  editor.append(label,input,make('small','','Saved when you click outside the note or press Ctrl+Enter.'));
  const share=make('label','share-line');const box=make('input');box.type='checkbox';box.checked=Boolean(a.share_note);
  box.addEventListener('change',()=>{if(box.checked&&!input.value.trim()){box.checked=false;message('Write a note before sharing.');return;}savePatch(p.doi,{note:input.value,share_note:box.checked},false);message(box.checked?'Note shared with authorized users.':'Note made private.');});
  share.append(box,make('span','','Share this note with authorized colleagues (optional)'));editor.append(share);return editor;
}
function actionBar(p){const a=annotation(p.doi);const bar=make('div','card-actions');bar.append(starBar(p,true));
  const actions=make('div','action-group');const ack=make('button','quick-action acknowledge',a.acknowledged?'✓ Acknowledged':'✓ Acknowledge');ack.type='button';
  ack.addEventListener('click',()=>{savePatch(p.doi,{acknowledged:!Boolean(annotation(p.doi).acknowledged)},true);message('Reading status saved.');});
  const hide=make('button','quick-action'+(a.hidden?' is-on':''),a.hidden?'↶ Unhide':'◌ Hide');hide.type='button';
  hide.addEventListener('click',()=>{savePatch(p.doi,{hidden:!Boolean(annotation(p.doi).hidden)},true);message('Visibility saved.');});
  const remove=make('button','quick-action delete','Delete');remove.type='button';remove.title='Remove this paper and your saved annotation from your personal workspace';
  remove.addEventListener('click',async()=>{
    if(!confirm('Permanently remove this paper from YOUR research workspace? This deletes your saved note and rating and keeps the paper excluded from future catalog refreshes. The public catalog is not changed.'))return;
    remove.disabled=true;try{await (state.pending.get(p.doi)||Promise.resolve()).catch(()=>{});await mutation('/api/paper-delete','POST',{doi:p.doi});
      state.overrides.set(p.doi,{doi:p.doi,deleted_at:new Date().toISOString(),categories_json:null});state.annotations.delete(p.doi);state.manual=state.manual.filter(x=>x.doi!==p.doi);state.drafts.delete(p.doi);message('Paper removed from your workspace.');render();
    }catch(e){remove.disabled=false;message('Delete failed: '+e.message);}
  });actions.append(ack,hide,remove);bar.append(actions);return bar;
}
function paperCard(p){
  const a=annotation(p.doi),fast=state.view==='triage';const card=make('article','paper'+(fast?' triage-paper':''));card.dataset.doi=p.doi;
  const meta=make('div','paper-meta');meta.append(make('span','',p.journal||'Journal not specified'),make('span','meta-sep','·'),make('span','',dateLabel(p)));
  const kind=articleKind(p,state.overrides.get(p.doi));const kindBadge=make('span','pill article-kind '+kind.kind,kindLabel(kind));kindBadge.title='Type evidence: '+kind.source;meta.append(kindBadge);
  if(a.hidden)meta.append(make('span','pill manual-pill','Hidden'));if(a.acknowledged)meta.append(make('span','pill','Acknowledged'));
  const h=make('h2');const link=make('a','',p.title||p.doi);link.href=doiUrl(p.doi);link.target='_blank';link.rel='noopener noreferrer';h.append(link);card.append(meta,h);
  const tagrow=make('div','tag-row');const categories=tags(p);for(const id of categories){const c=CATEGORIES.find(x=>x.id===id);tagrow.append(make('span','tag'+(isManualCategories(state.overrides.get(p.doi))?' manual-tag':''),c?.label||id));}
  if(!categories.length)tagrow.append(make('span','tag','No topic labels'));if(missingAbstract(p))tagrow.append(make('span','tag','Abstract not indexed'));card.append(tagrow);
  if(!fast){if(a.note)card.append(make('p','note-preview',a.note));if(a.rating)card.append(starBar(p,false));}
  card.append(createAbstractDetails(p),metadataDetails(p));
  if(fast){card.append(actionBar(p),noteEditor(p));}
  else{const actions=make('details','card-details');actions.append(make('summary','','Review actions'));const body=make('div');body.append(actionBar(p),noteEditor(p));actions.append(body);card.append(actions);}
  return card;
}
function sharedCard(n){const card=make('article','shared-card');const h=make('h2');const link=make('a','',n.doi);link.href=doiUrl(n.doi);link.target='_blank';link.rel='noopener noreferrer';h.append(link);card.append(h,make('div','shared-meta','Shared by '+n.author+' · '+new Date(n.updated_at).toLocaleString()),make('p','',n.note));return card;}
const VIEW_TEXT={
  browse:['LITERATURE LIBRARY','Browse papers','Search, filter and organize research relevant to your work.'],
  triage:['YOUR REVIEW QUEUE','Fast review','Read new papers and clear your inbox in a single click.'],
  shared:['COLLABORATION','Shared','Notes explicitly shared by authorized researchers.'],
  settings:['PREFERENCES & COLLECTION','Settings','Customize appearance, topics, tracked journals, and collection rules.'],
};
function render(){
  const shared=state.view==='shared',isSettings=state.view==='settings';
  state.filtered=isSettings?[]:shared?state.shared:filterPapers();
  if(state.offset>=state.filtered.length)state.offset=Math.max(0,Math.floor((state.filtered.length-1)/state.limit)*state.limit);
  const list=$('papers');list.replaceChildren();if(!isSettings){for(const p of state.filtered.slice(state.offset,state.offset+state.limit))list.append(shared?sharedCard(p):paperCard(p));
    if(!state.filtered.length)list.append(make('div','empty',shared?'No shared notes yet.':state.view==='triage'?'Your review queue is clear.':'No papers match these filters.'));}
  $('resultCount').textContent=state.filtered.length.toLocaleString()+(shared?' shared notes':state.view==='triage'?' papers to review':' matching papers');
  $('page').textContent=(state.filtered.length?state.offset+1:0)+'–'+Math.min(state.filtered.length,state.offset+state.limit)+' of '+state.filtered.length;
  $('prev').disabled=state.offset===0;$('next').disabled=state.offset+state.limit>=state.filtered.length;
  for(const b of document.querySelectorAll('[data-view]'))b.classList.toggle('active',b.dataset.view===state.view);
  const [eyebrow,title,desc]=VIEW_TEXT[state.view];$('viewEyebrow').textContent=eyebrow;$('viewTitle').textContent=title;$('viewDescription').textContent=desc;
  $('settingsPage').hidden=!isSettings;
  $('filterPanel').hidden=state.view!=='browse';$('triageIntro').hidden=state.view!=='triage';
  $('papers').hidden=isSettings;$('resultCount').parentElement.hidden=isSettings;$('papers').nextElementSibling.hidden=isSettings;
  for(const item of document.querySelectorAll('.utility-section'))item.hidden=state.view!=='browse';
  $('refresh').hidden=isSettings;
  $('exportCsv').disabled=shared||isSettings||state.filtered.length===0;$('exportRis').disabled=shared||isSettings||state.filtered.length===0;
  $('unseenCount').textContent=paperList().filter(p=>!annotation(p.doi).acknowledged&&!annotation(p.doi).hidden&&!state.overrides.get(p.doi)?.deleted_at).length.toLocaleString();
}
function renderSettingsPage(){
  const config=state.workspaceConfig;
  const keywordRoot=$('keywordEditors');keywordRoot.replaceChildren();
  for(const c of CATEGORIES){
    const label=make('label','keyword-field',c.label);const input=make('textarea');
    input.rows=2;input.dataset.topic=c.id;input.value=(config.topic_keywords[c.id]||[]).join(', ');
    input.setAttribute('aria-label',c.label+' keyword phrases');label.append(input);keywordRoot.append(label);
  }
  const journalRoot=$('journalEditors');journalRoot.replaceChildren();
  for(const j of config.journals){
    const row=make('div','journal-row');row.dataset.journalId=j.id;
    const nameLabel=make('label','','Journal');const name=make('input','journal-name');name.value=j.name;name.maxLength=150;nameLabel.append(name);
    const issnLabel=make('label','','ISSN(s)');const issn=make('input','journal-issns');issn.value=j.issns.join(', ');issnLabel.append(issn);
    const remove=make('button','outline journal-remove','Remove');remove.type='button';
    remove.addEventListener('click',()=>{if(journalRoot.children.length===1){message('At least one journal is required.');return;}row.remove();});
    row.append(nameLabel,issnLabel,remove);journalRoot.append(row);
  }
  $('lookbackDays').value=config.collection.lookback_days;$('backfillYears').value=config.collection.backfill_years;
  applySettings();
}
function configFromForm(){
  const config=structuredClone(state.workspaceConfig);
  config.preferences={theme:settings.theme,default_abstract:settings.defaultAbstract};
  for(const textarea of $('keywordEditors').querySelectorAll('textarea[data-topic]')){
    const terms=textarea.value.split(/[,\n]/).map(x=>x.trim()).filter(Boolean);
    config.topic_keywords[textarea.dataset.topic]=terms;
  }
  config.journals=[...$('journalEditors').children].map(row=>({id:row.dataset.journalId,
    name:row.querySelector('.journal-name').value.trim(),
    issns:row.querySelector('.journal-issns').value.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean)}));
  config.collection={lookback_days:Number($('lookbackDays').value),backfill_years:Number($('backfillYears').value)};
  return validateConfig(config);
}
async function saveWorkspaceSettings(){
  const config=configFromForm();const result=await mutation('/api/workspace-settings','PUT',{config});
  state.workspaceConfig=config;settings={theme:config.preferences.theme,defaultAbstract:config.preferences.default_abstract};rememberSettings();
  // Tracked journals are searchable even before the next collector run.
  const current=$('journal').value;const names=[...new Set([...state.catalog,...state.manual].map(p=>p.journal).concat(config.journals.map(j=>j.name)).filter(Boolean))].sort();
  $('journal').replaceChildren(make('option','','All journals'));
  for(const name of names){const opt=make('option','',name);opt.value=name;$('journal').append(opt);}$('journal').value=current;
  message('Settings saved to your account. Collection changes require exporting and committing the configuration.');
  if(state.view!=='settings')render();return result;
}
$('saveWorkspaceSettings').addEventListener('click',async()=>{const button=$('saveWorkspaceSettings');button.disabled=true;try{await saveWorkspaceSettings();}catch(e){message('Settings not saved: '+e.message);}finally{button.disabled=false;}});
$('addJournalForm').addEventListener('submit',e=>{
  e.preventDefault();try{
    const draft=configFromForm();const name=$('newJournalName').value.trim();
    const issns=$('newJournalIssns').value.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean);
    if(!name||issns.length===0||issns.some(x=>!validIssn(x)))throw new Error('Enter a valid journal name and verified ISSN(s).');
    let id=name.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'').slice(0,55);
    if(!/^[a-z]/.test(id))id='journal_'+id;
    const base=id;let counter=2;while(draft.journals.some(j=>j.id===id))id=base+'_'+counter++;
    draft.journals.push({id,name,issns});validateConfig(draft);
    state.workspaceConfig=draft;renderSettingsPage();$('addJournalForm').reset();message('Journal added to your draft. Click Save settings, then export the collector configuration to activate ingestion.');
  }catch(error){message('Cannot add journal: '+error.message);}
});
$('resetWorkspaceSettings').addEventListener('click',async()=>{
  if(!confirm('Restore the six default journals, topic keywords, collection window, and display preferences for your account? This does not delete papers or annotations.'))return;
  const previous=state.workspaceConfig;state.workspaceConfig=defaultConfig();settings={theme:'light',defaultAbstract:false};renderSettingsPage();
  try{await saveWorkspaceSettings();message('Default settings restored. Existing papers and annotations were not changed.');}
  catch(e){state.workspaceConfig=previous;renderSettingsPage();message('Could not restore defaults: '+e.message);}
});
$('downloadCollectorConfig').addEventListener('click',()=>{
  try{const config=collectorExport(configFromForm());downloadMetadata(JSON.stringify(config,null,2)+'\n','literature-tracker-collector-config.json','application/json;charset=utf-8');
    message('Collector configuration downloaded. Apply it locally and commit the resulting config files; downloading alone does not change weekly ingestion.');}
  catch(e){message('Cannot export settings: '+e.message);}
});

async function init(){try{
  const me=await request('/api/me');$('account').textContent=me.authenticated?'@'+me.login:'Not signed in';$('logout').hidden=!me.authenticated;$('signIn').hidden=me.authenticated;$('workspace').hidden=!me.authenticated;if(!me.authenticated)return;
  const [catalog,annotations,manual,shared,overrides,workspace]=await Promise.all([request('/articles.json'),loadAll('/api/annotations'),loadAll('/api/manual-papers'),loadAll('/api/shared'),loadAll('/api/paper-overrides'),request('/api/workspace-settings')]);
  if(!Array.isArray(catalog.articles)||catalog.abstracts_included!==false)throw new Error('Unexpected catalog format');
  state.catalog=catalog.articles;state.annotations=new Map(annotations.map(a=>[a.doi,a]));state.manual=manual;state.shared=shared;state.overrides=new Map(overrides.map(o=>[o.doi,o]));
  state.workspaceConfig=validateConfig(workspace.config);settings={theme:workspace.config.preferences.theme,defaultAbstract:workspace.config.preferences.default_abstract};rememberSettings();renderSettingsPage();
  $('totalCount').textContent=state.catalog.length.toLocaleString();$('snapshot').textContent='Updated '+new Date(catalog.generated_at).toLocaleDateString();
  const journals=[...new Set([...state.catalog,...state.manual].map(p=>p.journal).concat(state.workspaceConfig.journals.map(j=>j.name)).filter(Boolean))].sort();for(const name of journals){const option=make('option','',name);option.value=name;$('journal').append(option);}
  for(const c of CATEGORIES){const option=make('option','',c.label);option.value=c.id;$('category').append(option);}render();
}catch(e){$('account').textContent='Connection error';$('signIn').hidden=false;$('workspace').hidden=true;const p=make('p','',e.message);$('signIn').append(p);}}
for(const b of document.querySelectorAll('[data-view]'))b.addEventListener('click',()=>{state.view=b.dataset.view;state.offset=0;message('');if(state.view==='settings')renderSettingsPage();render();window.scrollTo({top:0,behavior:'instant'});});
for(const id of ['search','journal','category','kind','readStatus','minRating','abstract','dateFrom','dateTo','recency','sort','showHidden'])$(id).addEventListener(id==='search'?'input':'change',()=>{state.offset=0;render();});
$('clearFilters').addEventListener('click',()=>{for(const id of ['search','journal','category','kind','readStatus','abstract','dateFrom','dateTo'])$(id).value='';$('minRating').value='0';$('recency').value='0';$('sort').value='discovered';$('showHidden').checked=false;state.offset=0;render();});
$('prev').addEventListener('click',()=>{state.offset=Math.max(0,state.offset-state.limit);render();});$('next').addEventListener('click',()=>{state.offset+=state.limit;render();});
$('refresh').addEventListener('click',async()=>{try{const [annotations,shared,overrides]=await Promise.all([loadAll('/api/annotations'),loadAll('/api/shared'),loadAll('/api/paper-overrides')]);state.annotations=new Map(annotations.map(a=>[a.doi,a]));state.shared=shared;state.overrides=new Map(overrides.map(o=>[o.doi,o]));message('Research state refreshed.');render();}catch(e){message('Refresh failed: '+e.message);}});
function downloadMetadata(content,filename,mime){const blob=new Blob([content],{type:mime});const url=URL.createObjectURL(blob);const link=make('a');link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('exportCsv').addEventListener('click',()=>{if(state.view==='shared')return;const papers=filterPapers();if(!papers.length)return;downloadMetadata(metadataCsv(papers),'literature-tracker-filtered-'+new Date().toISOString().slice(0,10)+'.csv','text/csv;charset=utf-8');message('Exported '+papers.length+' metadata records to CSV.');});
$('exportRis').addEventListener('click',()=>{if(state.view==='shared')return;const papers=filterPapers();if(!papers.length)return;downloadMetadata(metadataRis(papers),'literature-tracker-filtered-'+new Date().toISOString().slice(0,10)+'.ris','application/x-research-info-systems;charset=utf-8');message('Exported '+papers.length+' metadata records to RIS.');});
$('logout').addEventListener('click',async()=>{try{await mutation('/auth/logout','POST',{});window.location.href='/team/';}catch(e){message('Sign out failed: '+e.message);}});
$('manualForm').addEventListener('submit',async e=>{e.preventDefault();const button=e.currentTarget.querySelector('button[type=submit]');button.disabled=true;try{const data=await mutation('/api/manual-papers','POST',{doi:$('manualDoi').value,title:$('manualTitle').value,journal:$('manualJournal').value});state.manual=state.manual.filter(p=>p.doi!==data.paper.doi);state.manual.unshift(data.paper);$('manualForm').reset();state.view='browse';state.offset=0;message('Personal DOI saved.');render();}catch(err){message('Could not add DOI: '+err.message);}finally{button.disabled=false;}});
let pendingImport = null;
$('exportBackup').addEventListener('click', async () => {
  const button=$('exportBackup');button.disabled=true;
  try {
    // Back up server state, not a potentially stale browser snapshot.
    const [annotations,manual]=await Promise.all([
      loadAll('/api/annotations'),loadAll('/api/manual-papers')
    ]);
    const backup = makeBackup(annotations,manual);
    const blob = new Blob([JSON.stringify(backup, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = 'literature-tracker-private-backup-' + new Date().toISOString().slice(0,10) + '.json';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    message('Downloaded a fresh private backup of saved cloud records. Unsaved edits are not included.');
  } catch (e) {
    message('Backup failed; no file downloaded: '+e.message);
  } finally {button.disabled=false;}
});
$('importBackup').addEventListener('change', () => {
  pendingImport = null; $('importSummary').hidden = true; $('importConfirm').hidden = true;
});
$('importPreview').addEventListener('click', async () => {
  pendingImport = null; $('importConfirm').hidden = true;
  try {
    const file = $('importBackup').files?.[0];
    if (!file) throw new Error('Choose a JSON backup file first');
    if (file.size > MAX_BACKUP_BYTES) throw new Error('Backup exceeds 8 MiB; import a smaller backup');
    const raw = JSON.parse(await file.text());
    const validated = validateBackup(raw);
    pendingImport = planImport(validated, [...state.annotations.values()], state.manual);
    const total = pendingImport.annotations.length + pendingImport.manual_papers.length;
    $('importSummary').textContent =
      'Ready to add ' + pendingImport.annotations.length + ' annotations and ' +
      pendingImport.manual_papers.length + ' personal DOIs. Skipping ' +
      pendingImport.skippedAnnotations + ' existing annotations and ' +
      pendingImport.skippedManual + ' existing personal DOIs. ' +
      'All imported notes will be private' + (pendingImport.previouslyShared ?
      ' (including ' + pendingImport.previouslyShared + ' previously shared notes).' : '.') +
      ' Existing records will not be changed.';
    $('importSummary').hidden = false;
    $('importConfirm').hidden = total === 0;
    message(total ? 'Review the import summary, then confirm.' : 'Nothing new to import.');
  } catch (e) {
    $('importSummary').hidden = false; $('importSummary').textContent = 'Cannot import: ' + e.message;
    message('Import preview failed. No changes were made.');
  }
});
$('importConfirm').addEventListener('click', async () => {
  if (!pendingImport || !confirm('Import missing records into your personal cloud account? Existing records will not be overwritten. Imported notes will remain private.')) return;
  $('importConfirm').disabled = true;
  let addedAnnotations = 0, addedManual = 0;
  try {
    // API calls are per-record and idempotent on retry because we refresh in-memory state.
    for (const a of pendingImport.annotations) {
      const result = await mutation('/api/import/annotation', 'POST', a);
      if (result.created) {
        state.annotations.set(a.doi, {...a, acknowledged:Number(a.acknowledged), hidden:Number(a.hidden),
          share_note:0, updated_at:result.updated_at});
        addedAnnotations++;
      }
    }
    for (const p of pendingImport.manual_papers) {
      const result = await mutation('/api/import/manual-paper', 'POST', p);
      if (result.created) { state.manual.push(result.paper); addedManual++; }
    }
    message('Import complete: ' + addedAnnotations + ' annotations and ' + addedManual + ' personal DOIs added.');
  } catch (e) {
    message('Import interrupted after ' + addedAnnotations + ' annotations and ' + addedManual +
      ' personal DOIs. Some records were saved. Review the backup again to import only the remainder. Error: ' + e.message);
  } finally {
    pendingImport = null; $('importConfirm').disabled = false; $('importConfirm').hidden = true;
    $('importSummary').hidden = true; $('importBackup').value = ''; render();
  }
});
init();

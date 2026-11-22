'use strict';
const $=id=>document.getElementById(id);
const state={papers:[],filtered:[],offset:0,limit:40,abstracts:false};
const make=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=String(text);return e;};
function dateLabel(date,precision){if(!date)return 'Publication date unknown';if(precision==='year'||/^\d{4}$/.test(date))return date+' · year only';if(precision==='month'||/^\d{4}-\d{2}$/.test(date))return date+' · month only';return date;}
function authors(paper){const a=paper.authors||[];return a.length>6?a.slice(0,6).join(', ')+' et al.':a.join(', ');}
function articleKind(p){const t=String(p.type||'').toLowerCase(),title=p.title||'';if(/(?:^|[-_ ])review(?:$|[-_ ])/.test(t)||/\b(?:systematic|critical|comprehensive|mini|narrative|scoping|literature|a|an)\s+review\b|\breview\s+(?:of|on)\b|\bmeta-analysis\b/i.test(title))return 'review';if(t==='journal-article'||/(?:^|[-_ ])research(?:$|[-_ ])/.test(t))return 'research';return 'unknown';}
function render(){const q=$('search').value.trim().toLocaleLowerCase(),journal=$('journal').value,kind=$('kind').value,sort=$('sort').value;
state.filtered=state.papers.filter(p=>(!journal||p.journal===journal)&&(!kind||articleKind(p)===kind)&&(!q||[p.title,p.journal,p.doi,...(p.authors||[]),...(state.abstracts?[p.abstract||'']:[])].join(' ').toLocaleLowerCase().includes(q)));
if(sort==='published')state.filtered.sort((a,b)=>(b.published_date||'').localeCompare(a.published_date||'')||(b.first_seen_at||'').localeCompare(a.first_seen_at||''));
else if(sort==='title')state.filtered.sort((a,b)=>a.title.localeCompare(b.title));
else state.filtered.sort((a,b)=>(b.first_seen_at||'').localeCompare(a.first_seen_at||''));
if(state.offset>=state.filtered.length)state.offset=Math.max(0,Math.floor((state.filtered.length-1)/state.limit)*state.limit);
const list=$('articles');list.replaceChildren();for(const p of state.filtered.slice(state.offset,state.offset+state.limit)){
const card=make('article','paper');const meta=make('div','paper-meta');meta.append(make('span','',p.journal||'Unknown journal'),make('span','',dateLabel(p.published_date,p.published_date_precision)),make('span','pill '+articleKind(p),articleKind(p)==='review'?'Review article':articleKind(p)==='research'?'Research article · provisional':'Article type unknown'));
const h=make('h2');const a=make('a','',p.title||p.doi);a.href='https://doi.org/'+encodeURIComponent(p.doi);a.target='_blank';a.rel='noopener noreferrer';h.append(a);card.append(meta,h);
if(Array.isArray(p.topics)&&p.topics.length){const row=make('div','topic-row');const labels={'solar-cell':'Solar cell','photodetector':'Photodetector','led':'Light emitter','film':'Film / processing','stability':'Stability','fundamentals':'Materials science','other-devices':'Other devices'};
for(const id of p.topics)if(labels[id])row.append(make('span','topic-chip',labels[id]));card.append(row);}
card.append(make('p','authors',authors(p)));
if(state.abstracts&&p.abstract)card.append(make('p','abstract',p.abstract));
card.append(make('a','doi','DOI: '+p.doi));const link=card.lastElementChild;link.href='https://doi.org/'+encodeURIComponent(p.doi);link.target='_blank';link.rel='noopener noreferrer';list.append(card);
}
if(!state.filtered.length)list.append(make('div','empty','No papers match the selected filters.'));
$('resultCount').textContent=state.filtered.length.toLocaleString()+' matching papers';$('page').textContent=(state.filtered.length?state.offset+1:0)+'–'+Math.min(state.filtered.length,state.offset+state.limit)+' of '+state.filtered.length;
$('prev').disabled=state.offset===0;$('next').disabled=state.offset+state.limit>=state.filtered.length;
}
async function init(){try{const response=await fetch('./articles.json',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);const data=await response.json();if(!Array.isArray(data.articles))throw new Error('Invalid catalog format');state.papers=data.articles;state.abstracts=Boolean(data.abstracts_included);$('totalCount').textContent=state.papers.length.toLocaleString();$('snapshot').textContent='Snapshot: '+new Date(data.generated_at).toLocaleDateString();const journals=[...new Set(state.papers.map(p=>p.journal).filter(Boolean))].sort();for(const journal of journals){const opt=document.createElement('option');opt.value=journal;opt.textContent=journal;$('journal').append(opt);}render();}catch(err){$('resultCount').textContent='Catalog unavailable';$('articles').append(make('div','empty','Unable to load articles.json: '+err.message));}}
for(const id of ['search','journal','kind','sort'])$(id).addEventListener(id==='search'?'input':'change',()=>{state.offset=0;render();});
$('prev').addEventListener('click',()=>{state.offset=Math.max(0,state.offset-state.limit);render();});$('next').addEventListener('click',()=>{state.offset+=state.limit;render();});init();

/** Personal literature-review utilities. Pure, metadata-only; no notes are exported. */
export function missingAbstract(paper) {
  return paper.abstract_status !== 'available';
}

/** Date filtering uses interval overlap for year/month-only metadata, not fabricated days. */
export function publicationInterval(p) {
  const d=p.published_date||'';
  if(!/^\d{4}(?:-\d{2})?(?:-\d{2})?$/.test(d))return null;
  if(d.length===4)return [d+'-01-01',d+'-12-31'];
  if(d.length===7){const [y,m]=d.split('-').map(Number);if(m<1||m>12)return null;return [d+'-01',d+'-'+String(new Date(Date.UTC(y,m,0)).getUTCDate()).padStart(2,'0')];}
  return [d,d];
}
function queryTerms(query){return [...query.matchAll(/"([^"]+)"|(\S+)/g)].map(m=>(m[1]||m[2]).toLocaleLowerCase());}
export function filterResearchPapers(papers, annotations, options = {}) {
  const {view='all',query='',journal='',decision='',abstract='',days=0,sort='discovered',category='',showHidden=false,
    kind='',readStatus='',minRating=0,dateFrom='',dateTo='',kindFor=()=>({kind:'unknown'}),
    overrides=new Map(),categoryFor=()=>[],now=new Date(),searchTextFor=()=>''} = options;
  const terms=queryTerms(query.trim());
  const cutoff=Number(days)>0?new Date(now).getTime()-Number(days)*86400000:null;
  const result=papers.filter(p=>{
    const a=annotations.get(p.doi)||{};
    const override=overrides.get(p.doi);
    if(override?.deleted_at)return false;
    if(category&&!categoryFor(p,override).includes(category))return false;
    if(view==='triage'&&(a.hidden||a.acknowledged))return false;
    if(['browse','rated','review'].includes(view)&&!showHidden&&a.hidden&&readStatus!=='hidden')return false;
    if(journal&&p.journal!==journal)return false;
    if(decision&&(p.personal?'manual':p.decision)!==decision)return false;
    if(kind&&kindFor(p,override).kind!==kind)return false;
    if(abstract==='missing'&&!missingAbstract(p))return false;
    if(abstract==='available'&&missingAbstract(p))return false;
    if(Number(minRating)>0&&Number(a.rating||0)<Number(minRating))return false;
    if(readStatus==='unseen'&&a.acknowledged)return false;
    if(readStatus==='acknowledged'&&!a.acknowledged)return false;
    if(readStatus==='rated'&&!(a.rating>=1))return false;
    if(readStatus==='annotated'&&!String(a.note||'').trim())return false;
    if(readStatus==='hidden'&&!a.hidden)return false;
    if(dateFrom||dateTo){const interval=publicationInterval(p);if(!interval)return false;if(dateFrom&&interval[1]<dateFrom)return false;if(dateTo&&interval[0]>dateTo)return false;}
    if(cutoff!==null){const seen=Date.parse(p.first_seen_at||'');if(!Number.isFinite(seen)||seen<cutoff)return false;}
    if(terms.length){const haystack=[p.title,p.journal,p.doi,...(p.authors||[]),searchTextFor(p)].join(' ').toLocaleLowerCase();if(!terms.every(term=>haystack.includes(term)))return false;}
    if(view==='inbox')return !a.hidden&&!a.acknowledged;
    if(view==='review')return !a.hidden&&p.decision==='review';
    if(view==='rated')return a.rating!=null||Boolean(a.note);
    if(view==='hidden')return Boolean(a.hidden);
    return true;
  });
  if(sort==='title')result.sort((a,b)=>(a.title||'').localeCompare(b.title||'')||a.doi.localeCompare(b.doi));
  else if(sort==='published')result.sort((a,b)=>(b.published_date||'').localeCompare(a.published_date||'')||(b.first_seen_at||'').localeCompare(a.first_seen_at||''));
  else if(sort==='rating')result.sort((a,b)=>Number(annotations.get(b.doi)?.rating||0)-Number(annotations.get(a.doi)?.rating||0)||(b.first_seen_at||'').localeCompare(a.first_seen_at||''));
  else result.sort((a,b)=>(b.first_seen_at||'').localeCompare(a.first_seen_at||'')||a.doi.localeCompare(b.doi));
  return result;
}

function safeCell(value) {
  let text = String(value ?? '').replace(/\r\n?/g, '\n');
  // Prevent spreadsheet formula evaluation of untrusted bibliographic metadata.
  if (/^[\s\u0000-\u001f]*[=+\-@]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g,'""') + '"';
}

export function metadataCsv(papers) {
  const rows = [['doi','title','journal','authors','published_date','date_precision','abstract_status','first_seen_at']];
  for (const p of papers) rows.push([
    p.doi, p.title||'', p.journal||'', (p.authors||[]).join('; '),
    p.published_date||'', p.published_date_precision||'',
    p.abstract_status||'unknown', p.first_seen_at||''
  ]);
  return '\uFEFF' + rows.map(row=>row.map(safeCell).join(',')).join('\r\n') + '\r\n';
}

function risValue(value) {
  return String(value ?? '').replace(/[\r\n\u2028\u2029]+/g,' ').replace(/\s+/g,' ').trim();
}

/** RIS can be imported into Zotero, EndNote and other citation managers. */
export function metadataRis(papers) {
  const lines=[];
  for (const p of papers) {
    lines.push('TY  - JOUR');
    if (p.title) lines.push('TI  - '+risValue(p.title));
    if (p.journal) lines.push('JO  - '+risValue(p.journal));
    for (const author of p.authors||[]) if (author) lines.push('AU  - '+risValue(author));
    const year = /^\d{4}/.exec(p.published_date||'');
    if (year) lines.push('PY  - '+year[0]);
    if (p.published_date_precision === 'day' && /^\d{4}-\d{2}-\d{2}$/.test(p.published_date||''))
      lines.push('DA  - '+p.published_date.replace(/-/g,'/'));
    lines.push('DO  - '+risValue(p.doi));
    lines.push('UR  - https://doi.org/'+encodeURIComponent(p.doi));
    lines.push('ER  - ', '');
  }
  return lines.join('\r\n');
}

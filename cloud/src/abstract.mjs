/** Authenticated, on-demand fallback for public metadata providers.
 *  This does not replace the collector's abstract-before-classification pass.
 *  Never exposes secrets, stores abstracts in public assets, or uses Elsevier
 *  API credentials server-side without verifying content rights.
 */
const LIMIT = 12000;
const TIMEOUT_MS = 8000;
const MAX_RESPONSE_BYTES = 500000;

function decodeEntities(s) {
  const named={amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:' ',ndash:'–',mdash:'—',minus:'−',hellip:'…',times:'×',alpha:'α',beta:'β',gamma:'γ',mu:'μ',sup2:'²',sup3:'³'};
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z][a-z0-9]+);/gi, (whole,token)=>{
    if(token[0]!=='#')return named[token.toLowerCase()]??whole;
    const n=token[1]?.toLowerCase()==='x'?parseInt(token.slice(2),16):parseInt(token.slice(1),10);
    return Number.isInteger(n)&&n>0&&n<=0x10ffff&&!(n>=0xd800&&n<=0xdfff)?String.fromCodePoint(n):' ';
  });
}
export function normalizeAbstractText(s) {
  if(typeof s!=='string')return '';
  return s.replace(/[\u00a0\u200b\u2060]/g,' ')
    .replace(/\s+/g,' ')
    .replace(/\s+([,.;:!?%)\]}])/g,'$1')
    .replace(/([([{])\s+/g,'$1')
    .replace(/\b([A-Za-z]{2,})\s+-\s+([A-Za-z]{2,})\b/g,'$1-$2')
    .trim().replace(/^abstract\s*(?::|[–—-]|\.)\s*/i,'')
    .trim().slice(0,LIMIT);
}
export function plainAbstract(raw) {
  if(typeof raw!=='string')return '';
  const text=raw.replace(/^\s*Abstract\s*[\r\n]+/i,' ')
    .replace(/<\s*(?:jats:)?title\b[^>]*>\s*Abstract\s*<\s*\/\s*(?:jats:)?title\s*>/gi,' ')
    .replace(/<\s*(?:script|style)\b[^>]*>[\s\S]*?<\s*\/\s*(?:script|style)\s*>/gi,' ')
    .replace(/<\/?\s*(?:jats:)?(?:p|sec|abstract|div|br|h[1-6])\b[^>]*>/gi,' ')
    .replace(/<[^>]*>/g,'');
  return normalizeAbstractText(decodeEntities(text));
}
export function openAlexAbstract(inverted) {
  if(!inverted||typeof inverted!=='object'||Array.isArray(inverted))return '';
  const entries=[];
  for(const [word,positions] of Object.entries(inverted)){
    if(!Array.isArray(positions)||word.length>80)return '';
    for(const pos of positions){if(!Number.isInteger(pos)||pos<0||pos>=5000)return '';entries.push([pos,word]);if(entries.length>5000)return '';}
  }
  entries.sort((a,b)=>a[0]-b[0]);
  if(entries.some((e,i)=>e[0]!==i))return '';
  return normalizeAbstractText(entries.map(e=>e[1]).join(' '));
}
async function jsonFromProvider(url,headers={}){
  const response=await fetch(url,{headers:{Accept:'application/json',...headers},signal:AbortSignal.timeout(TIMEOUT_MS)});
  if(!response.ok)return null;
  if(Number(response.headers.get('content-length')||0)>MAX_RESPONSE_BYTES)return null;
  const text=await response.text();if(text.length>MAX_RESPONSE_BYTES)return null;
  return JSON.parse(text);
}
function sameDoi(a,b){
  const canonical=s=>String(s||'').replace(/^https?:\/\/(?:dx\.)?doi\.org\//i,'').toLowerCase();
  return canonical(a)===canonical(b);
}
export async function fetchAbstract(doi,env={}){
  try{
    const crossref=await jsonFromProvider('https://api.crossref.org/works/'+encodeURIComponent(doi),{'User-Agent':'LiteratureTracker/1.0 (private research metadata lookup)'});
    if(sameDoi(crossref?.message?.DOI,doi)){
      const abstract=plainAbstract(crossref?.message?.abstract);
      if(abstract)return {abstract,source:'Crossref'};
    }
  }catch{}
  try{
    const url=new URL('https://api.openalex.org/works/https://doi.org/'+encodeURIComponent(doi));
    if(env.OPENALEX_API_KEY)url.searchParams.set('api_key',env.OPENALEX_API_KEY);
    const work=await jsonFromProvider(url.toString());
    if(sameDoi(work?.doi,doi)){
      const abstract=openAlexAbstract(work?.abstract_inverted_index);
      if(abstract)return {abstract,source:'OpenAlex'};
    }
  }catch{}
  try{
    const headers=env.SEMANTIC_SCHOLAR_API_KEY?{'x-api-key':env.SEMANTIC_SCHOLAR_API_KEY}:{};
    const url='https://api.semanticscholar.org/graph/v1/paper/'+encodeURIComponent('DOI:'+doi)+'?fields=abstract,externalIds';
    const work=await jsonFromProvider(url,headers);
    if(sameDoi(work?.externalIds?.DOI,doi)){
      const abstract=plainAbstract(work?.abstract);
      if(abstract)return {abstract,source:'Semantic Scholar'};
    }
  }catch{}
  try{
    const url=new URL('https://www.ebi.ac.uk/europepmc/webservices/rest/search');
    url.searchParams.set('query','DOI:'+doi);
    url.searchParams.set('format','json');
    url.searchParams.set('resultType','core');
    url.searchParams.set('pageSize','5');
    const works=await jsonFromProvider(url.toString());
    const match=works?.resultList?.result?.find(w=>sameDoi(w?.doi,doi));
    const abstract=plainAbstract(match?.abstractText);
    if(abstract)return {abstract,source:'Europe PMC'};
  }catch{}
  return {abstract:null,source:null};
}

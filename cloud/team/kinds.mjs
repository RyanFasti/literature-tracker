/** Article kind is not scientific relevance. Crossref journal-article alone cannot establish subtype. */
export const KINDS=['research','review','unknown'];
export function articleKind(p,override){
 if(KINDS.includes(override?.article_kind))return {kind:override.article_kind,source:'manual'};
 const type=String(p.type||'').toLowerCase();
 if(/(?:^|[-_ ])review(?:$|[-_ ])/.test(type))return {kind:'review',source:'publisher metadata'};
 if(/(?:^|[-_ ])research(?:$|[-_ ])/.test(type))return {kind:'research',source:'publisher metadata'};
 const title=String(p.title||'');
 if(/\b(?:systematic|critical|comprehensive|mini|narrative|scoping|literature|a|an)\s+review\b|\breview\s+(?:of|on)\b|\bmeta-analysis\b/i.test(title))return {kind:'review',source:'title inference'};
 if(type==='journal-article')return {kind:'research',source:'provisional; journal-article metadata'};
 return {kind:'unknown',source:'not established'};
}
export function kindLabel(result){
 if(result.kind==='review')return result.source==='title inference'?'Review article · inferred':'Review article';
 if(result.kind==='research')return result.source.startsWith('provisional')?'Research article · provisional':'Research article';
 return 'Article type unknown';
}

/** Per-user settings; server validates the same strict schema before D1 writes. */
export const TOPIC_IDS=['solar-cell','photodetector','led','film','stability','fundamentals','other-devices'];
export const DEFAULT_KEYWORDS={
 'solar-cell':['solar cell','solar cells','photovoltaic','photovoltaics','solar module','solar modules','PV device'],
 'photodetector':['photodetector','photodetectors','photodiode','photodiodes','photoconductor','photoconductors','phototransistor','light detector'],
 'led':['light-emitting diode','light emitting diode','LED','LEDs','electroluminescence','light emitter'],
 'film':['thin film','thin films','film formation','spin coating','spin-coating','blade coating','slot-die','crystallization','grain boundary','deposition','solution processing'],
 'stability':['stability','degradation','degrade','aging','ageing','durability','decomposition','operational lifetime','ion migration'],
 'fundamentals':['exciton','phonon','bandgap','band gap','defect physics','carrier dynamics','charge transport','crystal structure','phase transition'],
 'other-devices':['transistor','memristor','laser','sensor','scintillator','x-ray detector','X ray detector'],
};
export const DEFAULT_JOURNALS=[
 {id:'nature_communications',name:'Nature Communications',issns:['2041-1723']},
 {id:'science_advances',name:'Science Advances',issns:['2375-2548']},
 {id:'acs_energy_letters',name:'ACS Energy Letters',issns:['2380-8195']},
 {id:'advanced_materials',name:'Advanced Materials',issns:['1521-4095','0935-9648']},
 {id:'chemical_engineering_journal',name:'Chemical Engineering Journal',issns:['1385-8947','1873-3212']},
 {id:'ees',name:'Energy & Environmental Science',issns:['1754-5706','1754-5692']},
];
export function defaultConfig(){return {
 schema_version:1,
 preferences:{theme:'light',default_abstract:false},
 topic_keywords:structuredClone(DEFAULT_KEYWORDS),
 journals:structuredClone(DEFAULT_JOURNALS),
 collection:{lookback_days:45,backfill_years:3},
};}
export function validIssn(value){
 if(typeof value!=='string'||!/^\d{4}-\d{3}[\dX]$/.test(value))return false;
 const digits=value.replace('-','');const sum=[8,7,6,5,4,3,2].reduce((acc,w,i)=>acc+Number(digits[i])*w,0);
 const check=(11-(sum%11))%11;
 return digits[7]===(check===10?'X':String(check));
}
export function validateConfig(value){
 const bad=(message)=>{throw new Error(message);};
 if(!value||typeof value!=='object'||Array.isArray(value)||
   Object.keys(value).sort().join('|')!=='collection|journals|preferences|schema_version|topic_keywords'||value.schema_version!==1)bad('Invalid settings format');
 const p=value.preferences;
 if(!p||Object.keys(p).sort().join('|')!=='default_abstract|theme'||!['light','dark'].includes(p.theme)||typeof p.default_abstract!=='boolean')bad('Invalid display settings');
 const c=value.collection;
 if(!c||Object.keys(c).sort().join('|')!=='backfill_years|lookback_days'||!Number.isInteger(c.lookback_days)||c.lookback_days<7||c.lookback_days>120||!Number.isInteger(c.backfill_years)||c.backfill_years<1||c.backfill_years>5)bad('Invalid collection window');
 const keywords=value.topic_keywords;
 if(!keywords||typeof keywords!=='object'||Array.isArray(keywords)||Object.keys(keywords).sort().join('|')!==[...TOPIC_IDS].sort().join('|'))bad('Invalid topic keyword categories');
 for(const [id,terms] of Object.entries(keywords)){
   if(!Array.isArray(terms)||terms.length>35||terms.some(t=>typeof t!=='string'||!t.trim()||t.length>65||/[<>\r\n]/.test(t))||new Set(terms.map(t=>t.toLowerCase())).size!==terms.length)bad('Invalid keywords for '+id);
 }
 if(!Array.isArray(value.journals)||value.journals.length<1||value.journals.length>40)bad('Invalid journal count');
 const ids=new Set(),issns=new Set();
 for(const j of value.journals){
   if(!j||Object.keys(j).sort().join('|')!=='id|issns|name'||typeof j.id!=='string'||!(/^[a-z][a-z0-9_]{1,63}$/.test(j.id))||ids.has(j.id)||typeof j.name!=='string'||!j.name.trim()||j.name.length>150||/[<>]/.test(j.name)||!Array.isArray(j.issns)||j.issns.length<1||j.issns.length>8)bad('Invalid journal entry');
   ids.add(j.id);
   for(const issn of j.issns){if(!validIssn(issn)||issns.has(issn))bad('Invalid or duplicate ISSN: '+issn);issns.add(issn);}
 }
 return value;
}
export function matchesKeywords(text,keywords){
 const input=String(text||'').toLocaleLowerCase();
 return keywords.some(term=>{
   const escaped=term.toLocaleLowerCase().replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
   return new RegExp('(^|[^\\p{L}\\p{N}])'+escaped+'(?=$|[^\\p{L}\\p{N}])','iu').test(input);
 });
}
export function collectorExport(config){
 validateConfig(config);
 return {schema_version:1,journals:config.journals.map(j=>({...j,crossref_issns:j.issns})),topic_keywords:config.topic_keywords,collection:config.collection};
}

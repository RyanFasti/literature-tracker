import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/worker.mjs';
import {plainAbstract,openAlexAbstract} from '../src/abstract.mjs';
import {defaultConfig,validateConfig,validIssn,matchesKeywords,collectorExport} from '../team/workspace-config.mjs';
import {articleKind,kindLabel} from '../team/kinds.mjs';
import {filterResearchPapers,publicationInterval} from '../team/research.mjs';

const BASE='https://literature-tracker-team.example.workers.dev';
class SettingsDB {
  constructor(){this.settings=new Map();this.overrides=new Map();this.users=new Map([[1,{github_id:1,login:'RyanFasti'}],[2,{github_id:2,login:'Colleague'}]]);this.sessions=new Map();}
  prepare(sql){const db=this;return {bind(...args){return {
    async first(){
      if(sql.includes('FROM sessions s JOIN users u')){const s=db.sessions.get(args[0]);return s&&s.expires_at>args[1]?db.users.get(s.github_id):null;}
      if(sql.startsWith('SELECT config_json'))return db.settings.get(args[0])||null;
      if(sql.startsWith('SELECT deleted_at FROM paper_overrides'))return db.overrides.get(args[0]+'|'+args[1])||null;
      throw Error('Unexpected first '+sql);
    },
    async run(){
      if(sql.startsWith('INSERT INTO workspace_settings'))db.settings.set(args[0],{config_json:args[1],updated_at:args[2]});
      else if(sql.startsWith('INSERT INTO paper_overrides'))db.overrides.set(args[0]+'|'+args[1],{article_kind:args[2],updated_at:args[3],deleted_at:null});
      else throw Error('Unexpected run '+sql);
      return {success:true,meta:{changes:1}};
    },
    async all(){return {results:[]};}
  };}};}
}
async function session(db,id){const token=String(id).repeat(64);const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token));const key=Array.from(new Uint8Array(hash),x=>x.toString(16).padStart(2,'0')).join('');db.sessions.set(key,{github_id:id,expires_at:Math.floor(Date.now()/1000)+3600});return 'lt_session='+token;}
const env=()=>({DB:new SettingsDB(),PUBLIC_BASE_URL:BASE,ALLOWED_GITHUB_USERS:'RyanFasti,Colleague',ASSETS:{fetch:async()=>new Response('static')}});
const get=(path,cookie)=>new Request(BASE+path,{headers:{Cookie:cookie}});
const put=(path,body,cookie)=>new Request(BASE+path,{method:'PUT',headers:{Cookie:cookie,Origin:BASE,'Content-Type':'application/json','X-LT-Request':'1'},body:JSON.stringify(body)});

test('abstract cleanup removes duplicate heading, false linebreaks and punctuation gaps',()=>{
  const input='<jats:sec><jats:title>Abstract</jats:title><jats:p>Lead<jats:italic>-</jats:italic>halide ( <jats:italic>FAPbI3</jats:italic> ) devices.\n Efficiency <jats:sup>2</jats:sup> increases.</jats:p></jats:sec>';
  assert.equal(plainAbstract(input),'Lead-halide (FAPbI3) devices. Efficiency 2 increases.');
  assert.equal(plainAbstract('<p>Abstract: Solar-cell stability.</p>'),'Solar-cell stability.');
  assert.equal(plainAbstract('<title>Abstract</title><p>Study of thin films.</p>'),'Study of thin films.');
  assert.equal(plainAbstract('<p>Decay &ndash; stability &amp; efficiency.</p>'),'Decay – stability & efficiency.');
  assert.equal(openAlexAbstract({Perovskite:[0],devices:[1],',':[2],stability:[3],')':[4]}),'Perovskite devices, stability)');
});
test('journal ISSN validation and per-user configuration enforce strict schema',()=>{
  const config=defaultConfig();assert.equal(validateConfig(config),config);
  assert.equal(validIssn('2041-1723'),true);assert.equal(validIssn('2041-1724'),false);
  const clone=structuredClone(config);clone.journals.push({...clone.journals[0],id:'duplicate'});
  assert.throws(()=>validateConfig(clone),/duplicate ISSN/i);
  const malicious=structuredClone(config);malicious.extra='private notes';assert.throws(()=>validateConfig(malicious));
  const exported=collectorExport(config);assert.deepEqual(Object.keys(exported).sort(),['collection','journals','schema_version','topic_keywords']);
  assert.equal(matchesKeywords('Perovskite solar cells',config.topic_keywords['solar-cell']),true);
  assert.equal(matchesKeywords('Interfacial engineering',config.topic_keywords['solar-cell']),false);
});
test('review vs research article distinction is separate from classifier decision and can be corrected',()=>{
  const review={title:'A comprehensive review of halide perovskite devices',type:'journal-article',decision:'include'};
  assert.equal(articleKind(review).kind,'review');assert.equal(articleKind(review).source,'title inference');
  const research={title:'Photovoltaic devices with improved stability',type:'journal-article',decision:'review'};
  assert.equal(articleKind(research).kind,'research');assert.match(articleKind(research).source,/provisional/);
  assert.equal(articleKind(research,{article_kind:'review'}).kind,'review');
  assert.equal(kindLabel(articleKind({type:'posted-content'})),'Article type unknown');
});
test('combined filtering supports ratings, status, publication date precision, type, phrases and hidden',()=>{
  const papers=[{doi:'10.1000/a',title:'Perovskite solar cell stability',journal:'J',published_date:'2026',first_seen_at:'2026-11-19',type:'journal-article'},
    {doi:'10.1000/b',title:'Review of perovskite devices',journal:'J',published_date:'2026-11',first_seen_at:'2026-11-19',type:'journal-article'},
    {doi:'10.1000/c',title:'Solar cell fabrication',journal:'K',published_date:'2026-10-01',first_seen_at:'2026-11-19',type:'journal-article'}];
  const notes=new Map([['10.1000/a',{rating:4,note:'Important',acknowledged:1}],['10.1000/b',{hidden:1}]]);
  const options={view:'browse',kindFor:articleKind};
  const ids=o=>filterResearchPapers(papers,notes,{...options,...o}).map(p=>p.doi);
  assert.deepEqual(ids({kind:'review',showHidden:true}),['10.1000/b']);
  assert.deepEqual(ids({readStatus:'annotated',minRating:4,dateFrom:'2026-11-01',query:'"solar cell"'}),['10.1000/a']);
  assert.deepEqual(ids({readStatus:'hidden'}),['10.1000/b']);
  assert.deepEqual(ids({dateTo:'2025-12-31'}),[]);
  assert.deepEqual(publicationInterval({published_date:'2026-11'}),['2026-11-01','2026-11-30']);
});
test('authenticated workspace settings persist per-user; invalid payloads and article types are rejected',async()=>{
  const e=env(),ryan=await session(e.DB,1),colleague=await session(e.DB,2);
  const before=await (await worker.fetch(get('/api/workspace-settings',ryan),e)).json();
  assert.equal(before.config.collection.lookback_days,45);
  const changed=defaultConfig();changed.collection.lookback_days=60;changed.preferences.theme='dark';
  assert.equal((await worker.fetch(put('/api/workspace-settings',{config:changed},ryan),e)).status,200);
  const after=await (await worker.fetch(get('/api/workspace-settings',ryan),e)).json();
  assert.equal(after.config.collection.lookback_days,60);
  const other=await (await worker.fetch(get('/api/workspace-settings',colleague),e)).json();
  assert.equal(other.config.collection.lookback_days,45);
  const bad=structuredClone(changed);bad.collection.lookback_days=10000;
  assert.equal((await worker.fetch(put('/api/workspace-settings',{config:bad},ryan),e)).status,400);
  assert.equal((await worker.fetch(put('/api/article-kind',{doi:'10.1000/a',kind:'review'},ryan),e)).status,200);
  assert.equal(e.DB.overrides.get('1|10.1000/a').article_kind,'review');
  assert.equal(e.DB.overrides.get('2|10.1000/a'),undefined);
  assert.equal((await worker.fetch(put('/api/article-kind',{doi:'10.1000/a',kind:'arbitrary'},ryan),e)).status,400);
  assert.equal((await worker.fetch(put('/api/article-kind',{doi:'10.1000/a',kind:'review'},null),e)).status,401);
});

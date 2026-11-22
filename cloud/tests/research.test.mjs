import test from 'node:test';
import assert from 'node:assert/strict';
import {filterResearchPapers,missingAbstract,metadataCsv,metadataRis} from '../team/research.mjs';

const papers = [
  {doi:'10.1000/old',title:'Old paper',journal:'J1',authors:['Alpha, A'],decision:'include',abstract_status:'available',published_date:'2026',published_date_precision:'year',first_seen_at:'2026-09-01T00:00:00Z'},
  {doi:'10.1000/review',title:'Review needed',journal:'J1',authors:['Beta, B'],decision:'review',abstract_status:'missing',published_date:'2026-11',published_date_precision:'month',first_seen_at:'2026-11-16T00:00:00Z'},
  {doi:'10.1000/new',title:'=HYPERLINK("evil")',journal:'J2',authors:['Gamma, C'],decision:'include',abstract_status:'missing',published_date:'2026-11-17',published_date_precision:'day',first_seen_at:'2026-11-17T00:00:00Z'},
  {doi:'10.1000/manual',title:'Manually added',journal:'Other',authors:[],decision:'manual',personal:true,abstract_status:undefined,first_seen_at:'2026-11-18T00:00:00Z'}
];
const annotations=new Map([
  ['10.1000/old',{acknowledged:1,hidden:0,rating:5,note:'note'}],
  ['10.1000/review',{acknowledged:0,hidden:0,rating:null,note:''}],
  ['10.1000/new',{acknowledged:0,hidden:1,rating:null,note:''}]
]);
const now=new Date('2026-11-18T12:00:00Z');
const ids=options=>filterResearchPapers(papers,annotations,{now,...options}).map(p=>p.doi);

test('inbox, review, hidden and rated views are independent and private',()=>{
  assert.deepEqual(ids({view:'inbox'}),['10.1000/manual','10.1000/review']);
  assert.deepEqual(ids({view:'review'}),['10.1000/review']);
  assert.deepEqual(ids({view:'hidden'}),['10.1000/new']);
  assert.deepEqual(ids({view:'rated'}),['10.1000/old']);
});
test('filters compose across relevance, abstract status, journal, recency and search',()=>{
  assert.deepEqual(ids({decision:'review',abstract:'missing',days:7,journal:'J1'}),['10.1000/review']);
  assert.deepEqual(ids({decision:'manual'}),['10.1000/manual']);
  assert.deepEqual(ids({abstract:'available'}),['10.1000/old']);
  assert.deepEqual(ids({days:7,query:'gamma'}),['10.1000/new']);
  assert.deepEqual(ids({days:7,query:'old'}),[]);
  assert.equal(missingAbstract(papers[1]),true);
});
test('CSV exports only metadata and mitigates spreadsheet formula injection',()=>{
  const csv=metadataCsv(papers);
  assert.ok(csv.startsWith('\uFEFF'));
  assert.match(csv,/"'=HYPERLINK\(""evil""\)"/);
  assert.match(csv,/"10.1000\/new"/);
  assert.doesNotMatch(csv,/private note|rating|acknowledged/i);
  assert.equal(csv.split('\r\n').filter(Boolean).length,5);
});
test('RIS has authors and precision-aware publication dates, without injected lines',()=>{
  const ris=metadataRis([...papers,{doi:'10.1000/inject',title:'Safe\nER  - malicious',authors:[],published_date:'2026-11',published_date_precision:'month'}]);
  assert.match(ris,/AU  - Alpha, A/);
  assert.match(ris,/DA  - 2026\/11\/17/);
  assert.match(ris,/TI  - Safe ER - malicious/);
  assert.equal(ris.split('ER  - ').length-1,5);
  assert.doesNotMatch(ris,/note|rating|acknowledged/i);
});

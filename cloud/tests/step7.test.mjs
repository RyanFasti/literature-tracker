import test from 'node:test';
import assert from 'node:assert/strict';
import {CATEGORIES,suggestCategories,categoriesFor,isManualCategories} from '../team/categories.mjs';
import {filterResearchPapers} from '../team/research.mjs';
import {plainAbstract,openAlexAbstract,fetchAbstract} from '../src/abstract.mjs';

test('topic taxonomy uses transparent multi-label rules and preserves manual overrides',()=>{
  const paper={doi:'10.1000/a',title:'Stability of perovskite solar cells processed by spin-coating'};
  assert.deepEqual(suggestCategories(paper),['solar-cell','film','stability']);
  assert.deepEqual(categoriesFor(paper,{categories_json:'["photodetector"]'}),['photodetector']);
  assert.deepEqual(categoriesFor(paper,{categories_json:'[]'}),[]);
  assert.deepEqual(categoriesFor(paper,{categories_json:null}),['solar-cell','film','stability']);
  assert.equal(isManualCategories({categories_json:'[]'}),true);
  assert.equal(CATEGORIES.length,7);
  assert.deepEqual(suggestCategories({title:'An unrelated chemistry paper'}),[]);
  assert.deepEqual(categoriesFor({title:'Unrelated',topics:['stability']},null),['stability']);
  assert.deepEqual(suggestCategories({title:'Perovskite photodetectors and LED stability'}),['photodetector','led','stability']);
});

test('browse excludes hidden and deleted; fast review excludes acknowledged; category filter respects overrides',()=>{
  const papers=[
    {doi:'10.1000/a',title:'Solar cell',journal:'J',decision:'include'},
    {doi:'10.1000/b',title:'Solar cell stability',journal:'J',decision:'include'},
    {doi:'10.1000/c',title:'Photodetector',journal:'J',decision:'include'},
    {doi:'10.1000/d',title:'Solar cell',journal:'J',decision:'include'},
  ];
  const annotations=new Map([['10.1000/b',{hidden:1}],['10.1000/c',{acknowledged:1}]]);
  const overrides=new Map([['10.1000/d',{deleted_at:'2026-11-19'}],['10.1000/c',{categories_json:'["solar-cell"]'}]]);
  const opts={overrides,categoryFor:(p,o)=>categoriesFor(p,o)};
  const ids=(view,extra={})=>filterResearchPapers(papers,annotations,{...opts,view,...extra}).map(p=>p.doi);
  assert.deepEqual(ids('browse'),['10.1000/a','10.1000/c']);
  assert.deepEqual(ids('browse',{showHidden:true}),['10.1000/a','10.1000/b','10.1000/c']);
  assert.deepEqual(ids('triage'),['10.1000/a']);
  assert.deepEqual(ids('hidden'),['10.1000/b']);
  assert.deepEqual(ids('browse',{category:'solar-cell'}),['10.1000/a','10.1000/c']);
});

test('abstract extraction strips markup and handles entities without executing HTML',()=>{
  assert.equal(plainAbstract('<jats:p>Charge &amp; transport <script>alert(1)</script> in films &lt; 3 nm.</jats:p>'),
    'Charge & transport in films < 3 nm.');
  assert.equal(plainAbstract(null),'');
  assert.equal(openAlexAbstract({Solar:[0],cells:[1],work:[2]}),'Solar cells work');
  assert.equal(openAlexAbstract({bad:[1000000]}),'');
});

test('on-demand abstract retrieval prefers Crossref and falls back to OpenAlex',async()=>{
  const prior=globalThis.fetch;
  const urls=[];
  globalThis.fetch=async url=>{urls.push(String(url));if(String(url).includes('crossref'))return Response.json({message:{DOI:'10.1000/abc',abstract:'<p>Lead-halide &amp; devices.</p>'}});throw Error('Should not fetch OpenAlex');};
  try{assert.deepEqual(await fetchAbstract('10.1000/abc'),{abstract:'Lead-halide & devices.',source:'Crossref'});assert.equal(urls.length,1);}
  finally{globalThis.fetch=prior;}
  globalThis.fetch=async url=>String(url).includes('crossref')?Response.json({message:{}}):Response.json({doi:'https://doi.org/10.1000/abc',abstract_inverted_index:{Perovskite:[0],devices:[1]}});
  try{assert.deepEqual(await fetchAbstract('10.1000/abc'),{abstract:'Perovskite devices',source:'OpenAlex'});}
  finally{globalThis.fetch=prior;}
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchAbstract,plainAbstract} from '../src/abstract.mjs';

test('Step 9: inline chemical formula markup does not add spaces',()=>{
  assert.equal(plainAbstract('<jats:p>Abstract: (FAPbI<jats:sub>3</jats:sub>) devices have 21% PCE.</jats:p>'),
    '(FAPbI3) devices have 21% PCE.');
});

test('Step 9: Semantic Scholar fallback verifies DOI',async()=>{
  const previous=globalThis.fetch;
  globalThis.fetch=async url=>{
    const s=String(url);
    if(s.includes('semanticscholar'))return Response.json({externalIds:{DOI:'10.1000/a'},abstract:'Valid research abstract.'});
    return Response.json({});
  };
  try{assert.deepEqual(await fetchAbstract('10.1000/a'),{abstract:'Valid research abstract.',source:'Semantic Scholar'});}
  finally{globalThis.fetch=previous;}
});

test('Step 9: Europe PMC fallback ignores wrong DOI',async()=>{
  const previous=globalThis.fetch;
  globalThis.fetch=async url=>{
    if(String(url).includes('europepmc'))return Response.json({resultList:{result:[
      {doi:'10.1000/wrong',abstractText:'WRONG'},
      {doi:'10.1000/a',abstractText:'Europe PMC verified abstract.'}]}});
    return Response.json({});
  };
  try{assert.deepEqual(await fetchAbstract('10.1000/a'),{abstract:'Europe PMC verified abstract.',source:'Europe PMC'});}
  finally{globalThis.fetch=previous;}
});

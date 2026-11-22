import test from 'node:test';
import assert from 'node:assert/strict';
import {makeBackup, validateBackup, planImport, BACKUP_FORMAT} from '../team/backup.mjs';

const sample = () => makeBackup([
  {doi:'10.1039/D6EE04175B',rating:5,note:'Experiment α',acknowledged:1,hidden:0,share_note:1},
  {doi:'10.1000/second',rating:null,note:'',acknowledged:0,hidden:1,share_note:0},
], [{doi:'10.5281/zenodo.24121153',title:'Personal DOI',journal:'Other'}], '2026-11-18T00:00:00Z');

test('private backup is versioned and includes only user state',()=>{
  const backup=sample();assert.equal(backup.format,BACKUP_FORMAT);
  assert.equal(backup.annotations[0].note,'Experiment α');
  assert.equal(backup.manual_papers.length,1);
  assert.equal(JSON.stringify(backup).includes('access_token'),false);
  assert.equal(JSON.stringify(backup).includes('abstract'),false);
});
test('import forces all notes private and skips existing records',()=>{
  const valid=validateBackup(sample());
  assert.equal(valid.previouslyShared,1);
  assert.equal(valid.annotations[0].share_note,false);
  const plan=planImport(valid,[{doi:'10.1039/d6ee04175b'}],[{doi:'10.5281/zenodo.24121153'}]);
  assert.equal(plan.annotations.length,1);assert.equal(plan.manual_papers.length,0);
  assert.equal(plan.skippedAnnotations,1);assert.equal(plan.skippedManual,1);
});
test('bad files fail validation before mutation, without truncating notes',()=>{
  const bad=sample();bad.annotations[0].note='a'.repeat(4001);
  assert.throws(()=>validateBackup(bad),/4,000 characters/);
  bad.annotations[0].note='ok';bad.annotations[1].doi='10.1039/d6ee04175b';
  assert.throws(()=>validateBackup(bad),/duplicate annotation DOI/);
  bad.annotations[1].doi='10.1000/second';bad.annotations[1].rating=6;
  assert.throws(()=>validateBackup(bad),/Invalid rating/);
  bad.annotations[1].rating=null;bad.annotations[1].secret='should not be imported';
  assert.throws(()=>validateBackup(bad),/Invalid annotation/);
});
test('rejects wrong formats, malformed DOIs and non-boolean share flag',()=>{
  const b=sample();b.schema_version=99;assert.throws(()=>validateBackup(b),/Unsupported/);
  b.schema_version=1;b.manual_papers[0].doi='javascript:alert(1)';
  assert.throws(()=>validateBackup(b),/Invalid or duplicate manual DOI/);
  b.manual_papers[0].doi='10.5281/zenodo.24121153';b.annotations[0].share_note='false';
  assert.throws(()=>validateBackup(b),/boolean/);
});

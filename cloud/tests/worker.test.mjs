import test from 'node:test';
import assert from 'node:assert/strict';
import worker, {normalizeDoi, allowlisted, validMutation} from '../src/worker.mjs';

const BASE='https://literature-tracker-team.example.workers.dev';
class FakeDB {
  constructor(){this.users=new Map();this.sessions=new Map();this.annotations=new Map();this.manual=new Map();this.overrides=new Map();}
  prepare(sql){const db=this;return {bind(...args){return {
    async first(){if(sql.includes('FROM sessions s JOIN users u')){const s=db.sessions.get(args[0]);if(!s||s.expires_at<=args[1])return null;const u=db.users.get(s.github_id);return u?{github_id:u.github_id,login:u.login}:null;}
      if(sql.includes('FROM paper_overrides WHERE github_id'))return db.overrides.get(args[0]+'|'+args[1])||null;
      throw Error('Unexpected first '+sql);},
    async run(){
      let changes=1;
      if(sql.startsWith('INSERT INTO users(')){db.users.set(args[0],{github_id:args[0],login:args[1]});}
      else if(sql.startsWith('DELETE FROM sessions WHERE expires_at')){for(const [k,s] of db.sessions)if(s.expires_at<=args[0])db.sessions.delete(k);}
      else if(sql.startsWith('INSERT INTO sessions(')){db.sessions.set(args[0],{github_id:args[1],expires_at:args[2]});}
      else if(sql.startsWith('DELETE FROM sessions WHERE token_hash')){db.sessions.delete(args[0]);}
      else if(sql.startsWith('INSERT INTO annotations(')){
        const insertOnly=sql.includes('DO NOTHING');
        const [id,doi,rating,note,acknowledged,hidden]=args;
        const share_note=insertOnly?0:args[6],updated_at=insertOnly?args[6]:args[7];
        if(insertOnly&&db.annotations.has(id+'|'+doi))changes=0;
        else db.annotations.set(id+'|'+doi,{github_id:id,doi,rating,note,acknowledged,hidden,share_note,updated_at});
      }
      else if(sql.startsWith('DELETE FROM annotations')){db.annotations.delete(args[0]+'|'+args[1]);}
      else if(sql.startsWith('INSERT INTO manual_papers(')){const [id,doi,title,journal,created_at]=args;
        if(sql.includes('DO NOTHING')&&db.manual.has(id+'|'+doi))changes=0;
        else db.manual.set(id+'|'+doi,{github_id:id,doi,title,journal,created_at});
      }
      else if(sql.startsWith('DELETE FROM manual_papers')){db.manual.delete(args[0]+'|'+args[1]);}
      else if(sql.startsWith('INSERT INTO paper_overrides(')){
        const [id,doi,value,stamp]=args;
        const key=id+'|'+doi;
        const old=db.overrides.get(key)||{github_id:id,doi,deleted_at:null};
        if(sql.includes('categories_json=excluded.categories_json'))
          db.overrides.set(key,{...old,categories_json:value,updated_at:stamp});
        else db.overrides.set(key,{...old,categories_json:null,deleted_at:value,updated_at:stamp});
      }
      else throw Error('Unexpected run '+sql);
      return {success:true,meta:{changes}};
    },
    async all(){let records;
      if(sql.includes('FROM annotations WHERE github_id'))records=[...db.annotations.values()].filter(x=>x.github_id===args[0]).sort((a,b)=>a.doi.localeCompare(b.doi));
      else if(sql.includes('FROM annotations a JOIN users u'))records=[...db.annotations.values()].filter(x=>x.share_note===1&&x.note&&args.slice(0,-2).includes(db.users.get(x.github_id)?.login.toLowerCase())).map(x=>({doi:x.doi,note:x.note,updated_at:x.updated_at,author:db.users.get(x.github_id)?.login})).sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
      else if(sql.includes('FROM manual_papers WHERE github_id'))records=[...db.manual.values()].filter(x=>x.github_id===args[0]);
      else if(sql.includes('FROM paper_overrides WHERE github_id'))records=[...db.overrides.values()].filter(x=>x.github_id===args[0]).sort((a,b)=>a.doi.localeCompare(b.doi));
      else throw Error('Unexpected all '+sql);
      return {results:records.slice(args.at(-1),args.at(-1)+args.at(-2))};
    },
  };}};}
  async batch(statements){const results=[];for(const stmt of statements)results.push(await stmt.run());return results;}
}
function env(){return {DB:new FakeDB(),PUBLIC_BASE_URL:BASE,GITHUB_CLIENT_ID:'test-client',GITHUB_CLIENT_SECRET:'test-secret',ALLOWED_GITHUB_USERS:'RyanFasti,Colleague',ASSETS:{fetch:async()=>new Response('static public catalog',{status:200})}};}
const req=(path,opts={})=>new Request(BASE+path,opts);
const mutation=(path,method,body,cookie,origin=BASE)=>req(path,{method,headers:{'Content-Type':'application/json','X-LT-Request':'1',Origin:origin,...(cookie?{Cookie:cookie}:{})},body:JSON.stringify(body)});
async function login(environment,login,id){
  const start=await worker.fetch(req('/auth/login'),environment);assert.equal(start.status,302);
  const stateCookie=start.headers.get('set-cookie').split(';')[0];const state=stateCookie.split('=')[1];
  const prior=globalThis.fetch;
  globalThis.fetch=async(url)=>{
    if(String(url).includes('/access_token'))return Response.json({access_token:'fake'});
    if(String(url).endsWith('/user'))return Response.json({id,login});
    throw Error('Unexpected fetch '+url);
  };
  try{
    const cb=await worker.fetch(req('/auth/callback?code=mock&state='+state,{headers:{Cookie:stateCookie}}),environment);
    return cb;
  }finally{globalThis.fetch=prior;}
}
function sessionCookie(response){const cookies=response.headers.getSetCookie();const session=cookies.find(c=>c.startsWith('lt_session='));assert.ok(session,'Missing session cookie');return session.split(';')[0];}

test('DOI normalization and explicit nonempty allowlist',()=>{
  assert.equal(normalizeDoi('https://doi.org/10.1039/D6EE04175B'),'10.1039/d6ee04175b');
  assert.equal(normalizeDoi('not a DOI'),null);
  assert.equal(normalizeDoi('10.1000/test?query'),null);
  assert.equal(allowlisted({ALLOWED_GITHUB_USERS:''},'RyanFasti'),false);
  assert.equal(allowlisted({ALLOWED_GITHUB_USERS:'ryanfasti'},'RyanFasti'),true);
});
test('public assets accessible; unauthenticated API protected',async()=>{
  const e=env();const asset=await worker.fetch(req('/articles.json'),e);assert.equal(asset.status,200);
  const me=await worker.fetch(req('/api/me'),e);assert.deepEqual(await me.json(),{authenticated:false});
  const denied=await worker.fetch(req('/api/annotations'),e);assert.equal(denied.status,401);
  const bad=await worker.fetch(req('/auth/callback?code=x&state=invalid'),e);assert.equal(bad.status,400);
});
test('GitHub OAuth state rejects forged callback; disallowed accounts cannot log in',async()=>{
  const e=env();const start=await worker.fetch(req('/auth/login'),e);
  const cookie=start.headers.get('set-cookie').split(';')[0];
  const invalid=await worker.fetch(req('/auth/callback?code=mock&state='+'a'.repeat(64),{headers:{Cookie:cookie}}),e);
  assert.equal(invalid.status,400);
  const denied=await login(e,'Uninvited',1003);assert.equal(denied.status,403);assert.equal(e.DB.sessions.size,0);
});
test('sessions, user isolation, opt-in team notes, CSRF, validation and logout',async()=>{
  const e=env();const ryan=sessionCookie(await login(e,'RyanFasti',1001));const other=sessionCookie(await login(e,'Colleague',1002));
  const me=await worker.fetch(req('/api/me',{headers:{Cookie:ryan}}),e);assert.equal((await me.json()).login,'RyanFasti');
  const doc={doi:'10.1039/d6ee04175b',rating:5,note:'Private experimental note',acknowledged:true,hidden:true,share_note:false};
  const badOrigin=await worker.fetch(mutation('/api/annotation','PUT',doc,ryan,'https://evil.example'),e);assert.equal(badOrigin.status,403);
  const noCsrf=await worker.fetch(req('/api/annotation',{method:'PUT',headers:{Cookie:ryan,'Content-Type':'application/json'},body:JSON.stringify(doc)}),e);assert.equal(noCsrf.status,403);
  const badRating=await worker.fetch(mutation('/api/annotation','PUT',{...doc,rating:6},ryan),e);assert.equal(badRating.status,400);
  const badEmptyShare=await worker.fetch(mutation('/api/annotation','PUT',{...doc,note:'',share_note:true},ryan),e);assert.equal(badEmptyShare.status,400);
  const save=await worker.fetch(mutation('/api/annotation','PUT',doc,ryan),e);assert.equal(save.status,200);
  const mine=await worker.fetch(req('/api/annotations',{headers:{Cookie:ryan}}),e);assert.equal((await mine.json()).items[0].note,doc.note);
  const theirs=await worker.fetch(req('/api/annotations',{headers:{Cookie:other}}),e);assert.deepEqual((await theirs.json()).items,[]);
  const sharedBefore=await worker.fetch(req('/api/shared',{headers:{Cookie:other}}),e);assert.deepEqual((await sharedBefore.json()).items,[]);
  const share=await worker.fetch(mutation('/api/annotation','PUT',{...doc,share_note:true},ryan),e);assert.equal(share.status,200);
  const sharedAfter=await worker.fetch(req('/api/shared',{headers:{Cookie:other}}),e);const notes=(await sharedAfter.json()).items;
  assert.equal(notes.length,1);assert.equal(notes[0].note,doc.note);assert.equal(notes[0].author,'RyanFasti');
  assert.equal('rating' in notes[0],false);assert.equal('hidden' in notes[0],false);
  const revoke=await worker.fetch(mutation('/api/annotation','PUT',{...doc,share_note:false},ryan),e);assert.equal(revoke.status,200);
  const sharedGone=await worker.fetch(req('/api/shared',{headers:{Cookie:other}}),e);assert.deepEqual((await sharedGone.json()).items,[]);
  const logout=await worker.fetch(mutation('/auth/logout','POST',{},ryan),e);assert.equal(logout.status,200);
  const after=await worker.fetch(req('/api/me',{headers:{Cookie:ryan}}),e);assert.equal((await after.json()).authenticated,false);
});
test('manual DOI entries are per-user and accept any journal',async()=>{
  const e=env();const ryan=sessionCookie(await login(e,'RyanFasti',1001));const other=sessionCookie(await login(e,'Colleague',1002));
  const add=await worker.fetch(mutation('/api/manual-papers','POST',{doi:'10.5281/zenodo.24121153',title:'Manual study',journal:'Other'},ryan),e);assert.equal(add.status,200);
  const mine=await worker.fetch(req('/api/manual-papers',{headers:{Cookie:ryan}}),e);assert.equal((await mine.json()).items[0].doi,'10.5281/zenodo.24121153');
  const theirs=await worker.fetch(req('/api/manual-papers',{headers:{Cookie:other}}),e);assert.deepEqual((await theirs.json()).items,[]);
  const deleted=await worker.fetch(mutation('/api/manual-papers','DELETE',{doi:'10.5281/zenodo.24121153'},ryan),e);assert.equal(deleted.status,200);
  const after=await worker.fetch(req('/api/manual-papers',{headers:{Cookie:ryan}}),e);assert.deepEqual((await after.json()).items,[]);
});
test('allowlist revocation takes effect on next API request',async()=>{
  const e=env();const session=sessionCookie(await login(e,'Colleague',1002));e.ALLOWED_GITHUB_USERS='RyanFasti';
  const res=await worker.fetch(req('/api/annotations',{headers:{Cookie:session}}),e);assert.equal(res.status,401);
});
test('revoked member shared notes are hidden from remaining team',async()=>{
  const e=env();const ryan=sessionCookie(await login(e,'RyanFasti',1001));const other=sessionCookie(await login(e,'Colleague',1002));
  const doc={doi:'10.1000/private',rating:2,note:'Explicitly shared',acknowledged:false,hidden:false,share_note:true};
  assert.equal((await worker.fetch(mutation('/api/annotation','PUT',doc,other),e)).status,200);
  const before=await worker.fetch(req('/api/shared',{headers:{Cookie:ryan}}),e);
  assert.equal((await before.json()).items.length,1);
  e.ALLOWED_GITHUB_USERS='RyanFasti';
  const after=await worker.fetch(req('/api/shared',{headers:{Cookie:ryan}}),e);
  assert.equal((await after.json()).items.length,0);
});
test('pagination and invalid offset handled',async()=>{
  const e=env();const session=sessionCookie(await login(e,'RyanFasti',1001));
  const invalid=await worker.fetch(req('/api/annotations?offset=-1',{headers:{Cookie:session}}),e);assert.equal(invalid.status,400);
  for(let i=0;i<255;i++)e.DB.annotations.set('1001|10.1000/'+i,{github_id:1001,doi:'10.1000/'+i,note:'',share_note:0});
  const first=await worker.fetch(req('/api/annotations',{headers:{Cookie:session}}),e);const p1=await first.json();assert.equal(p1.items.length,250);assert.equal(p1.next_offset,250);
  const second=await worker.fetch(req('/api/annotations?offset=250',{headers:{Cookie:session}}),e);const p2=await second.json();assert.equal(p2.items.length,5);assert.equal(p2.next_offset,null);
});

test('import endpoints enforce insert-only, per-user ownership and private notes',async()=>{
  const e=env();const ryan=sessionCookie(await login(e,'RyanFasti',1001));
  const colleague=sessionCookie(await login(e,'Colleague',1002));
  const base={doi:'10.1039/d6ee04175b',rating:5,note:'private first note',acknowledged:true,hidden:false,share_note:false};
  const first=await worker.fetch(mutation('/api/import/annotation','POST',base,ryan),e);
  assert.equal((await first.json()).created,true);
  const existing=await worker.fetch(mutation('/api/import/annotation','POST',{...base,rating:1,note:'overwritten'},ryan),e);
  assert.equal((await existing.json()).created,false);
  const own=await worker.fetch(req('/api/annotations',{headers:{Cookie:ryan}}),e);
  assert.equal((await own.json()).items[0].note,'private first note');
  const second=await worker.fetch(mutation('/api/import/annotation','POST',base,colleague),e);
  assert.equal((await second.json()).created,true);
  const illegal=await worker.fetch(mutation('/api/import/annotation','POST',{...base,doi:'10.1000/other',share_note:true},ryan),e);
  assert.equal(illegal.status,400);
  const unauth=await worker.fetch(mutation('/api/import/annotation','POST',base,null),e);
  assert.equal(unauth.status,401);
  const manual={doi:'10.5281/zenodo.1',title:'First title',journal:'Other'};
  assert.equal((await (await worker.fetch(mutation('/api/import/manual-paper','POST',manual,ryan),e)).json()).created,true);
  assert.equal((await (await worker.fetch(mutation('/api/import/manual-paper','POST',{...manual,title:'Changed'},ryan),e)).json()).created,false);
  const rows=await worker.fetch(req('/api/manual-papers',{headers:{Cookie:ryan}}),e);
  assert.equal((await rows.json()).items[0].title,'First title');
});

test('Step 7 categories are per-user, validated, and resettable',async()=>{
  const e=env();const ryan=sessionCookie(await login(e,'RyanFasti',1001));const other=sessionCookie(await login(e,'Colleague',1002));
  const doi='10.1000/film';
  assert.equal((await worker.fetch(mutation('/api/paper-categories','PUT',{doi,categories:['film','stability']},ryan),e)).status,200);
  const mine=await (await worker.fetch(req('/api/paper-overrides',{headers:{Cookie:ryan}}),e)).json();
  assert.deepEqual(JSON.parse(mine.items[0].categories_json),['film','stability']);
  assert.deepEqual((await (await worker.fetch(req('/api/paper-overrides',{headers:{Cookie:other}}),e)).json()).items,[]);
  assert.equal((await worker.fetch(mutation('/api/paper-categories','PUT',{doi,categories:['not-allowed']},ryan),e)).status,400);
  assert.equal((await worker.fetch(mutation('/api/paper-categories','PUT',{doi,categories:['film','film']},ryan),e)).status,400);
  assert.equal((await worker.fetch(mutation('/api/paper-categories','PUT',{doi,categories:null},ryan),e)).status,200);
  assert.equal((await (await worker.fetch(req('/api/paper-overrides',{headers:{Cookie:ryan}}),e)).json()).items[0].categories_json,null);
  assert.equal((await worker.fetch(mutation('/api/paper-categories','PUT',{doi,categories:[]},ryan),e)).status,200);
  assert.equal((await (await worker.fetch(req('/api/paper-overrides',{headers:{Cookie:ryan}}),e)).json()).items[0].categories_json,'[]');
});

test('Step 7 permanent deletion removes own notes and personal DOI, retains a tombstone',async()=>{
  const e=env();const ryan=sessionCookie(await login(e,'RyanFasti',1001));const other=sessionCookie(await login(e,'Colleague',1002));
  const doi='10.1000/private';
  const doc={doi,rating:5,note:'Sensitive research notes',acknowledged:true,hidden:false,share_note:false};
  assert.equal((await worker.fetch(mutation('/api/annotation','PUT',doc,ryan),e)).status,200);
  assert.equal((await worker.fetch(mutation('/api/annotation','PUT',{...doc,note:'Other researcher'},other),e)).status,200);
  assert.equal((await worker.fetch(mutation('/api/manual-papers','POST',{doi,title:'My paper',journal:'Other'},ryan),e)).status,200);
  const deleted=await worker.fetch(mutation('/api/paper-delete','POST',{doi},ryan),e);assert.equal(deleted.status,200);
  assert.deepEqual((await (await worker.fetch(req('/api/annotations',{headers:{Cookie:ryan}}),e)).json()).items,[]);
  assert.deepEqual((await (await worker.fetch(req('/api/manual-papers',{headers:{Cookie:ryan}}),e)).json()).items,[]);
  assert.ok((await (await worker.fetch(req('/api/paper-overrides',{headers:{Cookie:ryan}}),e)).json()).items[0].deleted_at);
  assert.equal((await (await worker.fetch(req('/api/annotations',{headers:{Cookie:other}}),e)).json()).items[0].note,'Other researcher');
  assert.equal((await worker.fetch(mutation('/api/paper-delete','POST',{doi:'invalid'},ryan),e)).status,400);
  assert.equal((await worker.fetch(mutation('/api/annotation','PUT',doc,ryan),e)).status,409);
  assert.equal((await worker.fetch(mutation('/api/manual-papers','POST',{doi,title:'Readded',journal:'Other'},ryan),e)).status,409);
});

test('Step 7 abstract API is authenticated and never exposes provider markup',async()=>{
  const e=env();const session=sessionCookie(await login(e,'RyanFasti',1001));
  assert.equal((await worker.fetch(req('/api/abstract?doi=10.1000/a'),e)).status,401);
  assert.equal((await worker.fetch(req('/api/abstract?doi=bad',{headers:{Cookie:session}}),e)).status,400);
  const prior=globalThis.fetch;
  globalThis.fetch=async()=>Response.json({message:{DOI:'10.1000/a',abstract:'<jats:p>Stable &amp; efficient solar cells.</jats:p>'}});
  try{const res=await worker.fetch(req('/api/abstract?doi=10.1000/a',{headers:{Cookie:session}}),e);assert.equal(res.status,200);assert.deepEqual(await res.json(),{abstract:'Stable & efficient solar cells.',source:'Crossref'});}
  finally{globalThis.fetch=prior;}
});

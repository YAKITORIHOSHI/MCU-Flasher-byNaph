/* Firebase rules integration checks. Start only Auth + RTDB emulators in a
 * synthetic demo project, then execute this file through emulators:exec.
 * Explicit loopback emulator hosts are required; production hosts are refused.
 * No real credential, account, project or database is accepted by this verifier.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
function loopback(name) {
  const host = process.env[name];
  assert.match(host || '', /^(127\.0\.0\.1|localhost):[0-9]{2,5}$/, `${name} must be an explicit loopback emulator`);
  return 'http://' + host;
}
const db = loopback('FIREBASE_DATABASE_EMULATOR_HOST');
const auth = loopback('FIREBASE_AUTH_EMULATOR_HOST');
const ns = 'demo-mcu-cloud-rules-default-rtdb';
const clone = value => JSON.parse(JSON.stringify(value));
async function account(email) {
  const r = await fetch(auth+'/identitytoolkit.googleapis.com/v1/accounts:signUp?key=fixture', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email,password:'fixture-password',returnSecureToken:true}),
    signal: AbortSignal.timeout(5000),
  });
  assert.equal(r.status,200,'Synthetic emulator account creation');
  return r.json();
}
async function req(method, node, value, token) {
  const r = await fetch(db+'/'+node+'.json?ns='+ns+(token && token!=='owner'?'&auth='+encodeURIComponent(token):''), {
    method, headers:{'Content-Type':'application/json', ...(token==='owner'?{'Authorization':'Bearer owner'}:{})},
    body:value===undefined?undefined:JSON.stringify(value), signal:AbortSignal.timeout(5000),
  });
  return [r.status,await r.json()];
}
(async()=>{
  const rules = JSON.parse(fs.readFileSync(path.join(__dirname,'cloud','database.rules.json'),'utf8'));
  const compilation = await req('PUT','.settings/rules',rules,'owner');
  assert.equal(compilation[0],200,JSON.stringify(compilation[1]));
  const one=await account('one-'+Date.now()+'@example.com'), two=await account('two-'+Date.now()+'@example.com');
  const node='users/'+one.localId+'/cloud_sketches/fixture';
  const now=Date.now();
  const files={U2tldGNoLmlubw:{name:'Sketch.ino',content:'void setup() {}',sha256:'a'.repeat(64)}};
  const rec={schema:1,meta:{id:'fixture',name:'Sketch',owner_uid:one.localId,revision:1,created_at:now,updated_at:now,file_count:1},versions:{r1:{revision:1,created_at:now,files}}};
  assert.notEqual((await req('PUT',node,rec))[0],200,'Anonymous writes must fail');
  assert.equal((await req('PUT',node,rec,one.idToken))[0],200,'Own source write must pass');
  const own=await req('GET',node,undefined,one.idToken);
  assert.equal(own[0],200,'Own read must pass');
  assert.equal(Array.isArray(own[1].versions),false,'Revision history must remain a map on REST read');
  assert.deepEqual(own[1].versions.r1,rec.versions.r1);
  assert.notEqual((await req('GET',node,undefined,two.idToken))[0],200,'Other-user read must fail');
  assert.notEqual((await req('PUT',node,rec,two.idToken))[0],200,'Other-user write must fail');
  const ticket='users/'+one.localId+'/tickets/legacy';
  assert.equal((await req('PUT',ticket,{title:'existing user ticket'},one.idToken))[0],200,'Existing ticket path retained');
  const next=clone(rec); next.meta.revision=2; next.meta.updated_at=Date.now(); next.versions.r2={revision:2,created_at:Date.now(),files:clone(files)};
  assert.equal((await req('PUT',node,next,one.idToken))[0],200,'Revision increment must pass');
  const bad=clone(next); bad.meta.revision=3; bad.versions.r3={revision:3,created_at:Date.now(),files:clone(files)}; bad.versions.r1.files.U2tldGNoLmlubw.content='tampered retained version';
  assert.notEqual((await req('PUT',node,bad,one.idToken))[0],200,'Retained version mutation must fail');
  const traverse=clone(next); traverse.meta.revision=3; traverse.versions.r3={revision:3,created_at:Date.now(),files:clone(files)}; traverse.versions.r3.files.U2tldGNoLmlubw.name='../escape.ino';
  assert.notEqual((await req('PUT',node,traverse,one.idToken))[0],200,'Filename traversal must fail');
  const unknown=clone(next); unknown.meta.revision=3; unknown.versions.r3={revision:3,created_at:Date.now(),files:clone(files)}; unknown.private='extra';
  assert.notEqual((await req('PUT',node,unknown,one.idToken))[0],200,'Unknown cloud field must fail');
  for(let version=3;version<=22;version++){
    next.meta.revision=version; next.meta.updated_at=Date.now(); next.versions['r'+version]={revision:version,created_at:Date.now(),files:clone(files)};
    if(version>20) delete next.versions['r'+(version-20)];
    assert.equal((await req('PUT',node,next,one.idToken))[0],200,'Bounded revision history must pass');
  }
  const history=await req('GET',node,undefined,one.idToken);
  assert.equal(history[0],200);
  assert.equal(Array.isArray(history[1].versions),false);
  assert.equal(Object.keys(history[1].versions).length,20);
  assert.equal(history[1].versions.r22.revision,22);
  assert.equal(history[1].versions.r1,undefined);
  assert.equal((await req('DELETE',node,undefined,one.idToken))[0],200,'Own cloud delete must pass');
  assert.equal((await req('GET',ticket,undefined,one.idToken))[0],200,'Legacy tickets survive cloud delete');
  console.log('Cloud rules compiled; anonymous/cross-user denial, owner writes, legacy tickets, prefixed revision REST reads, 20-version retention, immutable leaves, traversal and schema checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

/* Verify real async source transport with controllable replies and no file IO. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../src/editor/index.html'), 'utf8');
const start = html.indexOf('      function initQtWebChannel()');
const end = html.indexOf('      // Let the bundle create', start);
assert(start > 0 && end > start);

async function main() {
  const requests = [], timers = new Map();
  let reply, sequence = 0;
  const bridge = {
    get_project_files() {throw new Error('The synchronous directory scan must stay unused');},
    read_file() {throw new Error('The synchronous source read must stay unused');},
    editor_storage_ready:{connect(callback) {reply = callback;}},
    request_editor_storage(renderer, request, operation, file, accepted) {
      requests.push({renderer, request, operation, file}); accepted(true);
    },
  };
  const window = {location:{search:'?renderer=current'},
    safeLoadProject:()=>Promise.resolve(true),removeEventListener(){},dispatchEvent(){}};
  const context = {window,URLSearchParams,console,
    qt:{webChannelTransport:{}},Event:class{},
    QWebChannel:function(_,complete) {complete({objects:{editorBridge:bridge}});},
    setTimeout(callback) {timers.set(++sequence,callback);return sequence;},
    clearTimeout(timer) {timers.delete(timer);},
  };
  vm.runInNewContext(html.slice(start,end)+'\ninitQtWebChannel();',context);
  const api = window.pywebview.api;
  const tick = async () => {for(let index=0;index<12;index++)await Promise.resolve();};
  let filesReady = false;
  const files = api.get_project_files().then(value=>{filesReady=true;return value;});
  const source = api.read_file('/fixture/main.cpp');
  await tick();
  assert.equal(requests.length,1,'Storage requests must remain serialized');
  reply('old',requests[0].request,{success:true,value:[]});
  await tick();
  assert.equal(filesReady,false,'An obsolete renderer must not satisfy a current request');
  reply('current',requests[0].request,{success:true,value:[{name:'main.cpp',path:'/fixture/main.cpp'}]});
  assert.equal((await files).length,1);
  await tick();
  assert.equal(requests.length,2);
  assert.equal(requests[1].operation,'read_file');
  reply('current',requests[1].request,{success:true,value:{success:true,content:'// unchanged source'}});
  assert.equal((await source).content,'// unchanged source');
  assert.equal(timers.size,0);

  const failed = api.read_file('/fixture/missing.cpp');
  const rejected = assert.rejects(failed,/could not be read/);
  await tick();
  reply('current',requests.at(-1).request,{success:true,value:{success:false,error:'could not be read'}});
  await rejected;
  assert.equal(window._editorReadFailures,1);
  const timedOut = api.get_project_files();
  const timeoutRejected = assert.rejects(timedOut,/timed out/);
  await tick();
  const obsolete = requests.at(-1).request;
  [...timers.values()][0]();
  await timeoutRejected;
  const retry = api.get_project_files();
  await tick();
  reply('current',obsolete,{success:true,value:['late result']});
  reply('current',requests.at(-1).request,{success:true,value:[]});
  assert.equal((await retry).length,0,'A timeout must leave the next request usable');
  assert.equal(timers.size,0);
  const failuresBeforeOverflow=window._editorReadFailures;
  const batch=Promise.allSettled(Array.from({length:33},()=>api.get_project_files()));
  await tick();
  assert.equal(window._editorReadFailures,failuresBeforeOverflow+1,
    'Queue overflow must prevent the loader from acknowledging partial sources');
  for(let index=0;index<32;index++) {
    reply('current',requests.at(-1).request,{success:true,value:[]});
    await tick();
  }
  const completed=await batch;
  assert.equal(completed.filter(result=>result.status==='rejected').length,1);
  assert.equal(timers.size,0);
  console.log('Editor source IPC serializes reads, rejects stale replies/errors, and retries after timeout.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

// Independent main-dispatch regression: background text never relaxes pixels.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import {captureBoundScreenshot,executeBoundDom,publicUrl,requirePinnedProfile,inspectDom} from './xhs_bridge_driver.mjs';
import {connectorStatus} from './xhs_connector_health.mjs';

const page='A'.repeat(32),uid='1'.repeat(24),note='2'.repeat(24);
const url='https://www.xiaohongshu.com/explore/'+note;
const cfg={action:'screenshot',tab:page,uid,contextId:'synthetic-profile',session:'xhs-synthetic-detail',
  extensionVersion:'1.0.27',packageRoot:'/synthetic-pinned-opencli',stage:'body',expectedPath:'/explore/'+note,
  backgroundText:true};
const source=fs.readFileSync(new URL('./xhs_bridge_driver.mjs',import.meta.url),'utf8');
const code=source.slice(source.indexOf('async function main(cfg) {'),source.indexOf('\nasync function respond(cfg) {'))
  .replace("const load = p => import(pathToFileURL(path.join(pkg,'dist/src',p)).href);",'const load=globalThis.fixtureLoad;');

async function dispatch(visibility) {
  const calls=[];let reads=0;
  const command=async(action,params)=>{
    assert.equal(params.page,page);assert.equal(params.contextId,cfg.contextId);assert.equal(params.session,cfg.session);
    calls.push(action);
    if(action==='exec')return {page,data:{uid,url,visible:visibility[Math.min(reads++,visibility.length-1)]}};
    assert.equal(action,'screenshot');return {page,data:'SYNTHETIC_PIXELS_TO_DISCARD'};
  };
  const fakePage={tabs:async()=>[{page,url}],setActivePage:target=>assert.equal(target,page)};
  const modules={
    'browser/daemon-transport.js':{requestDaemon:async()=>({ok:true,json:async()=>({daemonVersion:'2.0.2',profiles:[{contextId:cfg.contextId,extensionVersion:'1.0.27'}]})})},
    'browser/daemon-client.js':{setDaemonCommandTimeoutSeconds:()=>{},sendCommandFull:command,
      sendCommand:()=>{throw new Error('NATIVE_API_FORBIDDEN');}},
    'browser/daemon-lifecycle.js':{daemonLifecycleHooks:{}},
    'browser/utils.js':{buildEvaluateExpression:expr=>expr},
    'browser/bridge.js':{BrowserBridge:class{async connect(){return fakePage;}async close(){}}},
  };
  const context={fs:{readFileSync:()=>JSON.stringify({name:'@jackwener/opencli',version:'2.0.2'})},path,pathToFileURL,URL,
    captureBoundScreenshot,executeBoundDom,publicUrl,requirePinnedProfile,inspectDom,connectorStatus,
    domSource:fn=>fn.toString(),fixtureLoad:async name=>{assert.ok(name in modules);return modules[name];}};
  vm.createContext(context);vm.runInContext(code+';globalThis.run=main;',context);
  try{return {calls,result:await context.run(cfg)};}catch(error){return {calls,error};}
}

const hidden=await dispatch([false]);
assert.equal(hidden.result,undefined);assert.equal(hidden.error.message,'SCREENSHOT_TARGET_NOT_VISIBLE');
assert.deepEqual(hidden.calls,['exec','exec']); // No screenshot, activation or native window command.
const changed=await dispatch([true,true,false]);
assert.equal(changed.result,undefined);assert.equal(changed.error.message,'SCREENSHOT_TARGET_NOT_VISIBLE');
assert.deepEqual(changed.calls,['exec','exec','screenshot','exec']);
assert.ok(!JSON.stringify(changed.error).includes('SYNTHETIC_PIXELS'));
assert.equal(JSON.parse(changed.error.detail).pngDiscarded,true);
console.log('PASS: backgroundText=true cannot bypass screenshot visibility before or after capture; actual main dispatch; no Chrome/network');

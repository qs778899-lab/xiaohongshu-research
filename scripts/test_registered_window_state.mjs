// Synthetic read-only native-window diagnostics. No daemon/Chrome/network.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import {registeredWindowState,executeBoundDom,publicUrl,requirePinnedProfile,inspectDom} from './xhs_bridge_driver.mjs';
import {connectorStatus} from './xhs_connector_health.mjs';

const target='A'.repeat(32),other='B'.repeat(32),uid='1'.repeat(24);
const url='https://www.xiaohongshu.com/explore/'+'2'.repeat(24)+'?xsec_token=synthetic';
const cfg={tab:target,contextId:'synthetic-profile',session:'xhs-synthetic-detail',uid,
  extensionVersion:'1.0.27',packageRoot:'/synthetic-pinned-opencli'};
const identity={page:target,url,uid,visible:false};
let checks=0;

function fixture(options={}) {
  const calls=[];
  const queryRow={id:31,windowId:47,url};
  Object.defineProperty(queryRow,'title',{get(){throw new Error('TITLE_READ');}});
  const window={id:47,state:'minimized',focused:false};
  Object.defineProperty(window,'tabs',{get(){throw new Error('OTHER_TABS_READ');}});
  const tab={id:31,windowId:47,url,active:true};
  Object.defineProperty(tab,'title',{get(){throw new Error('TITLE_READ');}});
  const native=async(action,params)=>{
    calls.push({action,params});assert.equal(action,'chrome-call');assert.equal(params.contextId,cfg.contextId);
    switch(params.chromeMethod) {
      case 'tabs.query':assert.deepEqual(params.chromeArgs,[{url}]);return options.query??[queryRow];
      case 'windows.get':assert.deepEqual(params.chromeArgs,[47,{populate:false}]);return options.window??window;
      case 'tabs.get':assert.deepEqual(params.chromeArgs,[31]);return options.tab??tab;
      default:throw new Error('UNPERMITTED_NATIVE_METHOD');
    }
  };
  const after=async()=>{calls.push({action:'identity-after'});return options.after??identity;};
  return {calls,native,after};
}

{
  const f=fixture();const result=await registeredWindowState(f.native,cfg,url,identity,f.after);
  assert.deepEqual(f.calls.map(c=>c.params?.chromeMethod??c.action),['tabs.query','windows.get','tabs.get','identity-after']);
  assert.deepEqual(result.window,{id:47,state:'minimized',focused:false});
  assert.deepEqual(result.tab,{id:31,windowId:47,url,active:true});
  assert.equal(result.visibleBefore,false);assert.equal(result.visibleAfter,false);
  assert.equal(result.readOnly,true);assert.equal(result.windowFocusRequested,false);assert.equal(result.windowRestoreRequested,false);
  assert.ok(!JSON.stringify(result).includes('title'));checks++;
}
for(const query of [[],[{id:31,windowId:47,url},{id:32,windowId:47,url}],{},
                    [{id:31,windowId:47,url:'https://www.xiaohongshu.com/explore/'+'3'.repeat(24)}]]) {
  const f=fixture({query});await assert.rejects(()=>registeredWindowState(f.native,cfg,url,identity,f.after),
    /REGISTERED_WINDOW_(UNIQUE_MATCH_REQUIRED|URL_CHANGED)/);
  assert.equal(f.calls.length,1);checks++;
}
for(const changed of [{id:32,windowId:47,url,active:true},{id:31,windowId:48,url,active:true},
                     {id:31,windowId:47,url:url+'&changed=1',active:true},{id:31,windowId:47,url}]) {
  const f=fixture({tab:changed});await assert.rejects(()=>registeredWindowState(f.native,cfg,url,identity,f.after),
    /REGISTERED_WINDOW_(TARGET_CHANGED|IDENTITY_UNCONFIRMED)/);
  assert.equal(f.calls.length,3);checks++;
}
for(const window of [{id:48,state:'normal',focused:true},{id:47,state:'unknown',focused:true},{id:47,state:'normal'}]) {
  const f=fixture({window});await assert.rejects(()=>registeredWindowState(f.native,cfg,url,identity,f.after),/REGISTERED_WINDOW_IDENTITY_UNCONFIRMED/);
  assert.equal(f.calls.length,2);checks++;
}
for(const before of [{...identity,uid:'wrong'},{...identity,url:url+'&changed=1'},
                     {...identity,page:other},{...identity,error:'ACCESS_STOP'}]) {
  const f=fixture();await assert.rejects(()=>registeredWindowState(f.native,cfg,url,before,f.after),
    /ACCOUNT_MISMATCH|UNEXPECTED_TARGET_CHANGE|TARGET_MISMATCH|ACCESS_STOP/);
  assert.equal(f.calls.length,0);checks++;
}
for(const after of [{...identity,uid:'wrong'},{...identity,url:url+'&changed=1'},
                    {...identity,page:other},{...identity,error:'ACCESS_STOP'}]) {
  const f=fixture({after});await assert.rejects(()=>registeredWindowState(f.native,cfg,url,identity,f.after),
    /ACCOUNT_MISMATCH|UNEXPECTED_TARGET_CHANGE|TARGET_MISMATCH|ACCESS_STOP/);
  assert.equal(f.calls.length,4);checks++;
}
{
  const f=fixture();await assert.rejects(()=>registeredWindowState(f.native,cfg,url+'*',identity,f.after),/EXACT_URL_REQUIRED/);
  assert.equal(f.calls.length,0);checks++;
}
{
  const f=fixture({query:[{id:-1,windowId:47,url}]});
  await assert.rejects(()=>registeredWindowState(f.native,cfg,url,identity,f.after),/REGISTERED_WINDOW_IDENTITY_UNCONFIRMED/);
  assert.equal(f.calls.length,1);checks++;
}

// Run the actual main dispatch with only its imports replaced. No write-capable
// stub is provided: the only available native calls are the three exact reads.
const source=fs.readFileSync(new URL('./xhs_bridge_driver.mjs',import.meta.url),'utf8');
let mainCode=source.slice(source.indexOf('async function main(cfg) {'),source.indexOf('\nasync function respond(cfg) {'));
mainCode=mainCode.replace("const load = p => import(pathToFileURL(path.join(pkg,'dist/src',p)).href);",'const load=globalThis.fixtureLoad;');
async function dispatch(options={}) {
  const f=fixture();const events=[],pageCalls=[];let guards=0;
  const fakePage={tabs:async()=>[{page:target,url}],setActivePage:value=>pageCalls.push(['bind',value])};
  const command=async(action,params)=>{
    events.push(action);assert.equal(action,'exec');assert.equal(params.page,target);guards++;
    return {page:guards===2&&options.wrongAfterPage?other:target,
      data:guards===2&&options.after?options.after:options.before??{uid,url,visible:false}};
  };
  const native=async(action,params)=>{events.push(params.chromeMethod);return f.native(action,params);};
  const modules={
    'browser/daemon-transport.js':{requestDaemon:async()=>({ok:true,json:async()=>({daemonVersion:'2.0.2',profiles:[{contextId:cfg.contextId,extensionVersion:'1.0.27'}]})})},
    'browser/daemon-client.js':{setDaemonCommandTimeoutSeconds:()=>{},sendCommandFull:command,sendCommand:native},
    'browser/daemon-lifecycle.js':{daemonLifecycleHooks:{}},
    'browser/utils.js':{buildEvaluateExpression:code=>code},
    'browser/bridge.js':{BrowserBridge:class{async connect(){return fakePage;}async close(){pageCalls.push(['close-reference']);}}}
  };
  const context={fs:{readFileSync:()=>JSON.stringify({name:'@jackwener/opencli',version:'2.0.2'})},path,pathToFileURL,URL,
    publicUrl,requirePinnedProfile,executeBoundDom,registeredWindowState,inspectDom,connectorStatus,
    domSource:fn=>fn.toString(),fixtureLoad:async name=>{assert.ok(name in modules,name);return modules[name];}};
  vm.createContext(context);vm.runInContext(mainCode+';globalThis.run=main;',context);
  try{return {result:await context.run({...cfg,action:'registered_window_state'}),events,pageCalls};}
  catch(error){return {error,events,pageCalls};}
}
{
  const output=await dispatch();assert.equal(output.error,undefined);
  assert.deepEqual(output.events,['exec','tabs.query','windows.get','tabs.get','exec']);
  assert.deepEqual(output.pageCalls,[['bind',target],['close-reference']]);
  assert.equal(output.result.window.state,'minimized');assert.equal(output.result.readOnly,true);checks++;
}
for(const before of [{uid:'wrong',url,visible:false},{error:'ACCESS_STOP'}]) {
  const output=await dispatch({before});assert.ok(output.error);assert.equal(output.result,undefined);
  assert.deepEqual(output.events,['exec']);checks++;
}
{
  const output=await dispatch({wrongAfterPage:true});assert.equal(output.error.message,'EXEC_TARGET_MISMATCH');
  assert.equal(output.result,undefined);assert.deepEqual(output.events,['exec','tabs.query','windows.get','tabs.get','exec']);checks++;
}
{
  const output=await dispatch({after:{uid:'wrong',url,visible:false}});assert.equal(output.error.message,'ACCOUNT_MISMATCH');
  assert.equal(output.result,undefined);checks++;
}
console.log(`PASS: ${checks} exact registered-window read/guard cases; no Chrome/network/writes`);

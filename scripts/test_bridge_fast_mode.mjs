// Offline checks for verified_capture and bounded deep options in xhs_bridge_driver.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import {executeBoundDom,captureBoundScreenshot,publicUrl,requirePinnedProfile,
  inspectDom,selectResults,scrollDiagnoseDom,requireScrollDiagnosticTarget,
  commentMediaStateDom,commentStructureDom,carouselDom,advanceDom} from './xhs_bridge_driver.mjs';
import {connectorStatus} from './xhs_connector_health.mjs';

const target='A'.repeat(32),other='B'.repeat(32),uid='1'.repeat(24),note='2'.repeat(24);
const url='https://www.xiaohongshu.com/explore/'+note;
const base={tab:target,contextId:'synthetic-profile',session:'xhs-synthetic-detail',uid,
  extensionVersion:'1.0.26',packageRoot:'/synthetic-pinned-opencli',expectedPath:'/explore/'+note,url};
const source=fs.readFileSync(new URL('./xhs_bridge_driver.mjs',import.meta.url),'utf8');
let mainCode=source.slice(source.indexOf('async function main(cfg) {'),source.indexOf('\nasync function respond(cfg) {'));
mainCode=mainCode.replace("const load = p => import(pathToFileURL(path.join(pkg,'dist/src',p)).href);",'const load=globalThis.fixtureLoad;')
  .replaceAll("const adapter=await import(pathToFileURL(path.join(pkg,'clis/xiaohongshu/comments.js')).href);",'const adapter=globalThis.fixtureAdapter;');
const domSource=fn=>`(function(opts){const selectResults=${selectResults.toString()};return (${fn.toString()})(opts);})`;

let checks=0;
const invoke=async(action,options={})=>{
  const calls=[],pageCalls=[],deepSeen=[];
  const fakePage={tabs:async()=>[{page:target,url:options.tabUrl ?? url,windowId:11}],
    setActivePage:value=>pageCalls.push(['bind',value]),
    evaluateOnce:()=>{throw new Error('UNBOUND_EVALUATE_USED');},
    selectTab:async value=>pageCalls.push(['select',value])};
  const command=async(type,params)=>{
    calls.push({type,params});const n=calls.length;
    return {page:options.wrongAt===n?other:target,
      data:options.dataByCall?.[n] ?? {uid,url,visible:true,body:'synthetic body',title:'title',media:[]}};
  };
  const modules={
    'browser/daemon-transport.js':{requestDaemon:async()=>({ok:true,json:async()=>({daemonVersion:'2.0.2',profiles:[{contextId:base.contextId,extensionVersion:'1.0.26'}]})})},
    'browser/daemon-client.js':{setDaemonCommandTimeoutSeconds:()=>{},sendCommandFull:command},
    'browser/daemon-lifecycle.js':{daemonLifecycleHooks:{}},
    'browser/utils.js':{buildEvaluateExpression:code=>code},
    'browser/bridge.js':{BrowserBridge:class{async connect(){return fakePage;}async close(){pageCalls.push(['close-reference']);}}}
  };
  const context={fs:{readFileSync:()=>JSON.stringify({name:'@jackwener/opencli',version:'2.0.2'})},path,pathToFileURL,URL,
    publicUrl,requirePinnedProfile,executeBoundDom,captureBoundScreenshot,domSource,inspectDom,commentMediaStateDom,commentStructureDom,connectorStatus,
    scrollDiagnoseDom,requireScrollDiagnosticTarget,carouselDom,advanceDom,
    buildDeepExtractor:(_fn,opts)=>{deepSeen.push(opts);return '(bounded deep expression)';},
    fixtureAdapter:{buildCommentsExtractJs:()=>{}},fixtureLoad:async name=>modules[name]};
  vm.createContext(context);vm.runInContext(mainCode+';globalThis.run=main;',context);
  try {return {result:await context.run({...base,action,...options.cfg}),calls,pageCalls,deepSeen};}
  catch(error){return {error,calls,pageCalls,deepSeen};}
};

// 1. verified_capture executes a single bound DOM evaluation and returns page + windowId.
{
  const v=await invoke('verified_capture',{cfg:{stage:'body',backgroundText:true}});
  assert.equal(v.error,undefined);
  assert.equal(v.calls.length,1);
  assert.equal(v.result.page,target);
  assert.equal(v.result.windowId,11);
  assert.equal(v.result.body,'synthetic body');
  assert.deepEqual(v.pageCalls,[['bind',target],['close-reference']]);
  checks++;
}

// 2. verified_capture rejects target mismatch, account mismatch, and access stop without leaking data.
{
  const mismatch=await invoke('verified_capture',{wrongAt:1,dataByCall:{1:{secret:'MUST_NOT_LEAK'}}});
  assert.equal(mismatch.error.message,'EXEC_TARGET_MISMATCH');
  assert.equal(mismatch.result,undefined);
  checks++;
  for(const err of ['ACCOUNT_MISMATCH','ACCESS_STOP','CONTENT_LOADING']) {
    const failed=await invoke('verified_capture',{dataByCall:{1:{error:err}}});
    assert.equal(failed.error.message,err);
    assert.equal(failed.calls.length,1);
    checks++;
  }
}

// 3. deep accepts bounded options and rejects out-of-range / non-integer values.
{
  const ok=await invoke('deep',{cfg:{deepRoots:50,deepReplyClicks:5,deepBudgetMs:14000},
    dataByCall:{1:{uid,url,visible:true},2:{results:[],deepCoverage:{rootPaginationState:'pending'}}}});
  assert.equal(ok.error,undefined);
  assert.equal(ok.deepSeen.length,1);
  assert.equal(ok.deepSeen[0].roots,50);
  assert.equal(ok.deepSeen[0].maxReplyClicks,5);
  assert.equal(ok.deepSeen[0].budgetMs,14000);
  checks++;
  for(const bad of [{deepRoots:0},{deepRoots:61},{deepRoots:'50'},{deepReplyClicks:7},{deepBudgetMs:1000},{deepBudgetMs:20000}]) {
    const rejected=await invoke('deep',{cfg:bad,dataByCall:{1:{uid,url,visible:true}}});
    assert.equal(rejected.error.message,'DEEP_OPTIONS_INVALID');
    assert.equal(rejected.calls.length,1);
    checks++;
  }
}

console.log(`PASS: bridge fast-mode and bounded deep checks: ${checks}`);

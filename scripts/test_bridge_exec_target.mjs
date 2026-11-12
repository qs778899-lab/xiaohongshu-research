// Offline real helper and real dispatch-function checks; no browser connection.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import {executeBoundDom,captureBoundScreenshot,publicUrl,requirePinnedProfile,
  inspectDom,selectResults,scrollDiagnoseDom,commentMediaStateDom,commentStructureDom,carouselDom,advanceDom} from './xhs_bridge_driver.mjs';

import {connectorStatus} from './xhs_connector_health.mjs';

const target='A'.repeat(32),other='B'.repeat(32),uid='1'.repeat(24),note='2'.repeat(24);
const url='https://www.xiaohongshu.com/explore/'+note;
const base={tab:target,contextId:'synthetic-profile',session:'xhs-synthetic-detail',uid,
  extensionVersion:'1.0.26',packageRoot:'/synthetic-pinned-opencli',expectedPath:'/explore/'+note,url};
const good={uid,url,visible:true};
let checks=0;
{
  const calls=[],data={body:'actual synthetic body',results:[{commentId:'3'.repeat(24),text:'complete text'}]};
  const result=await executeBoundDom(async(action,params)=>{calls.push({action,params});return {page:target,data};},base,'unchanged expression');
  assert.equal(result.data,data);assert.equal(result.page,target);assert.equal(calls.length,1);
  assert.equal(calls[0].action,'exec');assert.deepEqual(calls[0].params,{code:'unchanged expression',
    contextId:base.contextId,session:base.session,page:target,surface:'adapter',siteSession:'persistent',windowMode:'background'});
  checks++;
}
for(const actual of [undefined,null,77,'77','bad',other,target.toLowerCase()]) {
  let calls=0,readData=false;
  const returned={page:actual};Object.defineProperty(returned,'data',{get(){readData=true;throw new Error('UNBOUND_DATA_READ');}});
  await assert.rejects(()=>executeBoundDom(async()=>{calls++;return returned;},{...base,action:'comment_media_open'},'click exactly once'),
    error=>/^EXEC_TARGET_(MISMATCH|IDENTITY_UNCONFIRMED)$/.test(error.message) && JSON.parse(error.detail).noAutomaticReplay);
  assert.equal(calls,1);assert.equal(readData,false);checks++;
}
{
  let calls=0;
  await assert.rejects(()=>executeBoundDom(async()=>{calls++;},{...base,tab:77},'code'),/EXEC_REGISTERED_TARGET_REQUIRED/);
  assert.equal(calls,0);checks++;
}
{
  let calls=0;
  await assert.rejects(()=>executeBoundDom(async()=>{calls++;throw new Error('command_result_unknown');},base,'click'),/command_result_unknown/);
  assert.equal(calls,1);checks++;
}

// Evaluate the actual dispatch function with only its imports substituted.
// Fake Page.evaluateOnce throws so an unconverted call site cannot pass.
const source=fs.readFileSync(new URL('./xhs_bridge_driver.mjs',import.meta.url),'utf8');
let mainCode=source.slice(source.indexOf('async function main(cfg) {'),source.indexOf('\nasync function respond(cfg) {'));
mainCode=mainCode.replace("const load = p => import(pathToFileURL(path.join(pkg,'dist/src',p)).href);",'const load=globalThis.fixtureLoad;')
  .replaceAll("const adapter=await import(pathToFileURL(path.join(pkg,'clis/xiaohongshu/comments.js')).href);",'const adapter=globalThis.fixtureAdapter;');
assert.ok(!mainCode.includes('await page.evaluateOnce('));
const domSource=fn=>`(function(opts){const selectResults=${selectResults.toString()};return (${fn.toString()})(opts);})`;
const invoke=async(action,options={})=>{
  const calls=[],pageCalls=[];
  const fakePage={tabs:async()=>[{page:target,url}],setActivePage:value=>pageCalls.push(['bind',value]),
    evaluateOnce:()=>{throw new Error('UNBOUND_EVALUATE_USED');},selectTab:async value=>pageCalls.push(['select',value])};
  const command=async(type,params)=>{
    calls.push({type,params});const n=calls.length;
    if(options.throwAt===n)throw new Error(options.throwMessage || 'command_result_unknown');
    return {page:options.wrongAt===n?other:options.missingAt===n?undefined:target,
      data:options.errorAt===n?{error:options.guardError}:n===1?good:options.data ?? {body:'body',results:[],deepCoverage:{rootPaginationState:'pending'}}};
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
    scrollDiagnoseDom,carouselDom,advanceDom,buildDeepExtractor:()=>'(synthetic bounded deep expression)',
    fixtureAdapter:{buildCommentsExtractJs:()=>{}},fixtureLoad:async name=>{assert.ok(name in modules,name);return modules[name];}};
  vm.createContext(context);vm.runInContext(mainCode+';globalThis.run=main;',context);
  try {return {result:await context.run({...base,action,...options.cfg}),calls,pageCalls};}
  catch(error){return {error,calls,pageCalls};}
};
for(const action of ['identity','capture','deep','comment_diagnose','comment_media_state','comment_media_focus','comment_media_open','comment_media_viewer_state','media_state','media_select','advance','reload','scroll_diagnose']) {
  const options=action==='scroll_diagnose'?{cfg:{stage:'search',query:'synthetic',expectedPath:'/search_result'}}:action==='comment_diagnose'?{cfg:{stage:'body'}}:{};
  const result=await invoke(action,options);assert.equal(result.error,undefined,action+': '+result.error);
  assert.equal(result.calls.length,action==='identity'?1:2,action+' adds no browser call');
  assert.ok(result.calls.every(x=>x.type==='exec' && x.params.page===target && x.params.contextId===base.contextId));
  if(action==='identity')assert.equal(result.result.page,target);
  checks++;
}
for(const action of ['capture','deep','comment_diagnose','comment_media_state','comment_media_viewer_state']) {
  const result=await invoke(action,{wrongAt:2,data:{secret:'MUST_NOT_RETURN'},...(action==='comment_diagnose'?{cfg:{stage:'body'}}:{})});
  assert.equal(result.error.message,'EXEC_TARGET_MISMATCH');assert.equal(result.result,undefined);
  assert.equal(result.calls.length,2);assert.ok(!JSON.stringify(result.error).includes('MUST_NOT_RETURN'));checks++;
}
for(const action of ['identity','capture','deep','navigate']) {
  const result=await invoke(action,{cfg:{surface:'browser'}});
  assert.equal(result.error,undefined,action+': '+result.error);
  assert.ok(result.calls.length>0);
  assert.ok(result.calls.every(x=>x.params.surface==='browser' && x.params.page===target));
  checks++;
}
for(const action of ['create','close']) {
  const result=await invoke(action,{cfg:{surface:'browser'}});
  assert.match(result.error.message,/USER_TAB_(CREATE|CLOSE)_NOT_ALLOWED/);
  checks++;
}
for(const action of ['comment_media_focus','comment_media_open']) {
  const result=await invoke(action,{wrongAt:2,data:{interactionSubmitted:true,secret:'MUST_NOT_RETURN'}});
  assert.equal(result.error.message,'COMMAND_OUTCOME_UNKNOWN');assert.equal(result.result,undefined);
  assert.equal(result.calls.length,2,'one guard plus one interaction, no retry');
  assert.equal(JSON.parse(result.error.detail).actualPage,other);checks++;
}
{
  const result=await invoke('identity',{missingAt:1});
  assert.equal(result.error.message,'EXEC_TARGET_IDENTITY_UNCONFIRMED');assert.equal(result.result,undefined);checks++;
}
for(const guardError of ['ACCOUNT_MISMATCH','ACCESS_STOP']) {
  const result=await invoke('capture',{errorAt:1,guardError});assert.equal(result.error.message,guardError);
  assert.equal(result.calls.length,1);assert.equal(result.result,undefined);checks++;
}
{
  const data={body:'unchanged payload',results:[{commentId:'3'.repeat(24),text:'full comment'}],deepCoverage:{rootPaginationState:'pending'}};
  const result=await invoke('capture',{data});assert.equal(result.result,data,'validated data shape/object remains unchanged');checks++;
}
console.log(`PASS: ${checks} bound DOM exec cases, actual dispatch paths offline, no browser/network`);

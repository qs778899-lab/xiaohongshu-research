import assert from 'node:assert/strict';
import {captureBoundScreenshot} from './xhs_bridge_driver.mjs';

const expected='A'.repeat(32),other='B'.repeat(32);
const cfg={tab:expected,contextId:'synthetic-profile',session:'xhs-synthetic-detail',uid:'1'.repeat(24)};
const guardCode='synthetic exact-note / account guard expression';
const good={uid:cfg.uid,url:'https://www.xiaohongshu.com/explore/'+'2'.repeat(24),visible:true};
let checks=0;
const run=async(responses,options=cfg)=>{
  const calls=[];
  try {
    const result=await captureBoundScreenshot(async(action,params)=>{
      calls.push({action,params});const response=responses.shift();
      if(response instanceof Error)throw response;
      return response;
    },options,guardCode);
    return {result,calls};
  } catch(error) {return {error,calls};}
};
const response=(data,page=expected)=>({data,page});
{
  const {result,calls}=await run([response(good),response('synthetic-png'),response(good)]);
  assert.equal(result.pngBase64,'synthetic-png');assert.equal(result.page,expected);
  assert.equal(result.targetIdentitySpace,'cdp_target_id');assert.equal(result.targetVerified,true);
  assert.deepEqual(calls.map(x=>x.action),['exec','screenshot','exec']);
  for(const {params} of calls) {
    assert.equal(params.page,expected);assert.equal(params.contextId,cfg.contextId);
    assert.equal(params.session,cfg.session);assert.equal(params.surface,'adapter');
    assert.equal(params.siteSession,'persistent');assert.equal(params.windowMode,'background');
  }
  assert.equal(calls[0].params.code,guardCode);assert.equal(calls[2].params.code,guardCode);
  checks++;
}
for(const actual of [undefined,null,77,'77',{},'invalid-target',other,expected.toLowerCase()]) {
  const {result,error,calls}=await run([response(good),{data:'SECRET_PNG_MUST_BE_DISCARDED',page:actual},response(good)]);
  assert.equal(result,undefined);assert.match(error.message,/^SCREENSHOT_TARGET_(?:MISMATCH|IDENTITY_UNCONFIRMED)$/);
  assert.equal(calls.length,2,'target failure must stop before any further DOM reads');
  assert.ok(!JSON.stringify(error).includes('SECRET_PNG'),'no screenshot data in failure diagnostics');
  const detail=JSON.parse(error.detail);assert.equal(detail.pngDiscarded,true);
  assert.equal(detail.expectedPage,expected);assert.equal(detail.targetIdentitySpace,'cdp_target_id');
  checks++;
}
{
  const {error,calls}=await run([response(good,other)]);
  assert.equal(error.message,'SCREENSHOT_TARGET_MISMATCH');assert.equal(calls.length,1);checks++;
}
{
  const {result,error,calls}=await run([response(good),response('SECRET_PNG'),response(good,other)]);
  assert.equal(result,undefined);assert.equal(error.message,'SCREENSHOT_TARGET_MISMATCH');
  assert.equal(calls.length,3);assert.ok(!JSON.stringify(error).includes('SECRET_PNG'));checks++;
}
for(const code of ['TARGET_MISMATCH','ACCOUNT_MISMATCH','ACCESS_STOP']) {
  const before=await run([response({error:code})]);assert.equal(before.error.message,code);assert.equal(before.calls.length,1);
  const after=await run([response(good),response('SECRET_PNG'),response({error:code})]);
  assert.equal(after.error.message,code);assert.equal(after.result,undefined);assert.equal(after.calls.length,3);
  checks++;
}
for(const visible of [false,undefined,null,1]) {
  const observation={...good,visible};
  const before=await run([response(observation)]);
  assert.match(before.error.message,/^SCREENSHOT_(?:TARGET_NOT_VISIBLE|VISIBILITY_UNCONFIRMED)$/);
  assert.deepEqual(before.calls.map(x=>x.action),['exec'],'hidden/unknown visibility must prevent screenshot and further actions');
  assert.equal(before.result,undefined);assert.equal(JSON.parse(before.error.detail).phase,'before');
  assert.equal(JSON.parse(before.error.detail).screenshotRequested,false);
  const after=await run([response(good),response('SECRET_PNG'),response(observation)]);
  assert.match(after.error.message,/^SCREENSHOT_(?:TARGET_NOT_VISIBLE|VISIBILITY_UNCONFIRMED)$/);
  assert.equal(after.result,undefined);assert.deepEqual(after.calls.map(x=>x.action),['exec','screenshot','exec']);
  assert.equal(JSON.parse(after.error.detail).phase,'after');assert.equal(JSON.parse(after.error.detail).pngDiscarded,true);
  assert.ok(!JSON.stringify(after.error).includes('SECRET_PNG'));checks++;
}
{
  const {error,calls}=await run([],{...cfg,tab:77});
  assert.equal(error.message,'SCREENSHOT_REGISTERED_TARGET_REQUIRED');assert.equal(calls.length,0);checks++;
}
{
  const {error,calls}=await run([response(good),new Error('command_result_unknown')]);
  assert.equal(error.message,'command_result_unknown');assert.equal(calls.length,2);checks++;
}
{
  const {error,result}=await run([response(good),response(''),response(good)]);
  assert.equal(error.message,'SCREENSHOT_EMPTY');assert.equal(result,undefined);checks++;
}
console.log(`PASS: ${checks} exact-target screenshot cases; entirely offline, no browser or network`);

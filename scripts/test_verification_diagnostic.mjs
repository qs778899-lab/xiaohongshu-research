import assert from 'node:assert/strict';
import {test} from 'node:test';
import {diagnosticTabMetadata,publicUrl} from './xhs_bridge_driver.mjs';
const tab=url=>({id:1,windowId:2,active:true,url});
test('verification URL can be diagnosed but remains forbidden to collection',()=>{
  const url='https://www.xiaohongshu.com/website-login/captcha?redirectPath=%2Fexplore';
  const result=diagnosticTabMetadata(tab(url));
  assert.equal(result.url,url);assert.equal(result.pageKind,'verification_page');
  assert.equal(result.collectionPermitted,false);assert.equal(result.pageContentRead,false);
  assert.throws(()=>publicUrl(url),/ACCESS_STOP/);
});
test('normal URL does not prove missing overlay or completed verification',()=>{
  const result=diagnosticTabMetadata(tab('https://www.xiaohongshu.com/explore'));
  assert.equal(result.visibleChallenge,'not_checked');
  assert.equal(result.verificationCompleted,false);assert.equal(result.identityVerified,false);
});
test('error and login pages stay diagnostic only',()=>{
  for(const path of ['/404?error_code=123','/login','/website-login/example']) {
    const result=diagnosticTabMetadata(tab('https://www.xiaohongshu.com'+path));
    assert.equal(result.collectionPermitted,false);
    assert.throws(()=>publicUrl(result.url));
  }
});
test('unrelated origins, credentials and private paths are not inspected',()=>{
  for(const url of ['https://example.invalid/website-login/captcha',
    'https://secret@www.xiaohongshu.com/explore','http://www.xiaohongshu.com/explore',
    'https://www.xiaohongshu.com/message'])assert.throws(()=>diagnosticTabMetadata(tab(url)),/SCOPE_CHANGED/);
});
test('inactive or ambiguous native identity is rejected',()=>{
  for(const change of [{id:null},{windowId:null},{active:false}])
    assert.throws(()=>diagnosticTabMetadata({...tab('https://www.xiaohongshu.com/explore'),...change}),/NATIVE_IDS/);
});

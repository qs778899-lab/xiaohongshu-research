import assert from 'node:assert/strict';
import {inspectDom,publicUrl,requirePinnedProfile} from './xhs_bridge_driver.mjs';
const uid='1'.repeat(24);
globalThis.location={href:'https://private.example/secret'};
globalThis.document=new Proxy({}, {get(){throw new Error('PRIVATE_DOM_TOUCHED');}});
assert.equal(inspectDom({uid}).error,'SCOPE_CHANGED');
assert.throws(()=>publicUrl('https://www.xiaohongshu.com/chat'));
assert.throws(()=>publicUrl('https://www.xiaohongshu.com.evil/explore'));
location.href='https://www.xiaohongshu.com/explore';
let bodyRead=false;
globalThis.document={querySelectorAll(selector){
  if(selector==='a[href*="/user/profile/"]')return [{innerText:'我',href:'https://www.xiaohongshu.com/user/profile/'+'2'.repeat(24)}];
  if(selector.includes('role="dialog"'))return [];
  bodyRead=true;throw new Error('BODY_TOUCHED');
}};
assert.equal(inspectDom({uid}).error,'ACCOUNT_MISMATCH');assert.equal(bodyRead,false);
location.href='https://www.xiaohongshu.com/explore?error_code=300017';
assert.equal(inspectDom({uid}).error,'ACCESS_STOP');assert.equal(bodyRead,false);
console.log('PASS: private URL, account mismatch and restriction block before body extraction');
const profile_alpha={contextId:'profile_alpha-context',extensionVersion:'1.0.26'};
const profile_beta={contextId:'profile_beta-context',extensionVersion:'1.0.26'};
const status={daemonVersion:'2.0.2',contextId:'profile_beta-context',profiles:[profile_beta,profile_alpha]};
assert.equal(requirePinnedProfile(status,'profile_alpha-context','1.0.26'),profile_alpha);
assert.throws(()=>requirePinnedProfile({...status,profiles:[profile_beta]},'profile_alpha-context','1.0.26'),/SPECIFIED_PROFILE_OFFLINE/);
assert.throws(()=>requirePinnedProfile(null,'profile_alpha-context','1.0.26'),/CONNECTOR_OFFLINE/);
assert.throws(()=>requirePinnedProfile({...status,daemonVersion:'2.0.1'},'profile_alpha-context','1.0.26'),/DAEMON_CHANGED/);
assert.throws(()=>requirePinnedProfile(status,'profile_alpha-context','1.0.27'),/EXTENSION_CHANGED/);
console.log('PASS: explicit context wins over default/order; disconnected, old daemon and extension mismatch fail closed');

// AI search is a public search route, never a note or an identity bypass.
const ai='https://www.xiaohongshu.com/search_result_ai?keyword=Synthetic%2520query';
assert.equal(publicUrl(ai),ai);
for(const tail of ['', '?keyword=', '?keyword=%20', '?keyword=A&keyword=B', '/extra?keyword=A']) {
  assert.throws(()=>publicUrl('https://www.xiaohongshu.com/search_result_ai'+tail),/SCOPE_CHANGED/);
  location.href='https://www.xiaohongshu.com/search_result_ai'+tail;
  document=new Proxy({}, {get(){throw new Error('DOM_TOUCHED');}});
  assert.equal(inspectDom({uid}).error,'SCOPE_CHANGED');
}
location.href=ai;
document={hidden:true,querySelectorAll(selector){
  if(selector==='a[href*="/user/profile/"]')return [{innerText:'我',href:'https://www.xiaohongshu.com/user/profile/'+uid}];
  if(selector.includes('role="dialog"'))return [];
  throw new Error('BODY_TOUCHED');
}};
assert.equal(inspectDom({uid,guardOnly:true,expectedPath:'/search_result_ai',query:'Synthetic%20query'}).uid,uid);
assert.equal(inspectDom({uid,guardOnly:true,expectedPath:'/search_result',query:'Synthetic%20query'}).error,'TARGET_MISMATCH');
assert.equal(inspectDom({uid,guardOnly:true,expectedPath:'/search_result_ai',query:'Other'}).error,'QUERY_MISMATCH');
assert.equal(inspectDom({uid:'9'.repeat(24),guardOnly:true}).error,'ACCOUNT_MISMATCH');
location.href=ai+'&error_code=300017';assert.equal(inspectDom({uid}).error,'ACCESS_STOP');
console.log('PASS: exact AI search route/query, blank/duplicate keyword, identity and access guards');

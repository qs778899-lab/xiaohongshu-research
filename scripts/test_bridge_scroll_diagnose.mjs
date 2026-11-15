import assert from 'node:assert/strict';
import fs from 'node:fs';
import {selectResults,inspectDom,scrollDiagnoseDom,requireScrollDiagnosticTarget} from './xhs_bridge_driver.mjs';

const driver=fs.readFileSync(new URL('./xhs_bridge_driver.mjs',import.meta.url),'utf8');
const definition=driver.match(/function domSource\(fn\) \{[\s\S]*?\n\}/)[0];
const serialize=Function('selectResults',`${definition};return domSource`)(selectResults);
const uid='a'.repeat(24),query='示例机构 测试丙';
const opts={uid,query,stage:'search',expectedPath:'/search_result',guardOnly:true};
const diagnostic=eval(`(()=>{const guard=${serialize(inspectDom)},diagnose=${serialize(scrollDiagnoseDom)};return opts=>{const c=guard(opts);if(c.error)return c;return {url:c.url,uid:c.uid,...diagnose({includeListingText:opts.stage==='search'})};};})()`);
const mutation=()=>{throw Error('diagnostic must never scroll or interact');};
function element(tag,h,ch,overflow,parent=null){return {tagName:tag,className:tag.toLowerCase(),scrollHeight:h,clientHeight:ch,scrollTop:0,overflow,parentElement:parent,
  getClientRects:()=>[{}],getBoundingClientRect:()=>({x:0,y:0,width:1200,height:ch}),scrollTo:mutation,scrollBy:mutation,click:mutation};}
const html=element('HTML',2390,672,'visible'),body=element('BODY',2390,2390,'visible',html),
      page=element('DIV',2390,2390,'scroll',body),root=element('DIV',2109,2109,'visible',page);
let roots=[root],extraRoots=[root],styleReads=0;
globalThis.getComputedStyle=e=>{styleReads++;return {overflowY:e.overflow,overflowX:'visible',position:'static',display:'block',visibility:'visible',transform:'none',getPropertyValue:()=>'',getPropertyPriority:()=>''};};
globalThis.location={href:'https://www.xiaohongshu.com/search_result?keyword='+encodeURIComponent(query)};
globalThis.window={innerHeight:672,innerWidth:1200,scrollY:0,scrollX:0,pageYOffset:0,scrollTo:mutation,scrollBy:mutation,visualViewport:null};
globalThis.document={hidden:false,compatMode:'BackCompat',documentElement:html,body,scrollingElement:body,
  querySelectorAll(selector){
    if(selector==='a[href*="/user/profile/"]')return [{href:'https://www.xiaohongshu.com/user/profile/'+uid,innerText:'我'}];
    if(selector==='.feeds-container,.search-result-container')return roots;
    if(selector==='.feeds-container,.search-result-container,.search-layout,.main-content')return extraRoots;
    return [];
  }};

const mismatch=diagnostic(opts);
assert.equal(mismatch.selected.scroller.tag,'BODY');
assert.equal(mismatch.selected.scroller.scrollRange,0);
assert.equal(mismatch.documentElement.scrollRange,1718);
assert.equal(mismatch.compatMode,'BackCompat');
assert.equal(mismatch.viewport.innerHeight,672);
assert.equal(mismatch.viewport.scrollY,0);
assert.equal(mismatch.documentScrollingElement.isBody,true);
assert.equal(mismatch.readOnly,true);

document.scrollingElement=html;document.compatMode='CSS1Compat';window.scrollY=120;html.scrollTop=120;
const standard=diagnostic(opts);
assert.equal(standard.selected.scroller.tag,'HTML');
assert.equal(standard.documentScrollingElement.top,120);
assert.equal(standard.viewport.scrollY,120);

roots=[];assert.equal(diagnostic(opts).selected,null);
extraRoots=Array(20).fill(root);assert.equal(diagnostic(opts).containers.length,12);
const before=styleReads;
assert.equal(diagnostic({...opts,uid:'b'.repeat(24)}).error,'ACCOUNT_MISMATCH');
assert.equal(styleReads,before,'No geometry inspection after failed guard');
assert.equal(diagnostic({...opts,query:'另一查询'}).error,'QUERY_MISMATCH');
location.href='https://www.xiaohongshu.com/website-login/error?error_code=300017';
assert.equal(diagnostic(opts).error,'ACCESS_STOP');
console.log('scroll diagnostic: 7 read-only/serialization/guard scenarios passed');

const authorPath='/user/profile/'+ 'c'.repeat(24);
const authorOpts={uid,stage:'author',expectedPath:authorPath,guardOnly:true};
requireScrollDiagnosticTarget(opts);
requireScrollDiagnosticTarget(authorOpts);
for(const invalid of [{stage:'author'}, {...authorOpts,expectedPath:authorPath+'/note'},
  {...authorOpts,query:'unrelated'}, {...authorOpts,stage:'body'},
  {...opts,query:'  '}, {...opts,expectedPath:'/explore/'+ 'd'.repeat(24)}]) {
  assert.throws(()=>requireScrollDiagnosticTarget(invalid),/(?:AUTHOR|QUERY)_TARGET_REQUIRED/);
}
location.href='https://www.xiaohongshu.com'+authorPath;
roots=[root];extraRoots=[root];
Object.defineProperty(root,'innerText',{get(){throw Error('author diagnostic must not read listing text');}});
const authorResult=diagnostic(authorOpts);
assert.equal(authorResult.readOnly,true);
assert.equal(Object.hasOwn(authorResult,'renderedListing'),false);
const authorBefore=styleReads;
assert.equal(diagnostic({...authorOpts,expectedPath:'/user/profile/'+ 'e'.repeat(24)}).error,'TARGET_MISMATCH');
assert.equal(diagnostic({...authorOpts,uid:'f'.repeat(24)}).error,'ACCOUNT_MISMATCH');
assert.equal(styleReads,authorBefore);
location.href='https://www.xiaohongshu.com/website-login/error?error_code=300017';
assert.equal(diagnostic(authorOpts).error,'ACCESS_STOP');
console.log('author diagnostics: exact target, scope and read-only guards passed');

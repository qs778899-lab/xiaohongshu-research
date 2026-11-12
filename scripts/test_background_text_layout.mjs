// Exact inspectDom logic in a synthetic DOM: no Chrome, daemon or network.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {inspectDom,selectResults} from './xhs_bridge_driver.mjs';

const uid='1'.repeat(24),note='2'.repeat(24),other='3'.repeat(24);
const notePath='/explore/'+note,query='Synthetic query';
const noteUrl='https://www.xiaohongshu.com'+notePath;
let checks=0;

function fixture(options={}) {
  const element=(values={})=>{
    const node={innerText:'',clientWidth:700,clientHeight:600,scrollHeight:600,scrollTop:0,
      isConnected:true,parentElement:null,style:{display:'block',visibility:'visible',opacity:'1',overflowY:'visible'},
      rect:{width:700,height:600,top:0,left:0,right:700,bottom:600},rects:true,
      getClientRects(){return this.rects?[this.rect]:[];},getBoundingClientRect(){return this.rect;},
      closest(){return null;},querySelector(){return null;},querySelectorAll(){return [];},...values};
    return node;
  };
  const ancestor=element(),scope=element({parentElement:ancestor});
  if(options.scopeStyle)Object.assign(scope.style,options.scopeStyle);
  if(options.ancestorStyle)Object.assign(ancestor.style,options.ancestorStyle);
  if(options.scopeRect)Object.assign(scope.rect,options.scopeRect);
  if(options.scopeRects===false)scope.rects=false;
  if(options.disconnected)scope.isConnected=false;
  const scroller=element({clientWidth:700,clientHeight:600,scrollHeight:2400,
    style:{display:'block',visibility:'visible',opacity:'1',overflowY:'auto'}});
  if(options.scrollerStyle)Object.assign(scroller.style,options.scrollerStyle);
  if(options.scrollerWidth===0)scroller.clientWidth=0;
  if(options.scrollerHeight===0)scroller.clientHeight=0;
  if(options.listing){
    scope.parentElement=scroller;
    if(options.zeroWrapper){scope.clientHeight=0;scope.rect.height=0;scope.scrollHeight=1800;}
    if(options.noOverflow){scope.clientHeight=0;scope.rect.height=0;scope.scrollHeight=0;}
  }
  const link=element({href:noteUrl,innerText:'Synthetic search card'});
  const desc=element({innerText:'Actual synthetic body text'}),title=element({innerText:'Synthetic title'});
  scope.querySelector=selector=>selector==='#detail-desc,.note-text,.desc'?desc:selector==='#detail-title,.title'?title:null;
  scope.querySelectorAll=selector=>selector==='a[href]'&&options.listing?[link]:[];
  const me=element({innerText:'我',href:'https://www.xiaohongshu.com/user/profile/'+(options.wrongUid?other:uid)});
  const modal=element({innerText:'请完成验证后继续'});
  const pathname=options.wrongPath?'/explore/'+other:notePath;
  const href=options.url || (options.listing?'https://www.xiaohongshu.com/search_result/?keyword='+encodeURIComponent(query):'https://www.xiaohongshu.com'+pathname);
  const document={hidden:options.hidden!==false,scrollingElement:scroller,
    querySelector:selector=>selector==='.note-detail-mask,.note-detail-container,.note-container'?scope:null,
    querySelectorAll(selector){
      if(selector==='a[href*="/user/profile/"]')return options.noUid?[]:[me];
      if(selector==='.login-container,.login-modal,.captcha-container,[role="dialog"]')return options.accessModal?[modal]:[];
      if(selector==='.feeds-container,.search-result-container')return options.listing?[scope]:[];
      return [];
    }};
  const context={URL,document,location:{href},window:{innerWidth:options.viewportWidth??1280,innerHeight:options.viewportHeight??800},
    getComputedStyle:node=>node.style};
  vm.createContext(context);
  vm.runInContext('const selectResults='+selectResults.toString()+';globalThis.inspect='+inspectDom.toString()+';',context);
  return {run(extra={}){return context.inspect({uid,stage:options.listing?'search':'body',
      expectedPath:options.listing?'/search_result/':notePath,...(options.listing?{query}:{}),backgroundText:true,...extra});}};
}

for(const options of [{},{hidden:false},{scopeRect:{top:-5000,bottom:-4400}},
  {listing:true},{listing:true,zeroWrapper:true},{listing:true,zeroWrapper:true,scopeRect:{top:-4000,bottom:-4000}}]) {
  const out=fixture(options).run();assert.equal(out.error,undefined,JSON.stringify(options));
  assert.equal(out.visible,options.hidden===false);if(options.listing)assert.equal(out.cards.length,1);
  else assert.equal(out.body,'Actual synthetic body text');checks++;
}
for(const options of [{scopeStyle:{display:'none'}},{scopeStyle:{visibility:'hidden'}},
  {scopeStyle:{visibility:'collapse'}},{scopeStyle:{opacity:'0'}},{scopeStyle:{opacity:'0.0'}},
  {ancestorStyle:{opacity:'0'}},{ancestorStyle:{display:'none'}},{scopeRect:{width:0}},
  {scopeRect:{height:0}},{scopeRect:{width:NaN}},{scopeRects:false},{disconnected:true},
  {viewportWidth:0},{viewportHeight:0},{viewportHeight:Infinity},
  {listing:true,zeroWrapper:true,scopeStyle:{visibility:'hidden'}},
  {listing:true,zeroWrapper:true,scopeStyle:{opacity:'0'}},
  {listing:true,noOverflow:true},{listing:true,zeroWrapper:true,scrollerWidth:0},
  {listing:true,zeroWrapper:true,scrollerHeight:0},{listing:true,zeroWrapper:true,scrollerStyle:{display:'none'}}]) {
  assert.equal(fixture(options).run().error,'CONTENT_LOADING',JSON.stringify(options));checks++;
}
for(const [options,error] of [[{accessModal:true},'ACCESS_STOP'],[{wrongUid:true},'ACCOUNT_MISMATCH'],
  [{noUid:true},'IDENTITY_UNCONFIRMED'],[{wrongPath:true},'TARGET_MISMATCH'],
  [{url:'https://www.xiaohongshu.com/website-login/captcha'},'ACCESS_STOP'],
  [{url:'https://example.com/private'},'SCOPE_CHANGED']]) {
  assert.equal(fixture(options).run().error,error);checks++;
}
assert.equal(fixture({listing:true}).run({query:'Other query'}).error,'QUERY_MISMATCH');checks++;
// Opt-in only: existing visual/default and bare identity contracts are unchanged.
for(const backgroundText of [undefined,false,'true',1]) {
  const out=fixture({scopeStyle:{display:'none'},scopeRect:{width:0,height:0},viewportWidth:0}).run({backgroundText});
  assert.equal(out.error,undefined);assert.equal(out.body,'Actual synthetic body text');checks++;
}
const identity=fixture({scopeStyle:{display:'none'},scopeRect:{height:0},viewportHeight:0})
  .run({stage:undefined,backgroundText:undefined,guardOnly:true,expectedPath:undefined});
assert.equal(identity.uid,uid);assert.equal(identity.visible,false);assert.equal(identity.error,undefined);checks++;
// A guarded background search action still verifies its CSS before interaction.
assert.equal(fixture({listing:true,zeroWrapper:true,scopeStyle:{display:'none'}}).run({guardOnly:true}).error,'CONTENT_LOADING');checks++;
console.log(`PASS: ${checks} background text layout/identity cases; default behavior preserved; no browser/network`);

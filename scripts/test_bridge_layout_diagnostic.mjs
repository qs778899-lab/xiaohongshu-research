import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const candidate=fs.readFileSync(path.join(here,'xhs_bridge_driver.mjs'),'utf8');
const split=source=>{
  const start=source.indexOf('export function scrollDiagnoseDom() {');
  const end=source.indexOf('\nfunction domSource(fn)',start);
  assert(start>=0&&end>start);return {before:source.slice(0,start),fn:source.slice(start,end),after:source.slice(end)};
};
const fresh=split(candidate);
if(process.env.XHS_LAYOUT_DIAGNOSTIC_BASELINE){const old=split(fs.readFileSync(process.env.XHS_LAYOUT_DIAGNOSTIC_BASELINE,'utf8'));assert.equal(fresh.before,old.before);assert.equal(fresh.after,old.after);}
const style=values=>{
  const keys=Object.keys(values),result={...values,length:keys.length,
    getPropertyValue:key=>values[key]||'',getPropertyPriority:()=>''};
  keys.forEach((key,index)=>result[index]=key);return result;
};
const PRIVATE='PRIVATE_SENTINEL_SHOULD_NOT_APPEAR';
function fixture(extraRules=[]) {
  const computed=style({'height':'208px','min-height':'0px','max-height':'none','block-size':'208px',
    display:'block',position:'static','overflow-y':'scroll','overflow-x':'auto',overflow:'scroll',
    'box-sizing':'border-box',transform:'none',contain:'none',zoom:'1',
    '--screen-height':'208px','--huge-height':'1'.repeat(1000000)+'px',
    '--bad-height':`url(https://private.invalid/${PRIVATE})`,'--color':PRIVATE});
  const make=(tag,cls,height=208)=>({tagName:tag,className:cls,scrollHeight:569,clientHeight:height,scrollTop:361,
    innerText:'PUBLIC_LISTING_EXISTING_OUTPUT',style:style({'height':'208px','color':PRIVATE}),
    getBoundingClientRect:()=>({x:0,y:0,width:1249,height}),getClientRects:()=>[{}],
    querySelectorAll:()=>[],contains:()=>true,matches:()=>true,closest:()=>null,
    click(){throw Error('MUTATION');},scrollTo(){throw Error('MUTATION');},scrollBy(){throw Error('MUTATION');}});
  const html=make('HTML','ai-layout-active',666),body=make('BODY','ai-layout-active');body.parentElement=html;
  const scroller=make('DIV','feeds-page');let parent=body;
  for(let i=0;i<16;i++){const e=make('DIV',`layout-${i}`);e.parentElement=parent;parent=e;}
  scroller.parentElement=parent;
  const scope=make('DIV','feeds-container',0);scope.parentElement=scroller;
  scope.closest=()=>scroller;
  const cardList=Array.from({length:9},(_,i)=>make('SECTION','note-item',400+i));
  const links=cardList.map((card,i)=>({href:`https://www.xiaohongshu.com/explore/${i.toString(16).padStart(24,'0')}?xsec_token=${PRIVATE}`,
    closest:()=>card}));
  scope.querySelectorAll=selector=>selector==='a[href]'?links:[];
  const rules=[
    {type:1,selectorText:'html.ai-layout-active',style:style({'height':'208px','color':PRIVATE})},
    {type:1,selectorText:`[data-private="${PRIVATE}"]`,style:style({'height':`url(https://private.invalid/${PRIVATE})`})},
    {type:1,selectorText:`#${PRIVATE}`,style:style({'max-height':'208px'})},
    {type:1,selectorText:`#app-${PRIVATE}`,style:style({'max-height':'208px'})},
    {type:1,selectorText:`#app-root-${PRIVATE}`,style:style({'max-height':'208px'})},
    {type:1,selectorText:`#root-${PRIVATE}`,style:style({'max-height':'208px'})},
    {type:4,conditionText:'(max-height: 800px)',cssRules:[{type:1,selectorText:'.feeds-page',style:style({'height':'calc(100vh - var(--screen-height))'})}]},
    {type:4,conditionText:PRIVATE,cssRules:[{type:1,selectorText:'.feeds-page',style:style({'height':'208px'})}]},
    ...extraRules];
  const blocked={get cssRules(){const e=Error('PRIVATE_MESSAGE');e.name='SecurityError';throw e;},href:`https://private.invalid/${PRIVATE}`};
  const document={documentElement:html,body,scrollingElement:html,compatMode:'CSS1Compat',styleSheets:[{cssRules:rules,disabled:false},blocked],
    querySelectorAll:selector=>selector.includes('.feeds-container')?[scope]:[]};
  const window={innerHeight:666,innerWidth:1249,scrollY:0,scrollX:0,pageYOffset:0,visualViewport:null,matchMedia:()=>({matches:true})};
  const getComputedStyle=()=>computed;
  const run=new Function('selectResults','document','window','location','getComputedStyle',
    `return (${fresh.fn.replace('export ','')})();`);
  return run(()=>({scope,scroller}),document,window,{href:'https://www.xiaohongshu.com/search_result/?keyword=public'},getComputedStyle);
}
const result=fixture(),diag=result.layoutDiagnosis;
assert.equal(result.readOnly,true);assert.equal(diag.diagnosticOnly,true);assert.equal(diag.notAnEndSignal,true);
assert.equal(diag.nodes.length,12);assert.equal(diag.cardsGeometry.length,6);
assert.equal(diag.css.securityErrors,1);assert.equal(diag.css.cascadeResolved,false);
assert.equal(diag.nodes[0].computed.height,'208px');
assert.deepEqual(diag.nodes[0].heightVariables,[{property:'--screen-height',value:'208px'}]);
assert.equal(diag.nodes[0].inlineHeight[0].value,'208px');
assert(diag.css.matches.some(x=>x.selector==='html.ai-layout-active'&&x.declarations[0].value==='208px'));
assert(diag.css.matches.some(x=>x.selectorOmitted&&x.declarations[0].valueOmitted));
assert(diag.css.matches.some(x=>x.media.some(m=>m.condition==='(max-height: 800px)'&&m.active===true)));
assert(diag.css.matches.some(x=>x.media.some(m=>m.conditionOmitted)));
const serialized=JSON.stringify(diag);
assert(!serialized.includes(PRIVATE));assert(!serialized.includes('https:'));
assert(!serialized.includes('PUBLIC_LISTING_EXISTING_OUTPUT'));
assert(diag.cardsGeometry.every(x=>/^[a-f0-9]{24}$/.test(x.noteId)&&!('url'in x)&&!('title'in x)));
const many=Array.from({length:2100},()=>({type:1,selectorText:'.no-height',style:style({color:PRIVATE})}));
const limited=fixture(many).layoutDiagnosis;
assert.equal(limited.css.rulesVisited,2000);assert.equal(limited.css.truncated,true);
const manyMatches=fixture(Array.from({length:100},()=>({type:1,selectorText:'.feeds-page',style:style({height:'208px'})}))).layoutDiagnosis;
assert.equal(manyMatches.css.matches.length,32);assert.equal(manyMatches.css.truncated,true);
console.log('PASS: only scrollDiagnoseDom changed; 208px CSS/inline/variables; 12 nodes/6 cards; SecurityError; media/selector/value privacy; 2000-rule/32-match caps; no interactions');

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const dir=path.dirname(fileURLToPath(import.meta.url));
const installed=process.argv[2] || process.env.XHS_TEST_DRIVER_BASELINE;
const cut=source=>{
  const start=source.indexOf('export function commentStructureDom() {');
  const end=source.indexOf('\n// Exact observed-comment',start);
  assert(start>=0&&end>start);return {before:source.slice(0,start),fn:source.slice(start,end),after:source.slice(end)};
};
const candidate=cut(fs.readFileSync(path.join(dir,'xhs_bridge_driver.mjs'),'utf8'));
if(installed){
  const base=cut(fs.readFileSync(installed,'utf8'));
  assert.equal(candidate.before,base.before);assert.equal(candidate.after,base.after);
}
class El {
  constructor(cls='',text='',tag='DIV'){this.className=cls;this.textContent=text;this.tagName=tag;this.children=[];this.visible=true;this.clientHeight=100;this.scrollHeight=100;this.scrollTop=0;this.id='';}
  add(child){child.parentElement=this;this.children.push(child);return child;}
  contains(el){for(let n=el;n;n=n.parentElement)if(n===this)return true;return false;}
  matches(selector){return selector.split(',').some(part=>{part=part.trim();return part.startsWith('.')?this.className.split(/\s+/).includes(part.slice(1)):part.startsWith('#')?this.id===part.slice(1):this.tagName.toLowerCase()===part;});}
  closest(selector){for(let n=this;n;n=n.parentElement)if(n.matches(selector))return n;return null;}
  querySelectorAll(selector){const out=[];for(const child of this.children){if(child.matches(selector))out.push(child);out.push(...child.querySelectorAll(selector));}return out;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  getClientRects(){return this.visible?[{}]:[];}
  getBoundingClientRect(){return {width:200,height:100};}
  getAttribute(name){return name==='class'?this.className:name==='id'?this.id:null;}
}
const exact='这是一片荒地\n点击评论';
const scope=()=>{const s=new El('note-detail-container');return [s,s.add(new El('note-scroller'))];};
const run=s=>new Function('document','getComputedStyle','performance',`return (${candidate.fn.replace('export ','')})();`)(
  {hidden:false,querySelector:selector=>selector==='.note-detail-mask,.note-detail-container,.note-container'?s:null},
  e=>({display:e.visible?'block':'none',visibility:'visible',opacity:'1'}),{timeOrigin:1000});
{
  const [s,scroll]=scope(),wrapper=scroll.add(new El('empty-block',exact)),leaf=wrapper.add(new El('empty-text',exact,'SPAN'));
  const d=run(s);assert.equal(d.commentContainer.exists,false);assert.equal(d.emptyUiProbe.nodes.length,1);
  assert.deepEqual(d.emptyUiProbe.nodes[0].classes,['empty-text']);assert.equal(d.emptyUiProbe.nodes[0].insideComment,false);
  assert.equal(d.emptyUiProbe.nodes[0].insideBody,false);assert.equal(d.collectionPermitted,false);assert.equal(d.repairVerified,false);
  assert.equal(d.endSignalVisible,false);assert.equal('text'in d.emptyUiProbe.nodes[0],false);
}
for(const [owner,inside] of [['comment-item','insideComment'],['desc','insideBody']]){
  const [s,scroll]=scope(),parent=scroll.add(new El(owner));parent.add(new El('quoted-empty',exact,'SPAN'));
  const d=run(s);assert.equal(d.emptyUiProbe.nodes.length,1);assert.equal(d.emptyUiProbe.nodes[0][inside],true);
  assert.equal(d.collectionPermitted,false);assert.equal(d.repairVerified,false);
}
{
  const [s,scroll]=scope();s.add(new El('outside',exact));scroll.add(new El('ordinary','PRIVATE_USER says '+exact));
  scroll.add(new El('partial','这是一片荒地'));scroll.add(new El('partial','点击评论'));scroll.add(new El('hidden',exact)).visible=false;
  const d=run(s);assert.deepEqual(d.emptyUiProbe.nodes,[]);assert.equal(JSON.stringify(d).includes('PRIVATE_USER'),false);
}
{
  const [s,scroll]=scope();scroll.add(new El('empty',exact));scroll.visible=false;
  const d=run(s);assert.equal(d.emptyUiProbe.scrollerVisible,false);assert.equal(d.emptyUiProbe.scannedNodes,0);assert.deepEqual(d.emptyUiProbe.nodes,[]);
}
{
  const [s,scroll]=scope();let parent=scroll;
  for(let i=0;i<8;i++)parent=parent.add(new El('layout-'+i));
  const syntheticId='f'.repeat(24);
  parent.add(new El('empty user_'+syntheticId+' https://private.invalid/x '+('a'.repeat(65))+' a b c d e f g',exact));
  const d=run(s),node=d.emptyUiProbe.nodes[0];assert(node.classes.length<=6);assert(node.ancestors.length<=4);
  const serialized=JSON.stringify(d.emptyUiProbe);assert(!serialized.includes(syntheticId));assert(!serialized.includes('https:'));
}
{
  const [s,scroll]=scope();for(let i=0;i<250;i++)scroll.add(new El(i<6?'empty':'ordinary',i<6?exact:'PRIVATE_TEXT'));
  const d=run(s);assert.equal(d.emptyUiProbe.scannedNodes,240);assert.equal(d.emptyUiProbe.truncated,true);assert.equal(d.emptyUiProbe.nodes.length,4);
  assert.equal(JSON.stringify(d).includes('PRIVATE_TEXT'),false);
}
console.log('PASS fixed exact-phrase structure only; missing-container UI; body/comment flags; hidden/outside/partial rejection; no text/URL/UID; 240/4/4/6 bounds; no END/permission change; baseline diff '+(installed?'checked':'skipped (no baseline supplied)'));

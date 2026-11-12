// Masonry readiness from real note-wrapper geometry, not titles or viewport intersection.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {inspectDom,selectResults} from './xhs_bridge_driver.mjs';

const uid='1'.repeat(24),query='Synthetic query';let checks=0;
function read(spec={}) {
  const element=(rect={},style={})=>({rect:{x:0,y:0,width:1000,height:600,...rect},
    style:{display:'block',visibility:'visible',opacity:'1',position:'relative',transform:'none',overflowY:'visible',...style},
    clientWidth:1000,clientHeight:600,scrollHeight:1800,scrollTop:0,isConnected:true,parentElement:null,
    innerText:'',getClientRects(){return [this.rect];},getBoundingClientRect(){return this.rect;},
    querySelector(){return null;},querySelectorAll(){return [];},closest(){return null;}});
  const scroller=element({}, {overflowY:'auto'});
  const scope=element({height:spec.zeroWrapper?0:1500});scope.parentElement=scroller;
  if(spec.zeroWrapper)scope.clientHeight=0;
  const positions=spec.positions??[[188,-227],[188,-227],[188,-227]];
  const wrappers=positions.map(([x,y],index)=>{
    const card=element({x,y,width:249,height:270+index*40},
      {position:spec.position??'absolute',transform:spec.transform??'none'});
    card.parentElement=scope;return card;
  });
  const links=wrappers.map((card,index)=>({innerText:'Synthetic card '+index,
    href:'https://www.xiaohongshu.com/search_result/'+String(spec.sameId?10:10+index).padStart(24,'0'),
    getClientRects:()=>[{}],closest:()=>spec.sameWrapper?wrappers[0]:card}));
  scope.querySelectorAll=selector=>selector==='a[href]'?links:[];
  scope.contains=card=>wrappers.includes(card);
  const me={innerText:'我',href:'https://www.xiaohongshu.com/user/profile/'+uid};
  const document={hidden:true,scrollingElement:scroller,querySelector:()=>null,
    querySelectorAll(selector){
      if(selector==='a[href*="/user/profile/"]')return [me];
      if(selector==='.feeds-container,.search-result-container')return [scope];
      return [];
    }};
  const context={URL,document,location:{href:'https://www.xiaohongshu.com/search_result/?keyword='+encodeURIComponent(query)},
    window:{innerWidth:1280,innerHeight:666},getComputedStyle:node=>node.style};
  vm.createContext(context);vm.runInContext('const selectResults='+selectResults.toString()+';globalThis.inspect='+inspectDom.toString(),context);
  return context.inspect({uid,stage:'search',query,expectedPath:'/search_result/',backgroundText:spec.backgroundText??true,
    guardOnly:spec.guardOnly??false});
}
for(const spec of [{},{zeroWrapper:true},{positions:[[0,0],[2,3],[4,5]]}]) {
  assert.equal(read(spec).error,'BACKGROUND_LAYOUT_UNSETTLED');checks++;
}
for(const spec of [
  {positions:[[0,0],[260,0],[520,0]]}, // Same row is normal: the x coordinates differ.
  {positions:[[0,0],[260,0],[0,400],[260,400]]},
  {zeroWrapper:true,positions:[[0,-1800],[260,-1800],[0,-1400]]},
  {positions:[[188,-227],[188,-227]]}, // Two cards do not establish a collapsed layout.
  {sameId:true},{sameWrapper:true},{position:'static'},
  {transform:'matrix(1, 0, 0, 1, 0, 0)'}, // Exact observed none is required.
  {backgroundText:false},{backgroundText:'true'},
]) {
  const result=read(spec);assert.equal(result.error,undefined,JSON.stringify(spec));
  assert.equal(result.visible,false);checks++;
}
assert.equal(read({guardOnly:true}).error,'BACKGROUND_LAYOUT_UNSETTLED');checks++;
console.log(`PASS: ${checks} background masonry-readiness cases; no Chrome/network or CSS mutation`);

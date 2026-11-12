// Offline original-asset display transaction: no browser or pixel/network API.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {commentMediaStateDom,inspectDom} from './xhs_bridge_driver.mjs';
const UID='1'.repeat(24),NOTE='a'.repeat(24),CID='b'.repeat(24);
const URL='https://fixture.xhscdn.com/202601011200/cccccccccccccccccccccccccccccccc/comment/syntheticasset0000000000000001!nc_n_webp_mw_1';
const allowed=['width','height','max-width','max-height','object-fit','overflow'];
const code=commentMediaStateDom.toString().replace('GUARD({...opts,guardOnly:true})',`(${inspectDom.toString()})({...opts,guardOnly:true})`);
const copy=value=>JSON.parse(JSON.stringify(value));
function fixture() {
  const writes=[],state={source:URL,onWrite:null,forceClip:false,operations:0};
  class Style {
    constructor(role,initial={}) {this.role=role;this.values=new Map(Object.entries(initial).map(([k,v])=>[k,typeof v==='string'?{value:v,priority:''}:v]));}
    getPropertyValue(k) {return this.values.get(k)?.value || '';}
    getPropertyPriority(k) {return this.values.get(k)?.priority || '';}
    setProperty(k,value,priority='') {assert.ok(allowed.includes(k));writes.push({role:this.role,k,value,priority});
      state.onWrite?.();this.values.set(k,{value,priority});}
    removeProperty(k) {assert.ok(allowed.includes(k));writes.push({role:this.role,k,value:'',priority:''});state.onWrite?.();this.values.delete(k);}
  }
  class Element {
    constructor(tagName,className,attrs={},role='',initial={}) {this.tagName=tagName;this.className=className;this.attrs=attrs;this.parentElement=null;
      this.role=role;this.children=[];this.style=new Style(role,{color:'red',...initial});this.isConnected=true;
      this.classList={contains:value=>this.className.split(/\s+/).includes(value)};}
    add(e) {this.children.push(e);e.parentElement=this;return e;}
    getAttribute(k) {return this.attrs[k] ?? null;}
    hasAttribute(k) {return k in this.attrs;}
    querySelector(k) {if(k==='.comment-item')return this.children.find(e=>e.className==='comment-item') || null;throw new Error(k);}
    querySelectorAll(k) {
      const all=[];const visit=e=>{for(const child of e.children){all.push(child);visit(child);}};visit(this);
      if(k==='img')return all.filter(e=>e.tagName==='IMG');
      if(k==='.reply-container .comment-item-sub, .sub-comment-list .comment-item')return [];
      throw new Error(k);
    }
    closest(k) {for(let e=this;e;e=e.parentElement){
      if(k==='.content, .note-text' && /^(content|note-text)$/.test(e.className))return e;
      if(k==='.comment-item, .comment-item-sub' && /^(comment-item|comment-item-sub)$/.test(e.className))return e;
      if(k.includes('[class*="comment-pic"]') && e.className==='comment-picture')return e;
    }return null;}
    getBoundingClientRect() {
      const width=Number.parseFloat(this.style.getPropertyValue('width')) || (this.role==='commentPicture'?307:160);
      const height=Number.parseFloat(this.style.getPropertyValue('height')) || (this.role==='commentPicture'?120:120);
      const actualWidth=state.forceClip && this.role==='image'?160:width;
      const top=this.role==='commentPicture'?100:108,totalHeight=height+(this.role==='commentPicture'?8:0);
      return {x:600,y:top,left:600,top,right:600+actualWidth,bottom:top+totalHeight,width:actualWidth,height:totalHeight};
    }
    getClientRects() {return [this.getBoundingClientRect()];}
    click() {state.operations++;throw new Error('No click');}
    scrollIntoView() {state.operations++;throw new Error('No scroll');}
  }
  const parent=new Element('DIV','parent-comment'),item=parent.add(new Element('DIV','comment-item',{'data-comment-id':CID}));
  const picture=item.add(new Element('DIV','comment-picture',{},'commentPicture',{overflow:'clip'}));picture.clientWidth=307;
  const box=picture.add(new Element('DIV','img-box',{},'imgBox',{width:{value:'160px',priority:'important'},height:'120px'}));
  const image=box.add(new Element('IMG','inner',{src:URL},'image',{'max-height':{value:'120px',priority:'important'}}));
  image.naturalWidth=640;image.naturalHeight=480;image.complete=true;
  Object.defineProperties(image,{currentSrc:{get:()=>state.source,set:()=>{throw new Error('No source changes');}},src:{get:()=>state.source,set:()=>{throw new Error('No source changes');}}});
  const me={innerText:'我',href:'https://www.xiaohongshu.com/user/profile/'+UID};
  const document={hidden:false,querySelectorAll(selector){
    if(selector==='a[href*="/user/profile/"]')return [me];
    if(selector==='.login-container,.login-modal,.captcha-container,[role="dialog"]')return [];
    if(selector==='.parent-comment')return [parent];throw new Error('Unexpected broad DOM access: '+selector);
  }};
  Object.defineProperty(document,'body',{get:()=>{throw new Error('No body text');}});
  const context={document,location:{href:'https://www.xiaohongshu.com/explore/'+NOTE},window:{innerWidth:1249,innerHeight:672},URL:globalThis.URL,
    getComputedStyle:e=>({display:'block',visibility:'visible',opacity:'1',cursor:'zoom-in',transform:'none',boxSizing:'content-box',
      paddingLeft:'0px',paddingRight:'0px',paddingTop:e===picture?'8px':'0px',paddingBottom:'0px',borderTopWidth:'0px',borderBottomWidth:'0px'}),
    fetch:()=>{throw new Error('No network');}};
  const run=(action='comment_media_display_state',extra={})=>vm.runInNewContext(`(${code})(${JSON.stringify({action,uid:UID,expectedPath:'/explore/'+NOTE,commentId:CID,imageUrl:URL,...extra})})`,context);
  return {run,state,writes,image,box,picture,me,context};
}
function enlarge(f,before) {return f.run('comment_media_enlarge',{displayIntentId:'saved-intent-001',expectedDisplayState:before});}
{
  const f=fixture(),before=f.run();assert.equal(before.gap,null);assert.equal(f.writes.length,0);
  assert.equal(before.displayState.nodes.length,3);assert.equal(before.displayState.nodes[0].inline['max-height'].priority,'important');
  assert.equal(before.displayPlan.width,307);assert.equal(before.displayPlan.height,230.25);
  const original=copy(before.displayState),after=enlarge(f,original);
  assert.equal(after.enlarged,true);assert.equal(after.fullImageGeometryConfirmed,true);assert.equal(after.notAVisualReview,true);
  assert.equal(after.displayState.nodes[0].inline.width.value,'307px');assert.equal(after.displayState.nodes[0].inline.height.value,'230.25px');
  assert.equal(after.displayState.nodes[0].inline['object-fit'].value,'contain');assert.equal(after.displayState.nodes[0].inline.width.priority,'important');
  const restored=f.run('comment_media_restore',{displayIntentId:'saved-intent-001',expectedDisplayState:after.displayState,originalDisplayState:original});
  assert.equal(restored.restored,true);assert.equal(restored.notAVisualReview,true);
  assert.equal(JSON.stringify(restored.displayState.nodes.map(n=>n.inline)),JSON.stringify(original.nodes.map(n=>n.inline)));
  assert.equal(f.image.style.getPropertyValue('color'),'red');assert.equal(f.box.style.getPropertyValue('color'),'red');
  assert.ok(f.writes.every(w=>allowed.includes(w.k)));assert.equal(f.state.source,URL);assert.equal(f.state.operations,0);
}
{
  const f=fixture(),before=f.run().displayState;
  assert.equal(f.run('comment_media_enlarge',{expectedDisplayState:before}).gap,'COMMENT_DISPLAY_SAVED_INTENT_REQUIRED');
  const stale=copy(before);stale.nodes[0].inline.width.value='different';
  assert.equal(enlarge(f,stale).gap,'COMMENT_DISPLAY_STATE_CHANGED');assert.equal(f.writes.length,0);
  const extra=copy(before);extra.nodes[0].inline.position={value:'fixed',priority:'important'};
  assert.equal(enlarge(f,extra).gap,'COMMENT_DISPLAY_STATE_CHANGED');assert.equal(f.writes.length,0);
  const other=copy(before);other.binding.commentId='9'.repeat(24);
  assert.equal(enlarge(f,other).gap,'COMMENT_DISPLAY_STATE_CHANGED');assert.equal(f.writes.length,0);
  assert.equal(f.run('comment_media_enlarge',{displayIntentId:'saved-intent-001',expectedDisplayState:before,selector:'body'}).error,'COMMENT_DISPLAY_OPTIONS_NOT_ALLOWED');
}
{
  const f=fixture(),before=f.run().displayState,after=enlarge(f,before),count=f.writes.length;
  const forged=copy(before);forged.nodes[2].inline.position={value:'fixed',priority:''};
  assert.equal(f.run('comment_media_restore',{displayIntentId:'saved-intent-001',expectedDisplayState:after.displayState,originalDisplayState:forged}).gap,'COMMENT_DISPLAY_ORIGINAL_STATE_REQUIRED');
  f.image.style.values.set('width',{value:'300px',priority:'important'});
  assert.equal(f.run('comment_media_restore',{displayIntentId:'saved-intent-001',expectedDisplayState:after.displayState,originalDisplayState:before}).gap,'COMMENT_DISPLAY_STATE_CHANGED');
  assert.equal(f.writes.length,count,'A stale restore cannot clobber a subsequent display change');
}
{
  for(const kind of ['long','hidden','unloaded','responsive','short-viewport']) {
    const f=fixture();
    if(kind==='long')f.image.naturalHeight=2000;
    if(kind==='hidden')f.context.document.hidden=true;
    if(kind==='unloaded')f.image.complete=false;
    if(kind==='responsive')f.image.attrs.srcset=URL+' 1x';
    if(kind==='short-viewport')f.context.window.innerHeight=200;
    const before=f.run();assert.ok(before.gap);assert.ok(enlarge(f,before.displayState).gap);assert.equal(f.writes.length,0,kind);
  }
}
{
  const f=fixture(),before=f.run().displayState;f.state.forceClip=true;
  const r=enlarge(f,before);assert.equal(r.rolledBack,true);assert.equal(r.gap,'COMMENT_DISPLAY_FULL_IMAGE_UNCONFIRMED');
  assert.equal(JSON.stringify(r.displayState.nodes.map(n=>n.inline)),JSON.stringify(before.nodes.map(n=>n.inline)));
}
{
  const f=fixture(),panel=f.picture.parentElement.parentElement,baseStyle=f.context.getComputedStyle;
  panel.clientWidth=700;panel.clientHeight=250;
  panel.getBoundingClientRect=()=>({left:500,top:0,right:1200,bottom:250,width:700,height:250});
  f.context.getComputedStyle=node=>({...baseStyle(node),...(node===panel?{overflowY:'auto',overflowX:'hidden'}:{})});
  const before=f.run();assert.equal(before.displayPlan.clippingBounds.bottom,250);
  assert.equal(before.gap,'COMMENT_DISPLAY_VIEWPORT_TOO_SMALL','Fitting the browser viewport alone does not prove a clipped comment panel can show the full image');
  assert.equal(enlarge(f,before.displayState).gap,'COMMENT_DISPLAY_VIEWPORT_TOO_SMALL');assert.equal(f.writes.length,0);
}
{
  const f=fixture(),before=f.run().displayState;let calls=0;
  f.state.onWrite=()=>{if(++calls===3)throw new Error('one style write failed');};
  const r=enlarge(f,before);assert.equal(r.rolledBack,true);assert.equal(r.gap,'COMMENT_DISPLAY_MUTATION_FAILED');
  assert.equal(JSON.stringify(r.displayState.nodes.map(n=>n.inline)),JSON.stringify(before.nodes.map(n=>n.inline)));
}
{
  const f=fixture(),before=f.run().displayState;
  f.state.onWrite=()=>{f.me.href='https://www.xiaohongshu.com/user/profile/'+'9'.repeat(24);};
  const r=enlarge(f,before);assert.equal(r.error,'ACCOUNT_MISMATCH');assert.equal(f.writes.length,1,'A guard change stops subsequent property writes');
  const g=fixture(),original=g.run().displayState;g.state.source=URL.replace('syntheticasset','differentasset');
  assert.equal(enlarge(g,original).gap,'COMMENT_MEDIA_SOURCE_MISMATCH');assert.equal(g.writes.length,0);
}
console.log('PASS: three-node six-property original-asset display/restore, exact prior state/priority, full viewport ratio, long/responsive/unloaded gaps, partial rollback and scope/source stops; no click/scroll/pixels/network');

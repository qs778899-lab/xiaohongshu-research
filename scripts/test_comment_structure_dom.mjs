import assert from 'node:assert/strict';
import {commentStructureDom} from './xhs_bridge_driver.mjs';
class El {
  constructor(text='',visible=true,attrs={}){this.textContent=text;this.visible=visible;this.attrs=attrs;this.one={};this.many={};this.clientHeight=100;this.scrollHeight=100;this.scrollTop=0;this.nested=false;}
  getClientRects(){return this.visible?[{}]:[]}
  getBoundingClientRect(){return {height:this.clientHeight,width:200,top:10,bottom:110}}
  getAttribute(k){return this.attrs[k]||null}
  querySelector(s){return this.one[s]||null}
  querySelectorAll(s){return this.many[s]||[]}
  closest(){return this.nested?{}:null}
}
globalThis.getComputedStyle=e=>({display:e.visible?'block':'none',visibility:'visible',opacity:'1'});
const scope=new El(),container=new El(),header=new El(),interaction=new El('评论');
scope.one['.comments-container']=container;scope.one['.interact-container .chat-wrapper .count']=interaction;container.one['.total']=header;
globalThis.document={hidden:false,querySelector:s=>s==='.note-detail-mask,.note-detail-container,.note-container'?scope:null};
let d=commentStructureDom();assert.equal(d.commentContainer.exists,true);assert.equal(d.rootNodeCount,0);assert.equal(d.countHeading.text,null);assert.equal(d.interactionCount.text,'评论');assert.deepEqual(d.messages,[]);assert.equal(d.endSignalVisible,false);
const roots=[new El('',true,{'data-id':'a'.repeat(24)}),new El('',true,{id:'comment-'+ 'b'.repeat(24)}),new El('',true,{'data-comment-id':'c'.repeat(24)}),new El('',true,{'data-id':'d'.repeat(24)})];
container.many['.parent-comment']=roots;d=commentStructureDom();assert.equal(d.rootNodeCount,4);assert.deepEqual(d.rootIds,['a'.repeat(24),'b'.repeat(24),'c'.repeat(24)]);assert.equal(JSON.stringify(d).includes('patient'),false);
header.textContent='共 17 条评论';d=commentStructureDom();assert.equal(d.countHeading.text,'共 17 条评论');
for(const text of ['138.0013.8000','138,0013,8000','138 0013 8000','共 138.0013.8000 条评论','138.00.13','1,23']) {header.textContent=text;d=commentStructureDom();assert.equal(d.countHeading.text,null,text);assert.equal(JSON.stringify(d).includes(text),false);}
for(const text of ['共 17 条评论','1,234','共1,234条评论','1.2万','0','评论']) {header.textContent=text;d=commentStructureDom();assert.equal(d.countHeading.text,text);}
const empty=new El('暂无评论'),closed=new El('作者已关闭评论'),app=new El('打开小红书APP查看更多评论');
container.many['.empty']=[empty];container.many['.comment-disabled']=[closed];container.many['.open-app']=[app];
d=commentStructureDom();assert.deepEqual(d.messages.map(x=>x.type),['empty','closed','app_required']);assert.equal(d.endSignalVisible,false);assert.equal(d.repairVerified,false);assert.equal(d.collectionPermitted,false);
const quoted=new El('暂无评论');quoted.nested=true;container.many['.empty']=[quoted];d=commentStructureDom();assert.equal(d.messages.some(x=>x.type==='empty'),false);
const privateUi=new El('暂无评论，我的电话13800138000，微信patient_contact');container.many['.empty']=[privateUi];d=commentStructureDom();assert.equal(d.unrecognizedUiText,true);assert.equal(JSON.stringify(d).includes('13800138000'),false);assert.equal(JSON.stringify(d).includes('patient_contact'),false);
header.textContent='13800138000';d=commentStructureDom();assert.equal(d.countHeading.text,null);
header.textContent='这是patient的评论13800138000';d=commentStructureDom();assert.equal(d.countHeading.text,null);assert.equal(d.countHeading.textWithheld,true);assert.equal(JSON.stringify(d).includes('patient'),false);
container.many['.empty']=[new El('暂无评论',false)];d=commentStructureDom();assert.equal(d.messages.some(x=>x.type==='empty'),false);
container.many['.loading,.loading-container,[aria-busy="true"]']=[new El('Loading')];d=commentStructureDom();assert.equal(d.loadingVisible,true);
container.many['.end-container,.end,.list-end,.no-more']=[new El('THE END')];d=commentStructureDom();assert.equal(d.endSignalVisible,true);
scope.one['.comments-container']=null;d=commentStructureDom();assert.equal(d.commentContainer.exists,false);assert.equal(d.rootNodeCount,0);assert.deepEqual(d.messages,[]);
console.log('comment structure DOM: 10 synthetic behavior/privacy cases passed');

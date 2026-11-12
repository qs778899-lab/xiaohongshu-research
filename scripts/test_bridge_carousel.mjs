import assert from 'node:assert/strict';
import {carouselDom} from './xhs_bridge_driver.mjs';
let current=0,calls=[],roots;
const rect={x:0,y:0,width:600,height:800};
function element(tagName,fields={}) {
  return {tagName,style:{display:'block',visibility:'visible',opacity:'1',backgroundImage:'none'},
    getClientRects:()=>[rect],getBoundingClientRect:()=>({...rect}),getAttribute:()=>null,
    querySelectorAll:()=>[],...fields};
}
function image(fields={}) {return element('IMG',{src:'https://observed-image/image.webp',currentSrc:'https://observed-image/image.webp',
  complete:true,naturalWidth:1000,naturalHeight:1200,loading:'eager',...fields});}
function slide(i,children=[]) {return element('DIV',{children,getAttribute:k=>k==='data-swiper-slide-index'?String(i):null,
  classList:{contains:()=>i===current},querySelectorAll:function(s){return s==='*'?this.children:this.children.filter(x=>s===x.tagName.toLowerCase());}});}
let slides;
function reset(children) {
  current=0;calls=[];slides=Array.from({length:8},(_,i)=>slide(i,[image()]));
  if(children)slides[0].children=children;
  const root=element('DIV',{querySelectorAll:s=>s==='.swiper-slide'?slides:[],swiper:{slideToLoop:i=>{calls.push(i);current=i;}}});
  roots=[root];return root;
}
globalThis.document={querySelectorAll:()=>roots};
globalThis.getComputedStyle=e=>e.style;
reset();
let state=carouselDom({action:'media_state'});
assert.equal(state.index,0);assert.equal(state.count,8);assert.equal(state.kind,'image');assert.equal(state.screenshotReady,true);
assert.equal(state.mediaEvidence.images[0].naturalWidth,1000);assert.equal(state.mediaEvidence.images[0].loaded,true);
assert.equal(carouselDom({action:'media_select',index:7,expectedIndex:1,expectedCount:8}).error,'MEDIA_POSITION_CHANGED');
assert.equal(carouselDom({action:'media_select',index:8,expectedIndex:0,expectedCount:8}).error,'MEDIA_INDEX_INVALID');
assert.equal(calls.length,0);
assert.equal(carouselDom({action:'media_select',index:7,expectedIndex:0,expectedCount:8}).selectionSubmitted,true);
assert.equal(carouselDom({action:'media_state'}).index,7);assert.deepEqual(calls,[7]);
roots=[roots[0],roots[0]];assert.equal(carouselDom({action:'media_state'}).error,'MEDIA_CONTAINER_UNAVAILABLE');

reset([]);state=carouselDom({action:'media_state'});
assert.equal(state.kind,'empty_or_unloaded');assert.equal(state.screenshotReady,false);
assert.equal(state.mediaEvidence.images.length,0);assert.equal(state.mediaEvidence.notANoMediaProof,true);
assert.equal(state.mediaEvidence.notAVisualReview,true);assert.equal(state.mediaEvidence.activeSlide.rect.height,800);
assert.equal(carouselDom({action:'media_select',index:0,expectedIndex:0,expectedCount:8}).error,'MEDIA_EMPTY_OR_UNLOADED');
assert.equal(calls.length,0);
for(const candidate of [image({complete:false,naturalWidth:0,naturalHeight:0}),image({naturalWidth:0,naturalHeight:0}),
                        image({src:'',currentSrc:''}),image({naturalWidth:1,naturalHeight:1}),
                        image({getClientRects:()=>[]}),image({style:{display:'none',visibility:'visible',opacity:'1'}}),
                        image({getBoundingClientRect:()=>({...rect,width:0})})]) {
  reset([candidate]);state=carouselDom({action:'media_state'});
  assert.equal(state.kind,'empty_or_unloaded');assert.equal(state.mediaEvidence.images.length,1);
}
reset([]);slides[0].style.backgroundImage='url("https://observed-image/background.webp")';
state=carouselDom({action:'media_state'});assert.equal(state.kind,'empty_or_unloaded');
assert.deepEqual(state.mediaEvidence.backgrounds[0].urls,['https://observed-image/background.webp']);
assert.equal(state.mediaEvidence.backgrounds[0].loadState,'not_observable_from_css');
assert.deepEqual(state.mediaEvidence.potentialKinds,['background_image']);
reset([]);slides[0].style.backgroundImage='linear-gradient(white, white)';
assert.equal(carouselDom({action:'media_state'}).mediaEvidence.backgrounds.length,0);
const video=element('VIDEO',{currentSrc:'https://observed-video/video.mp4',readyState:2,networkState:1,videoWidth:1280,videoHeight:720});
reset([video]);state=carouselDom({action:'media_state'});assert.equal(state.kind,'video');assert.equal(state.mediaEvidence.videos[0].loaded,true);
reset([{...video,readyState:1},image()]);state=carouselDom({action:'media_state'});
assert.equal(state.kind,'empty_or_unloaded');assert.deepEqual(state.mediaEvidence.potentialKinds,['image','video']);
reset();roots[0].style.opacity='0';assert.equal(carouselDom({action:'media_state'}).kind,'empty_or_unloaded');
reset();slides[0].style.visibility='hidden';assert.equal(carouselDom({action:'media_state'}).kind,'empty_or_unloaded');
reset();assert.equal(Function(`return (${carouselDom.toString()})({action:'media_state'})`)().kind,'image');
console.log('PASS: exact carousel, decoded image/video evidence, empty/unloaded/hidden/placeholder/background diagnostics, no-media boundary, serialization');

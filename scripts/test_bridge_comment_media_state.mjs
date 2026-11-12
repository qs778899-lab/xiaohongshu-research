// Synthetic public-note DOM only: no browser, transport or image requests.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {commentMediaStateDom,inspectDom} from './xhs_bridge_driver.mjs';

const NOTE='a'.repeat(24),UID='1'.repeat(24),ROOT='2'.repeat(24),COMMENT='b'.repeat(24);
const IMAGE='https://fixture.xhscdn.com/202601011200/cccccccccccccccccccccccccccccccc/comment/syntheticasset0000000000000001!nc_n_webp_mw_1';
const OTHER=IMAGE.replace('syntheticasset','differentasset');
const request={uid:UID,expectedPath:'/explore/'+NOTE,commentId:COMMENT,imageUrl:IMAGE};
const code=commentMediaStateDom.toString().replace('GUARD({...opts,guardOnly:true})',`(${inspectDom.toString()})({...opts,guardOnly:true})`);

function fixture() {
  const counters={commentLookups:0,identityLookups:0,interactions:0,diagnosticScans:0};
  const viewerRoots=[];
  class Element {
    constructor(tagName='DIV',className='',attrs={}) {
      this.tagName=tagName;this.className=className;this.attrs=attrs;this.children=[];this.parentElement=null;
      this.style={display:'block',visibility:'visible',opacity:'1',cursor:'default'};
      this.rect={x:100,y:100,top:100,left:100,right:350,bottom:287,width:250,height:187};
      this.classList={contains:name=>this.className.split(/\s+/).includes(name)};
    }
    add(child) {this.children.push(child);child.parentElement=this;return child;}
    getAttribute(name) {return this.attrs[name] ?? null;}
    hasAttribute(name) {return name in this.attrs;}
    getClientRects() {return this.rect ? [this.rect] : [];}
    getBoundingClientRect() {return this.rect;}
    querySelector(selector) {
      if (selector==='.comment-item') return this.children.find(e=>e.classList.contains('comment-item')) || null;
      throw new Error('Unexpected selector: '+selector);
    }
    querySelectorAll(selector) {
      const descendants=[];const collect=node=>{for (const child of node.children) {descendants.push(child);collect(child);}};collect(this);
      if (selector==='img') return descendants.filter(e=>e.tagName==='IMG');
      if (selector==='img,canvas,video,iframe') return descendants.filter(e=>['IMG','CANVAS','VIDEO','IFRAME'].includes(e.tagName));
      if (selector==='*') return descendants;
      if (selector==='.reply-container .comment-item-sub, .sub-comment-list .comment-item')
        return descendants.filter(e=>e.classList.contains('comment-item-sub'));
      throw new Error('Unexpected selector: '+selector);
    }
    closest(selector) {
      const names=selector==='.content, .note-text' ? ['content','note-text']
        : selector==='.comment-item, .comment-item-sub' ? ['comment-item','comment-item-sub']
        : ['comment-pic','reply-pic','comment-image','reply-image','comment-img','reply-img'];
      for (let node=this;node;node=node.parentElement)
        if (names.some(name=>node.classList.contains(name)) ||
            !['.content, .note-text','.comment-item, .comment-item-sub'].includes(selector) &&
              ['comment-pic','reply-pic','comment-image','reply-image'].some(name=>node.className.includes(name))) return node;
      return null;
    }
    click() {counters.interactions++;throw new Error('No clicking allowed');}
    scrollIntoView() {counters.interactions++;throw new Error('No scrolling allowed');}
  }
  const image=(url=IMAGE,className='inner')=>{
    const img=new Element('IMG',className,{src:url});
    Object.assign(img,{src:url,currentSrc:url,complete:true,naturalWidth:1080,naturalHeight:1440,loading:'lazy'});return img;
  };
  const parent=new Element('DIV','parent-comment');
  const root=parent.add(new Element('DIV','comment-item',{'data-comment-id':ROOT}));
  const replies=parent.add(new Element('DIV','reply-container'));
  const reply=replies.add(new Element('DIV','comment-item-sub',{id:'comment-'+COMMENT}));
  const content=reply.add(new Element('DIV','content'));
  const emoji=content.add(image('https://picasso-static.xiaohongshu.com/fe-platform/emoji.png','note-content-emoji'));
  const wrapper=reply.add(new Element('DIV','reply-pic',{'data-preview':IMAGE,role:'button',tabindex:'0'}));
  const target=wrapper.add(image());
  const me=new Element('A','',{});me.innerText='我';me.href='https://www.xiaohongshu.com/user/profile/'+UID;
  const parents=[parent],state={restriction:false};
  const document={hidden:false,querySelectorAll(selector) {
    if (selector==='a[href*="/user/profile/"]') {counters.identityLookups++;return [me];}
    if (selector==='.login-container,.login-modal,.captcha-container,[role="dialog"]')
      return state.restriction?[Object.assign(new Element(),{innerText:'登录后查看'})]:[];
    if (selector==='.parent-comment') {counters.commentLookups++;return parents;}
    if (selector==='img') return [...parents,...viewerRoots].flatMap(root=>root.querySelectorAll('img'));
    if (selector==='*' || selector==='img,canvas,video,iframe') {
      counters.diagnosticScans++;
      const all=[...parents,...viewerRoots].flatMap(root=>[root,...root.querySelectorAll('*')]);
      return selector==='*'?all:all.filter(node=>['IMG','CANVAS','VIDEO','IFRAME'].includes(node.tagName));
    }
    if (selector==='body > *, body > * > *, [role="dialog"]') return [...parents,...viewerRoots].flatMap(root=>[root,...root.children]);
    throw new Error('Unexpected document selector: '+selector);
  }};
  Object.defineProperty(document,'body',{get(){throw new Error('The diagnostic must not extract page/body text');}});
  const location={href:'https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=observed'};
  const context={document,location,window:{innerWidth:1200,innerHeight:800},URL,
    getComputedStyle:e=>e.style,fetch:()=>{throw new Error('No network allowed');}};
  const run=(overrides={})=>vm.runInNewContext(`(${code})(${JSON.stringify({...request,...overrides})})`,context);
  return {run,context,counters,Element,image,parents,parent,root,reply,wrapper,target,emoji,content,me,state,viewerRoots};
}

{
  const f=fixture(),r=f.run();
  assert.equal(r.matchState,'matched');assert.equal(r.commentId,COMMENT);assert.equal(r.isReply,true);
  assert.equal(r.parentCommentId,ROOT);assert.equal(r.media.src,IMAGE);assert.equal(r.media.loaded,true);
  assert.equal(r.media.naturalWidth,1080);assert.equal(r.media.intersectsViewport,true);
  assert.equal(r.gap,null);assert.equal(r.notAVisualReview,true);assert.equal(r.pixelReadNotPerformed,true);
  assert.equal(r.guardVisible,true);
  assert.equal(r.readOnly,true);assert.equal(r.notANoMediaProof,true);assert.equal(r.previewReadiness,'not_tested_no_interaction');
  assert.equal(r.excludedInlineOrAvatar,1);
  assert.ok(r.ancestors.some(a=>a.isAttachmentWrapper && a.attributes['data-preview']===IMAGE));
  assert.equal(f.counters.interactions,0);
}
for (const [overrides,error] of [
  [{expectedPath:'/explore/'+'9'.repeat(24)},'TARGET_MISMATCH'],
  [{uid:'9'.repeat(24)},'ACCOUNT_MISMATCH'],
  [{commentId:'abc'},'COMMENT_MEDIA_TARGET_REQUIRED'],
  [{commentId:'not-a-comment-id'},'COMMENT_MEDIA_TARGET_REQUIRED'],
  [{imageUrl:'https://picasso-static.xiaohongshu.com/fe-platform/emoji.png'},'OBSERVED_COMMENT_IMAGE_REQUIRED'],
  [{imageUrl:'https://xhscdn.com.evil/comment/file'},'OBSERVED_COMMENT_IMAGE_REQUIRED'],
  [{imageUrl:'http://fixture.xhscdn.com/comment/file'},'OBSERVED_COMMENT_IMAGE_REQUIRED'],
]) {
  const f=fixture();assert.equal(f.run(overrides).error,error);assert.equal(f.counters.commentLookups,0);
}
{
  const f=fixture();f.context.location.href='https://example.com/private';
  assert.equal(f.run().error,'SCOPE_CHANGED');assert.equal(f.counters.identityLookups,0);assert.equal(f.counters.commentLookups,0);
  const restricted=fixture();restricted.state.restriction=true;
  assert.equal(restricted.run().error,'ACCESS_STOP');assert.equal(restricted.counters.commentLookups,0);
}
{
  const f=fixture();assert.equal(f.run({commentId:'9'.repeat(24)}).gap,'COMMENT_NOT_RENDERED');
  assert.equal(f.run({commentId:ROOT}).gap,'COMMENT_MEDIA_SOURCE_MISMATCH','Another comment cannot borrow this reply image');
  assert.equal(f.run({imageUrl:OTHER}).gap,'COMMENT_MEDIA_SOURCE_MISMATCH');
  f.target.currentSrc=OTHER;
  assert.equal(f.run().gap,'COMMENT_MEDIA_SOURCE_MISMATCH','A source/signature change is diagnostic, never silently substituted');
}
{
  const f=fixture();f.emoji.currentSrc=IMAGE;f.emoji.src=IMAGE;
  const avatar=f.wrapper.add(f.image(IMAGE,'avatar-item'));
  f.wrapper.add(f.image(IMAGE,'note-content-emoji'));
  const unrelated=f.reply.add(f.image(IMAGE,'unrelated'));
  assert.equal(f.run().matchingImages,1,'Inline emoji, avatars and images outside actual attachment wrappers are excluded');
  f.wrapper.children=f.wrapper.children.filter(e=>e!==f.target);
  assert.equal(f.run().gap,'COMMENT_MEDIA_SOURCE_MISMATCH','Matching inline emoji alone is not an ordinary comment attachment');
  assert.equal(avatar.className,'avatar-item');assert.equal(unrelated.className,'unrelated');
}
{
  const f=fixture();f.wrapper.add(f.image());
  assert.equal(f.run().gap,'COMMENT_MEDIA_AMBIGUOUS');
  const g=fixture();const duplicate=g.parent.add(new g.Element('DIV','comment-item-sub',{'data-comment-id':COMMENT}));
  assert.equal(g.run().gap,'COMMENT_ID_AMBIGUOUS');assert.ok(duplicate);assert.equal(g.counters.interactions,0);
}
{
  const f=fixture();f.target.complete=false;f.target.naturalWidth=0;f.target.naturalHeight=0;
  const r=f.run();assert.equal(r.gap,'COMMENT_MEDIA_UNLOADED');assert.equal(r.media.loaded,false);assert.equal(r.notANoMediaProof,true);
  f.target.complete=true;f.target.naturalWidth=1080;f.target.naturalHeight=1440;
  f.target.rect={x:100,y:900,top:900,left:100,right:350,bottom:1087,width:250,height:187};
  const offscreen=f.run();assert.equal(offscreen.media.loaded,true);assert.equal(offscreen.media.intersectsViewport,false);
  assert.equal(offscreen.notAVisualReview,true);
}
function observedControls(f) {
  f.target.className='inner';f.wrapper.className='comment-picture';
  f.wrapper.children=f.wrapper.children.filter(e=>e!==f.target);
  const imgBox=f.wrapper.add(new f.Element('DIV','img-box'));imgBox.add(f.target);
  f.target.style.cursor=imgBox.style.cursor='zoom-in';
  let clicks=0,scrolls=0;
  f.target.scrollIntoView=options=>{
    assert.equal(options.block,'center');assert.equal(options.behavior,'instant');scrolls++;f.counters.interactions++;
    f.target.rect={x:100,y:100,top:100,left:100,right:350,bottom:287,width:250,height:187};
  };
  const addPreview=(url=IMAGE.replace('!nc_n_webp_mw_1','!nd_dft_wlteh_webp_3'))=>{
    const portal=new f.Element('DIV','observed-preview-overlay');portal.style.position='fixed';portal.style.zIndex='1000';
    const preview=portal.add(f.image(url,'actual-large-image'));
    const close=portal.add(new f.Element('BUTTON','actual-close-control',{'aria-label':'关闭',title:'Close'}));
    close.innerText='×';f.viewerRoots.push(portal);return {portal,preview,close};
  };
  f.target.click=()=>{clicks++;f.counters.interactions++;addPreview();};
  return {imgBox,addPreview,get clicks(){return clicks;},get scrolls(){return scrolls;}};
}
{
  const f=fixture(),controls=observedControls(f);
  f.target.rect={x:100,y:900,top:900,left:100,right:350,bottom:1087,width:250,height:187};
  const blocked=f.run({action:'comment_media_open'});
  assert.equal(blocked.gap,'COMMENT_MEDIA_NOT_VISIBLE');assert.equal(controls.clicks,0);
  const focused=f.run({action:'comment_media_focus'});
  assert.equal(focused.focusSubmitted,true);assert.equal(focused.openSubmitted,false);assert.equal(focused.media.intersectsViewport,true);
  assert.equal(controls.scrolls,1);assert.equal(controls.clicks,0);
  const opened=f.run({action:'comment_media_open'});
  assert.equal(opened.openSubmitted,true);assert.equal(opened.clicksSubmitted,1);assert.equal(opened.noAutomaticRetry,true);
  assert.equal(opened.notAVisualReview,true);assert.equal(controls.clicks,1);assert.equal(controls.scrolls,1);
  const viewer=f.run({action:'comment_media_viewer_state'});
  assert.equal(viewer.visibleLoadedPreviewCandidates,1);assert.equal(viewer.gap,null);assert.equal(viewer.readOnly,true);
  assert.equal(viewer.previewCandidates[0].src,IMAGE.replace('!nc_n_webp_mw_1','!nd_dft_wlteh_webp_3'));
  assert.equal(viewer.previewCandidates[0].closeControlCandidates[0].ariaLabel,'关闭');
  assert.equal(viewer.previewCandidates[0].closeControlCandidates[0].observedWithinAncestor.className,'observed-preview-overlay');
  assert.equal(viewer.notAVisualReview,true);assert.equal(controls.clicks,1);
  const duplicate=f.run({action:'comment_media_open'});
  assert.equal(duplicate.gap,'COMMENT_VIEWER_ALREADY_OBSERVED');assert.equal(controls.clicks,1,'An existing preview never gets another opening click');
}
{
  const f=fixture();assert.equal(f.run({action:'comment_media_open'}).gap,'COMMENT_MEDIA_OPEN_CONTROL_UNCONFIRMED');
  assert.equal(f.counters.interactions,0);
  const linked=fixture(),controls=observedControls(linked);controls.imgBox.tagName='A';controls.imgBox.attrs.href=IMAGE;
  assert.equal(linked.run({action:'comment_media_open'}).gap,'COMMENT_MEDIA_LINK_NOT_ALLOWED');assert.equal(controls.clicks,0);
}
for (const replacement of [OTHER,IMAGE+'?different=1',IMAGE.replace('/cccccccccccccccccccccccccccccccc/','/'+'a'.repeat(32)+'/'),
  IMAGE.replace('fixture.xhscdn.com','different.xhscdn.com'),IMAGE.replace('https:','http:')]) {
  const f=fixture(),controls=observedControls(f);controls.addPreview(replacement);
  const r=f.run({action:'comment_media_viewer_state'});
  assert.equal(r.gap,'COMMENT_VIEWER_NOT_OBSERVED','Only a transform suffix may differ; asset/path/query/host stay exact');
  assert.equal(r.previewCandidates.length,0);assert.equal(controls.clicks,0);
}
{
  const f=fixture(),controls=observedControls(f);controls.addPreview();controls.addPreview();
  assert.equal(f.run({action:'comment_media_viewer_state'}).gap,'COMMENT_VIEWER_AMBIGUOUS');assert.equal(controls.clicks,0);
  const g=fixture(),other=observedControls(g),preview=other.addPreview().preview;preview.complete=false;
  assert.equal(g.run({action:'comment_media_viewer_state'}).gap,'COMMENT_VIEWER_NOT_OBSERVED');
  assert.equal(g.run({action:'comment_media_open'}).gap,'COMMENT_VIEWER_ALREADY_OBSERVED','An unready same-asset preview is not clicked again');
  assert.equal(other.clicks,0);
}
for (const action of ['comment_media_focus','comment_media_open']) {
  const f=fixture();observedControls(f);
  const method=action==='comment_media_focus'?'scrollIntoView':'click';
  f.target[method]=()=>{f.counters.interactions++;f.context.location.href='https://www.xiaohongshu.com/explore/'+'9'.repeat(24);};
  assert.equal(f.run({action}).error,'TARGET_MISMATCH','A navigation during an action stops before further inspection');
  assert.equal(f.counters.interactions,1);
}
{
  const f=fixture(),controls=observedControls(f),differentHost=IMAGE.replace('fixture.xhscdn.com','different.xhscdn.com');
  const {portal,preview,close}=controls.addPreview(differentHost);
  portal.rect={x:0,y:0,top:0,left:0,right:1200,bottom:800,width:1200,height:800};
  preview.rect={x:200,y:100,top:100,left:200,right:1000,bottom:700,width:800,height:600};
  close.rect={x:10,y:10,top:10,left:10,right:42,bottom:42,width:32,height:32};
  const r=f.run({action:'comment_media_viewer_state'}),d=r.unverifiedPreviewDiagnostics;
  assert.equal(r.gap,'COMMENT_VIEWER_NOT_OBSERVED');assert.equal(r.previewCandidates.length,0);
  assert.equal(r.visibleLoadedPreviewCandidates,0,'Large unassociated media never increases verified preview counts');
  assert.equal(d.notAnAssetAssociation,true);assert.equal(d.notAVisualReview,true);assert.equal(d.readOnly,true);
  assert.equal(d.mediaCandidates[0].src.value,differentHost);assert.equal(d.mediaCandidates[0].src.truncated,false);
  assert.equal(d.mediaCandidates[0].association,'unverified_not_linked_to_comment_asset');
  assert.equal(d.containerCandidates[0].visibleControls.candidates[0].ariaLabel,'关闭');
  assert.equal(d.containerCandidates[0].visibleControls.candidates[0].notAnActionTarget,true);
  assert.equal(f.counters.interactions,0);assert.equal(controls.clicks,0);assert.equal(controls.scrolls,0);
}
{
  const f=fixture(),portal=new f.Element('DIV','unknown-canvas-container');portal.style.position='fixed';
  portal.rect={x:0,y:0,top:0,left:0,right:1200,bottom:800,width:1200,height:800};
  portal.style.backgroundImage='url("https://actual-background.example/unverified-image.webp")';
  const canvas=portal.add(new f.Element('CANVAS','actual-canvas'));
  canvas.width=1600;canvas.height=1200;canvas.toDataURL=()=>{throw new Error('Pixels may not be read');};
  canvas.rect={x:200,y:100,top:100,left:200,right:1000,bottom:700,width:800,height:600};
  const close=portal.add(new f.Element('USE','',{'href':'#actual-close-symbol'}));
  f.viewerRoots.push(portal);
  const r=f.run({action:'comment_media_viewer_state'}),d=r.unverifiedPreviewDiagnostics;
  assert.equal(r.gap,'COMMENT_VIEWER_NOT_OBSERVED');assert.equal(r.previewCandidates.length,0);
  assert.equal(d.mediaCandidates.find(c=>c.tagName==='CANVAS').canvasSize.width,1600);
  assert.equal(d.mediaCandidates.find(c=>c.tagName==='DIV').backgroundUrls[0].value,'https://actual-background.example/unverified-image.webp');
  assert.ok(d.containerCandidates[0].visibleControls.candidates.some(c=>c.href.value==='#actual-close-symbol' && c.closeHintOnly));
  assert.equal(f.counters.interactions,0);assert.ok(close);
}
{
  const f=fixture();observedControls(f);f.context.document.hidden=true;
  const blocked=f.run({action:'comment_media_open'});
  assert.equal(blocked.guardVisible,false);assert.equal(blocked.visibilityDiagnostic.guardVisible,false);
  assert.equal(blocked.visibilityDiagnostic.mediaIntersectsViewport,true);assert.equal(f.counters.interactions,0);
  const diagnosed=f.run({action:'comment_media_viewer_state'});
  assert.equal(diagnosed.guardVisible,false);assert.equal(diagnosed.unverifiedPreviewDiagnostics.guardVisible,false);
  for (const overrides of [{uid:'9'.repeat(24)},{expectedPath:'/explore/'+'9'.repeat(24)},{commentId:'9'.repeat(24)},{imageUrl:OTHER}]) {
    const g=fixture();g.run({action:'comment_media_viewer_state',...overrides});
    assert.equal(g.counters.diagnosticScans,0,'An unmatched guard/comment/source prevents broad diagnostics');
  }
}
function addFeedImage(f,index) {
  const root=new f.Element('DIV','background-feed-'+index),image=root.add(f.image('https://feed.example/'+index+'.webp','background-image-'+index));
  image.rect={x:0,y:0,top:0,left:0,right:1100,bottom:700,width:1100,height:700};
  f.viewerRoots.push(root);return image;
}
{
  // More than eight earlier/larger background images must not consume the
  // output before a later overlay image is considered and ranked.
  const f=fixture(),controls=observedControls(f);
  for (let i=0;i<20;i++) addFeedImage(f,i);
  const {portal,preview}=controls.addPreview(IMAGE.replace('fixture.xhscdn.com','unverified-viewer.example'));
  portal.rect={x:0,y:0,top:0,left:0,right:1200,bottom:800,width:1200,height:800};
  preview.rect={x:400,y:200,top:200,left:400,right:700,bottom:400,width:300,height:200};
  const r=f.run({action:'comment_media_viewer_state'}),d=r.unverifiedPreviewDiagnostics;
  assert.equal(r.previewCandidates.length,0);assert.equal(r.gap,'COMMENT_VIEWER_NOT_OBSERVED');
  assert.equal(d.mediaCandidates.length,8);assert.equal(d.inspection.mediaOutputTruncated,true);
  assert.ok(d.inspection.eligibleMediaCandidates>=21,'All eligible sample media are considered before output truncation');
  assert.equal(d.mediaCandidates[0].className,'actual-large-image');
  assert.equal(d.mediaCandidates[0].rank.withinOverlay,true);assert.equal(d.mediaCandidates[0].rank.maxZIndex,1000);
  assert.equal(f.counters.interactions,0);
}
{
  // A prepended portal is outside both the DOM tail and last media segment.
  // Top-level/fixed probing and its actual media descendants must retain it.
  const f=fixture(),controls=observedControls(f);
  const {portal,preview}=controls.addPreview(IMAGE.replace('fixture.xhscdn.com','prepended-unverified.example'));
  portal.style.zIndex='9000';portal.rect={x:0,y:0,top:0,left:0,right:1200,bottom:800,width:1200,height:800};
  preview.rect={x:400,y:200,top:200,left:400,right:700,bottom:400,width:300,height:200};
  for (let i=0;i<1400;i++) addFeedImage(f,i);
  const r=f.run({action:'comment_media_viewer_state'}),d=r.unverifiedPreviewDiagnostics;
  assert.equal(d.inspection.sampleTruncated,true);assert.ok(d.inspection.sampledNodes<=1056);
  assert.ok(d.inspection.priorityOverlayMatches>=1);assert.ok(d.inspection.priorityMediaNodes>=1);
  assert.equal(d.mediaCandidates[0].className,'actual-large-image');assert.equal(d.mediaCandidates[0].rank.maxZIndex,9000);
  assert.equal(d.containerCandidates[0].className,'observed-preview-overlay');
  assert.equal(r.previewCandidates.length,0,'Ranking a large unverified portal never creates an asset association');
  assert.equal(f.counters.interactions,0);
}
{
  const f=fixture();observedControls(f);let attempts=0;
  f.target.click=()=>{attempts++;throw new Error('unknown result');};
  assert.throws(()=>f.run({action:'comment_media_open'}),/COMMAND_OUTCOME_UNKNOWN/);
  assert.equal(attempts,1,'An uncertain click has no automatic retry');
  for (const action of ['comment_media_focus','comment_media_open','comment_media_viewer_state']) {
    const g=fixture();observedControls(g);g.context.location.href='https://example.com/private';
    assert.equal(g.run({action}).error,'SCOPE_CHANGED');assert.equal(g.counters.interactions,0);assert.equal(g.counters.commentLookups,0);
    const restricted=fixture();observedControls(restricted);restricted.state.restriction=true;
    assert.equal(restricted.run({action}).error,'ACCESS_STOP');assert.equal(restricted.counters.interactions,0);
  }
}
console.log('PASS: bound comment/source actions and strict preview association; bounded unverified image/background/canvas/container/control diagnostics and guardVisible; no network/pixel/close/retry');

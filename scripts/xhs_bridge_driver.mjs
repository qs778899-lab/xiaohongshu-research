// Restricted bridge driver. No CLI/plugin discovery, arbitrary eval, cookies or writes.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {connectorStatus, startConnector} from './xhs_connector_health.mjs';
import {buildDeepExtractor} from './deep_comments.mjs';

export function publicUrl(value) {
  const u = new URL(value);
  if (u.origin==='https://www.xiaohongshu.com' &&
      (u.pathname.startsWith('/website-login/') || /^\/login\/?$/.test(u.pathname) || u.searchParams.has('error_code')))
    throw new Error('ACCESS_STOP');
  if (u.protocol !== 'https:' || u.hostname !== 'www.xiaohongshu.com' || u.port || u.username || u.password ||
      !/^\/(?:explore|search_result)(?:\/[a-f0-9]{24})?\/?$|^\/user\/profile\/[a-f0-9]{24}(?:\/[a-f0-9]{24})?\/?$/.test(u.pathname))
    throw new Error('SCOPE_CHANGED');
  if (u.searchParams.has('error_code')) throw new Error('ACCESS_STOP');
  return u.href;
}

export function requirePinnedProfile(status, contextId, extensionVersion) {
  if (!status) throw new Error('CONNECTOR_OFFLINE');
  if (status.daemonVersion !== '2.0.2') throw new Error('DAEMON_CHANGED_RECHECK_REQUIRED');
  const matches=(status.profiles || []).filter(p=>p.contextId===contextId);
  if(matches.length!==1) throw new Error('SPECIFIED_PROFILE_OFFLINE');
  if(matches[0].extensionVersion!==extensionVersion) throw new Error('EXTENSION_CHANGED_RECHECK_REQUIRED');
  return matches[0];
}

// OpenCLI 2.0.2 / extension 1.0.26 pageScopedResult returns a CDP targetId,
// not a numeric chrome.tabs id. Owned-lease resolution may fall back; retain
// and verify that returned identity before accepting any screenshot bytes.
export async function captureBoundScreenshot(sendCommandFull, cfg, guardCode) {
  const isTarget=value=>typeof value==='string' && /^[A-Fa-f0-9]{32}$/.test(value);
  if(!isTarget(cfg.tab))throw new Error('SCREENSHOT_REGISTERED_TARGET_REQUIRED');
  const bound={contextId:cfg.contextId,session:cfg.session,page:cfg.tab,
    surface:cfg.surface || 'adapter',siteSession:'persistent',windowMode:'background'};
  const command=async(action,fields)=>{
    const result=await sendCommandFull(action,{...bound,...fields});
    const actual=result?.page;
    if(!isTarget(actual) || actual!==cfg.tab) {
      const error=new Error(isTarget(actual)?'SCREENSHOT_TARGET_MISMATCH':'SCREENSHOT_TARGET_IDENTITY_UNCONFIRMED');
      error.detail=JSON.stringify({action,expectedPage:cfg.tab,
        actualPage:typeof actual==='string'?actual.slice(0,64):typeof actual==='number'?actual:null,
        actualPageType:typeof actual,targetIdentitySpace:'cdp_target_id',pngDiscarded:true});
      throw error;
    }
    return result;
  };
  const check=async phase=>{
    const result=await command('exec',{code:guardCode});
    if(!result.data || typeof result.data!=='object')throw new Error('SCREENSHOT_GUARD_UNCONFIRMED');
    if(result.data.error)throw new Error(result.data.error);
    if(result.data.uid!==cfg.uid)throw new Error('ACCOUNT_MISMATCH');
    if(result.data.visible!==true) {
      const error=new Error(result.data.visible===false?'SCREENSHOT_TARGET_NOT_VISIBLE':'SCREENSHOT_VISIBILITY_UNCONFIRMED');
      error.detail=JSON.stringify({phase,actualPage:result.page,targetIdentitySpace:'cdp_target_id',
        visible:result.data.visible===false?false:null,screenshotRequested:phase==='after',pngDiscarded:true});
      throw error;
    }
    return result.data;
  };
  await check('before');
  const screenshot=await command('screenshot',{format:'png',fullPage:false});
  const after=await check('after');
  if(typeof screenshot.data!=='string' || !screenshot.data)throw new Error('SCREENSHOT_EMPTY');
  return {url:after.url,uid:after.uid,page:screenshot.page,
    targetIdentitySpace:'cdp_target_id',targetVerified:true,pngBase64:screenshot.data};
}

// Preserve the executor's actual CDP target before reading its data. A returned
// fallback target is not a recoverable read/action result and is never replayed.
export async function executeBoundDom(sendCommandFull,cfg,code) {
  const isTarget=value=>typeof value==='string' && /^[A-Fa-f0-9]{32}$/.test(value);
  if(!isTarget(cfg.tab))throw new Error('EXEC_REGISTERED_TARGET_REQUIRED');
  const result=await sendCommandFull('exec',{code,contextId:cfg.contextId,session:cfg.session,page:cfg.tab,
    surface:cfg.surface || 'adapter',siteSession:'persistent',windowMode:'background'});
  const actual=result?.page;
  if(!isTarget(actual) || actual!==cfg.tab) {
    const error=new Error(isTarget(actual)?'EXEC_TARGET_MISMATCH':'EXEC_TARGET_IDENTITY_UNCONFIRMED');
    error.detail=JSON.stringify({action:cfg.action,expectedPage:cfg.tab,
      actualPage:typeof actual==='string'?actual.slice(0,64):typeof actual==='number'?actual:null,
      targetIdentitySpace:'cdp_target_id',dataDiscarded:true,noAutomaticReplay:true});
    throw error;
  }
  return {data:result.data,page:actual};
}

// Observe only the registered page's uniquely mapped native window. This never
// restores/focuses a window, changes a tab, or returns native titles/populated tabs.
export async function registeredWindowState(sendCommand,cfg,knownUrl,beforeIdentity,readIdentity) {
  if(!/^[A-Fa-f0-9]{32}$/.test(cfg.tab || ''))throw new Error('REGISTERED_TARGET_REQUIRED');
  if(typeof knownUrl!=='string' || knownUrl.includes('*'))throw new Error('EXACT_URL_REQUIRED');
  publicUrl(knownUrl);
  const verifyIdentity=value=>{
    if(!value || typeof value!=='object')throw new Error('IDENTITY_UNCONFIRMED');
    if(value.error)throw new Error(value.error);
    if(value.page!==cfg.tab)throw new Error('TARGET_MISMATCH');
    publicUrl(value.url);
    if(value.url!==knownUrl)throw new Error('UNEXPECTED_TARGET_CHANGE');
    if(value.uid!==cfg.uid)throw new Error('ACCOUNT_MISMATCH');
    return value.visible===true?true:value.visible===false?false:null;
  };
  const visibleBefore=verifyIdentity(beforeIdentity);
  const nativeRead=async(method,args)=>sendCommand('chrome-call',{
    contextId:cfg.contextId,chromeMethod:method,chromeArgs:args});
  const tabs=await nativeRead('tabs.query',[{url:knownUrl}]);
  if(!Array.isArray(tabs) || tabs.length!==1)throw new Error('REGISTERED_WINDOW_UNIQUE_MATCH_REQUIRED');
  const match={id:tabs[0]?.id,windowId:tabs[0]?.windowId,url:tabs[0]?.url};
  if(match.url!==knownUrl)throw new Error('REGISTERED_WINDOW_URL_CHANGED');
  if(!Number.isInteger(match.id) || match.id<0 || !Number.isInteger(match.windowId) || match.windowId<0)
    throw new Error('REGISTERED_WINDOW_IDENTITY_UNCONFIRMED');
  const observedWindow=await nativeRead('windows.get',[match.windowId,{populate:false}]);
  if(observedWindow?.id!==match.windowId || typeof observedWindow.focused!=='boolean' ||
      !['normal','minimized','maximized','fullscreen','locked-fullscreen'].includes(observedWindow.state))
    throw new Error('REGISTERED_WINDOW_IDENTITY_UNCONFIRMED');
  const observedTab=await nativeRead('tabs.get',[match.id]);
  const tab={id:observedTab?.id,windowId:observedTab?.windowId,url:observedTab?.url,active:observedTab?.active};
  if(tab.id!==match.id || tab.windowId!==match.windowId || tab.url!==knownUrl)
    throw new Error('REGISTERED_WINDOW_TARGET_CHANGED');
  if(typeof tab.active!=='boolean')throw new Error('REGISTERED_WINDOW_IDENTITY_UNCONFIRMED');
  const visibleAfter=verifyIdentity(await readIdentity());
  return {page:cfg.tab,url:knownUrl,uid:cfg.uid,readOnly:true,targetVerified:true,
    scope:'unique_registered_page_window_only',visibleBefore,visibleAfter,
    window:{id:observedWindow.id,state:observedWindow.state,focused:observedWindow.focused},tab,
    windowFocusRequested:false,windowRestoreRequested:false};
}

// Select among visible result roots before choosing a genuinely scrollable ancestor.
export function selectResults() {
  const roots=[...document.querySelectorAll('.feeds-container,.search-result-container')]
    // XHS absolutely positions cards inside a zero-height wrapper. It is a
    // valid result scope when it has rendered overflow, never itself a scroller.
    .filter(e=>(e.clientHeight>0 || e.scrollHeight>0) && e.getClientRects().length);
  for(const scope of roots) {
    for(let e=scope;e;e=e.parentElement) {
      if(e.clientHeight>0 && e.getClientRects().length && e.scrollHeight>e.clientHeight+10 && /^(auto|scroll|overlay)$/.test(getComputedStyle(e).overflowY))return {scope,scroller:e};
    }
  }
  const scroller=document.scrollingElement;
  if(roots.length && scroller?.clientHeight>0)return {scope:roots[0],scroller};
  return null;
}

// Read-only geometry for diagnosing a selected element versus viewport mismatch.
export function scrollDiagnoseDom() {
  const box=e=>{
    if(!e)return null;
    const r=e.getBoundingClientRect(),style=getComputedStyle(e);
    return {tag:e.tagName,cls:e.className,h:e.scrollHeight,ch:e.clientHeight,top:e.scrollTop,
      overflow:style.overflowY,overflowX:style.overflowX,position:style.position,
      display:style.display,visibility:style.visibility,transform:style.transform,
      rects:e.getClientRects().length,rect:{x:r.x,y:r.y,width:r.width,height:r.height},
      scrollRange:Math.max(0,e.scrollHeight-e.clientHeight),
      isDocumentScrollingElement:e===document.scrollingElement,
      isDocumentElement:e===document.documentElement,isBody:e===document.body};
  };
  const chosen=selectResults();
  const containers=[...document.querySelectorAll('.feeds-container,.search-result-container,.search-layout,.main-content')]
    .slice(0,12).map(e=>{const chain=[];for(let x=e;x && chain.length<24;x=x.parentElement)chain.push(box(x));return chain;});
  const listingScope=chosen?.scope?.closest?.('.search-layout__main,.search-layout') || chosen?.scope;
  const listingText=String(listingScope?.innerText || '').trim();
  const listingAncestors=[];
  for(let e=chosen?.scope?.parentElement;e && !['BODY','HTML'].includes(e.tagName) && listingAncestors.length<4;e=e.parentElement) {
    const value=String(e.innerText || '').trim();
    listingAncestors.push({tagName:e.tagName,className:String(e.className || ''),textPrefix:value.slice(0,1500),textLength:value.length,truncated:value.length>1500});
  }
  const searchInputs=[...document.querySelectorAll('input[placeholder*="搜索"],input.search-input,#search-input')]
    .filter(e=>e.getClientRects?.().length).slice(0,3).map(e=>({value:String(e.value || '').slice(0,200),placeholder:String(e.placeholder || '').slice(0,200)}));
  // Bounded CSSOM/geometry only. Never return sheet URLs, CSS text, attribute
  // selectors, arbitrary custom-property contents, card titles or link tokens.
  const limits={nodes:12,variablesPerNode:8,sheets:24,rules:2000,depth:6,matchedRules:32,declarationsPerRule:8,cards:6,cardLinks:96};
  const heightProps=new Set(['height','min-height','max-height','block-size','min-block-size','max-block-size']);
  const heightName=n=>/^--[a-zA-Z_][\w-]{0,77}$/.test(n) && /height|block-size|(?:^|[-_])(?:vh|dvh|svh|lvh)(?:[-_]|$)/i.test(n);
  const numeric=v=>typeof v==='string' && v.length<=160 && /^-?(?:\d+\.?\d*|\.\d+)(?:px|%|vh|dvh|svh|lvh|vw|dvw|svw|lvw|em|rem|vmin|vmax|cm|mm|in|pt|pc)?$/i.test(v);
  const safeHeight=v=>{
    if(typeof v!=='string' || v.length>160 || /["';{}@#\\]|url\s*\(/i.test(v))return null;
    const names=v.match(/--[\w-]+/g)||[];if(names.some(n=>!heightName(n)))return null;
    const rest=v.replace(/--[\w-]+/g,'0')
      .replace(/\b(?:auto|none|min-content|max-content|fit-content|stretch|initial|inherit|unset|revert-layer|revert|calc|min|max|clamp|var)\b/gi,'')
      .replace(/(?:\d+\.?\d*|\.\d+)(?:px|%|vh|dvh|svh|lvh|vw|dvw|svw|lvw|em|rem|vmin|vmax|cm|mm|in|pt|pc)?/gi,'')
      .replace(/[\s()+,*/%.-]/g,'');
    return rest ? null : v;
  };
  const declarations=style=>{
    const out=[];if(!style)return out;
    for(let i=0;i<Math.min(Number(style.length)||0,512) && out.length<limits.declarationsPerRule;i++){
      const property=String(style[i]||'');
      if(!heightProps.has(property) && !heightName(property))continue;
      const raw=String(style.getPropertyValue(property)||'').trim();
      if(heightName(property) && !numeric(raw))continue;
      const value=heightName(property)?raw:safeHeight(raw);
      out.push({property,...(value===null?{valueOmitted:true}:{value}),important:style.getPropertyPriority(property)==='important'});
    }
    return out;
  };
  const layoutNodes=[];
  const addNode=(element,role)=>{
    if(!element)return;
    const existing=layoutNodes.find(n=>n.element===element);
    if(existing){if(!existing.roles.includes(role))existing.roles.push(role);return;}
    if(layoutNodes.length<limits.nodes)layoutNodes.push({element,roles:[role]});
  };
  addNode(document.documentElement,'html');addNode(document.body,'body');
  addNode(chosen?.scope,'scope');addNode(chosen?.scroller,'scroller');
  for(const start of [chosen?.scope?.parentElement,chosen?.scroller?.parentElement]){
    for(let e=start,depth=0;e && depth<limits.nodes;e=e.parentElement,depth++)addNode(e,'ancestor');
  }
  const computedProps=['height','min-height','max-height','block-size','min-block-size','max-block-size',
    'overflow','overflow-x','overflow-y','display','position','zoom','transform','contain','box-sizing'];
  const layout=layoutNodes.map((n,index)=>{
    const style=getComputedStyle(n.element),computed={},heightVariables=[];
    for(const property of computedProps){
      const value=String(style.getPropertyValue(property)||'').trim();
      if(value.length<=160 && !/["';{}@#\\]|url\s*\(/i.test(value))computed[property]=value;
    }
    for(let i=0;i<Math.min(Number(style.length)||0,512) && heightVariables.length<limits.variablesPerNode;i++){
      const property=String(style[i]||''),value=String(style.getPropertyValue(property)||'').trim();
      if(heightName(property) && numeric(value))heightVariables.push({property,value});
    }
    return {node:`n${index}`,roles:n.roles,tag:n.element.tagName,computed,inlineHeight:declarations(n.element.style),heightVariables,
      heightVariableScanTruncated:(Number(style.length)||0)>512};
  });
  const css={sheetsVisited:0,rulesVisited:0,securityErrors:0,otherReadErrors:0,selectorErrors:0,
    truncated:false,declarationsAreCandidates:true,cascadeResolved:false,matches:[]};
  const safeSelector=value=>{
    const plain=value.replace(/\[data-v-[a-f0-9]{6,16}\]/gi,'').replace(/#(?:app-root|app|root)(?![\w-])/g,'');
    return value.length<=200 && /^[\w\s.:(),>+~*%-]+$/.test(plain)?value:null;
  };
  const mediaEntry=value=>{
    const condition=String(value||'').trim();
    if(!condition)return null;
    const known=condition.replace(/\b(?:min-|max-)?(?:device-)?(?:height|width|aspect-ratio|resolution|color|color-index|monochrome)\b/gi,'')
      .replace(/\b(?:screen|print|all|and|or|not|only|orientation|portrait|landscape|hover|any-hover|pointer|any-pointer|fine|coarse|none|prefers-color-scheme|dark|light|prefers-reduced-motion|reduce|no-preference|display-mode|browser|standalone|fullscreen)\b/gi,'')
      .replace(/(?:\d+\.?\d*|\.\d+)(?:px|em|rem|dppx|dpi|dpcm)?/gi,'').replace(/[\s:().,%\/-]/g,'');
    if(condition.length>160 || !/^[\w\s:().,%\/-]+$/.test(condition) || known)return {conditionOmitted:true};
    let active=null;try{active=window.matchMedia(condition).matches;}catch{}
    return {condition,active};
  };
  const readRules=owner=>{
    try{return owner.cssRules || [];}
    catch(error){if(error?.name==='SecurityError')css.securityErrors++;else css.otherReadErrors++;return [];}
  };
  const visit=(rules,sheet,media,depth,disabled)=>{
    if(depth>limits.depth){css.truncated=true;return;}
    for(let i=0;i<rules.length;i++){
      if(css.rulesVisited>=limits.rules || css.matches.length>=limits.matchedRules){css.truncated=true;break;}
      const rule=rules[i];css.rulesVisited++;
      if(typeof rule.selectorText==='string' && rule.selectorText.length<=1024){
        const heightDeclarations=declarations(rule.style);
        if(heightDeclarations.length){
          const nodes=[];
          for(let n=0;n<layoutNodes.length;n++){
            try{if(layoutNodes[n].element.matches(rule.selectorText))nodes.push(`n${n}`);}catch{css.selectorErrors++;break;}
          }
          if(nodes.length){const selector=safeSelector(rule.selectorText);
            css.matches.push({sheet,sheetDisabled:disabled,nodes,...(selector===null?{selectorOmitted:true}:{selector}),media:media.slice(-6),declarations:heightDeclarations});}
        }
      }
      // Reading existing grouping CSSOM never follows @import sheet URLs.
      if(rule.type!==3){
        const nested=readRules(rule);
        if(nested.length){const entry=rule.type===4?mediaEntry(rule.conditionText || rule.media?.mediaText):null;
          visit(nested,sheet,entry?[...media,entry]:media,depth+1,disabled);}
      }
    }
  };
  const sheets=document.styleSheets || [];
  if(sheets.length>limits.sheets)css.truncated=true;
  for(let i=0;i<Math.min(sheets.length,limits.sheets);i++){
    if(css.rulesVisited>=limits.rules || css.matches.length>=limits.matchedRules){css.truncated=true;break;}
    css.sheetsVisited++;const entry=mediaEntry(sheets[i].media?.mediaText);
    visit(readRules(sheets[i]),i,entry?[entry]:[],0,!!sheets[i].disabled);
  }
  const cardsGeometry=[],seenCards=new Set();let cardLinksVisited=0;
  for(const link of chosen?.scope?.querySelectorAll?.('a[href]') || []){
    if(cardsGeometry.length>=limits.cards || cardLinksVisited>=limits.cardLinks)break;
    cardLinksVisited++;
    let url;try{url=new URL(link.href,location.href);}catch{continue;}
    if(url.origin!=='https://www.xiaohongshu.com')continue;
    const noteId=url.pathname.match(/^\/(?:explore|search_result)\/([a-f0-9]{24})\/?$/)?.[1];
    if(!noteId || seenCards.has(noteId))continue;
    const card=link.closest?.('section.note-item,.note-item');
    if(!card || !chosen.scope.contains(card) || !card.getClientRects?.().length)continue;
    const r=card.getBoundingClientRect(),style=getComputedStyle(card);
    const position=String(style.position||''),transform=String(style.transform||'');
    cardsGeometry.push({noteId,rect:{x:r.x,y:r.y,width:r.width,height:r.height},
      ...(position.length<=32?{position}:{positionOmitted:true}),
      ...(transform.length<=512 && /^(?:none|(?:matrix|matrix3d)\([\d\s.,eE+\-]+\))$/.test(transform)?{transform}:{transformOmitted:true})});seenCards.add(noteId);
  }
  return {readOnly:true,compatMode:document.compatMode,
    renderedListing:{textPrefix:listingText.slice(0,2500),textSuffix:listingText.length>2500?listingText.slice(-1000):'',
      textLength:listingText.length,truncated:listingText.length>3500,searchInputs,listingAncestors,
      diagnosticOnly:true,notAnEndSignal:true},
    viewport:{innerHeight:window.innerHeight,innerWidth:window.innerWidth,scrollY:window.scrollY,
      scrollX:window.scrollX,pageYOffset:window.pageYOffset,
      visualViewport:window.visualViewport ? {height:window.visualViewport.height,width:window.visualViewport.width,
        offsetTop:window.visualViewport.offsetTop,pageTop:window.visualViewport.pageTop,scale:window.visualViewport.scale}:null},
    selected:chosen ? {scope:box(chosen.scope),scroller:box(chosen.scroller)}:null,
    documentScrollingElement:box(document.scrollingElement),documentElement:box(document.documentElement),
    body:box(document.body),containers,
    layoutDiagnosis:{diagnosticOnly:true,notAnEndSignal:true,limits,nodes:layout,css,cardsGeometry,cardLinksVisited}};
}
function domSource(fn) {
  return `(function(opts){const selectResults=${selectResults.toString()};return (${fn.toString()})(opts);})`;
}

// Serialized only into a pre-validated public XHS tab. Check again to close navigation races.
export function inspectDom(opts) {
  const u = new URL(location.href);
  if (u.origin==='https://www.xiaohongshu.com' &&
      (u.pathname.startsWith('/website-login/') || /^\/login\/?$/.test(u.pathname) || u.searchParams.has('error_code')))
    return {error:'ACCESS_STOP'};
  if (u.origin !== 'https://www.xiaohongshu.com' ||
      !/^\/(?:explore|search_result)(?:\/[a-f0-9]{24})?\/?$|^\/user\/profile\/[a-f0-9]{24}(?:\/[a-f0-9]{24})?\/?$/.test(u.pathname))
    return {error:'SCOPE_CHANGED'};
  if (u.searchParams.has('error_code')) return {error:'ACCESS_STOP'};
  const visible = e => !!e?.getClientRects?.().length;
  const text = e => e?.innerText?.trim() || '';
  const me = [...document.querySelectorAll('a[href*="/user/profile/"]')]
    .find(e => text(e) === '我');
  const uid = me?.href?.match(/\/user\/profile\/([a-f0-9]{24})/)?.[1];
  const restricted = [...document.querySelectorAll('.login-container,.login-modal,.captcha-container,[role="dialog"]')]
    .some(e => visible(e) && /登录|验证|访问限制|请求太频繁/.test(text(e)));
  if (restricted) return {error:'ACCESS_STOP'};
  if (!uid) return {error:'IDENTITY_UNCONFIRMED'};
  if (uid !== opts.uid) return {error:'ACCOUNT_MISMATCH'};
  const targetPath=p=>p.replace(/^\/user\/profile\/[a-f0-9]{24}\/([a-f0-9]{24})\/?$/, '/explore/$1').replace(/^\/search_result\/([a-f0-9]{24})\/?$/, '/explore/$1').replace(/\/$/,'');
  if (opts.expectedPath && targetPath(u.pathname) !== targetPath(opts.expectedPath)) return {error:'TARGET_MISMATCH'};
  if (opts.query && u.searchParams.get('keyword') !== opts.query) return {error:'QUERY_MISMATCH'};
  if(opts.backgroundText===true && ['body','roots','replies','search','author'].includes(opts.stage)) {
    // Page Visibility and CSS/layout readiness are different facts. Background
    // text may be read offscreen, but hidden/zero-sized content is not ready.
    const viewportReady=Number.isFinite(window.innerWidth) && window.innerWidth>0 &&
      Number.isFinite(window.innerHeight) && window.innerHeight>0;
    const laidOut=(element,allowOverflow=false)=>{
      if(!element || element.isConnected===false || !element.getClientRects?.().length)return false;
      const rect=element.getBoundingClientRect?.();
      if(!rect || !Number.isFinite(rect.width) || rect.width<=0 || !Number.isFinite(rect.height) ||
          !(rect.height>0 || allowOverflow && element.scrollHeight>0))return false;
      // Opacity is not inherited, so inspect the scope's actual ancestors too.
      for(let node=element;node;node=node.parentElement) {
        const style=getComputedStyle(node);
        if(style.display==='none' || ['hidden','collapse'].includes(style.visibility) ||
            Number(style.opacity || 1)===0)return false;
      }
      return true;
    };
    const listingScope=opts.stage==='search' || opts.stage==='author';
    const selection=listingScope ? selectResults() : null;
    const content=listingScope ? (selection?.scope?.closest?.('.search-layout__main,.search-layout') || selection?.scope) :
      document.querySelector('.note-detail-mask,.note-detail-container,.note-container');
    if(!viewportReady || !laidOut(content,listingScope) || listingScope &&
        (!laidOut(selection?.scope,true) || !laidOut(selection?.scroller) ||
         !(selection.scroller.clientWidth>0 && selection.scroller.clientHeight>0)))return {error:'CONTENT_LOADING'};
    if(opts.stage==='search') {
      // A positive overflow height alone does not prove the site's masonry
      // positioning has finished. Reuse the diagnostic's actual note wrappers.
      const cards=[],seen=new Set(),wrappers=new Set();let visited=0;
      for(const link of selection.scope.querySelectorAll('a[href]')) {
        if(cards.length>=6 || visited++>=96)break;
        let url;try{url=new URL(link.href,location.href);}catch{continue;}
        const id=url.origin==='https://www.xiaohongshu.com' &&
          url.pathname.match(/^\/(?:explore|search_result)\/([a-f0-9]{24})\/?$/)?.[1];
        if(!id || seen.has(id))continue;
        const card=link.closest?.('section.note-item,.note-item');
        if(!card || wrappers.has(card) || !selection.scope.contains(card) || !laidOut(card))continue;
        const rect=card.getBoundingClientRect(),style=getComputedStyle(card);
        if(![rect.x,rect.y,rect.width,rect.height].every(Number.isFinite) || rect.width<=0 || rect.height<=0)continue;
        cards.push({x:rect.x,y:rect.y,width:rect.width,height:rect.height,
          absolute:style.position==='absolute' && style.transform==='none'});seen.add(id);wrappers.add(card);
      }
      if(cards.length>=3 && cards.every(card=>card.absolute)) {
        const left=Math.max(...cards.map(card=>card.x)),top=Math.max(...cards.map(card=>card.y));
        const right=Math.min(...cards.map(card=>card.x+card.width)),bottom=Math.min(...cards.map(card=>card.y+card.height));
        const sameOrigin=cards.every(card=>Math.abs(card.x-cards[0].x)<=1 && Math.abs(card.y-cards[0].y)<=1);
        const stacked=right-left>=Math.min(...cards.map(card=>card.width))*.95 &&
          bottom-top>=Math.min(...cards.map(card=>card.height))*.95;
        if(sameOrigin || stacked)return {error:'BACKGROUND_LAYOUT_UNSETTLED'};
      }
    }
  }
  if (opts.guardOnly) return {uid, url:location.href, visible:!document.hidden};
  const listing=opts.stage==='search' || opts.stage==='author';
  const chosen=listing ? selectResults() : null;
  if(listing && !chosen)return {error:'CONTENT_LOADING'};
  const scope = listing ? (chosen.scope.closest?.('.search-layout__main,.search-layout') || chosen.scope) :
    document.querySelector('.note-detail-mask,.note-detail-container,.note-container');
  if (!scope) return {error:'CONTENT_LOADING'};
  if(listing && [...scope.querySelectorAll('.loading,.loading-container')].some(visible))return {error:'CONTENT_LOADING'};
  const links = [...scope.querySelectorAll('a[href]')].filter(visible).map(e => ({url:e.href,text:text(e)}))
    .filter(e => /^https:\/\/www\.xiaohongshu\.com\/(?:explore|search_result|user\/profile\/[a-f0-9]{24})\/[a-f0-9]{24}/.test(e.url));
  const cards = [...new Map(links.map(x => [x.url,x])).values()];
  const ends = [...scope.querySelectorAll('.end-container,.list-end,.no-more,.end')]
    .filter(visible).map(text);
  const selected = [...document.querySelectorAll('.filter-container .active,.filter-panel .active,[aria-checked="true"]')]
    .map(text);
  const sortControls=opts.stage==='search' ? [...document.querySelectorAll('[class*="filter"],[class*="sort"]')]
    .filter(visible).slice(0,40).map(e=>({className:e.className,text:text(e).slice(0,250)})) : [];
  const body = text(scope.querySelector('#detail-desc,.note-text,.desc'));
  const title = text(scope.querySelector('#detail-title,.title'));
  const media = [...scope.querySelectorAll('.swiper img,.swiper-slide img,.note-content img,.content img,video source,video')]
    .map((e,i) => ({position:i+1,tag:e.tagName,url:e.currentSrc || e.src || '',alt:e.alt || ''}));
  const authors = opts.stage==='body' ? [...scope.querySelectorAll('.author a[href*="/user/profile/"],.author-wrapper a[href*="/user/profile/"]')]
    .filter(e=>!e.closest('.comments-container,.comment-item,.comment-item-sub,.reply-container'))
    .map(e=>({url:e.href,name:text(e)})) : [];
  let position=null;
  if(opts.stage==='search' || opts.stage==='author') {
    const selectedRoot=selectResults();
    if(!selectedRoot)return {error:'CONTENT_LOADING'};
    const {scroller}=selectedRoot;
    const ids=cards.map(c=>c.url.match(/\/([a-f0-9]{24})(?:\?|$)/)?.[1]).filter(Boolean);
    position={scrollTop:scroller.scrollTop,scrollHeight:scroller.scrollHeight,viewportHeight:scroller.clientHeight,
      firstNoteId:ids[0] || null,lastNoteId:ids.at(-1) || null,uniqueNoteCount:new Set(ids).size,
      loading:false};
  }
  return {url:location.href,uid,stage:opts.stage,title,body,cards,media,selected,authors,
    position,sortControls,
    endSignals:ends,explicitEnd:ends.some(s=>/^(?:-\s*THE END\s*-|没有更多了?|已显示全部|到底了)$/.test(s)),
    visible:!document.hidden};
}

// Fixed read-only structure diagnostic. No comment bodies, navigation or state changes.
export function commentStructureDom() {
  const shown=e=>{
    if(!e?.getClientRects?.().length)return false;
    const style=typeof getComputedStyle==='function'?getComputedStyle(e):null;
    return !style || (style.display!=='none' && !['hidden','collapse'].includes(style.visibility) && style.opacity!=='0');
  };
  const box=e=>{
    if(!e)return null;
    const r=e.getBoundingClientRect?.();
    return {clientHeight:e.clientHeight || 0,scrollHeight:e.scrollHeight || 0,scrollTop:e.scrollTop || 0,
      width:r?.width || 0,height:r?.height || 0};
  };
  const scope=document.querySelector('.note-detail-mask,.note-detail-container,.note-container');
  const container=scope?.querySelector('.comments-container');
  const rootNodes=container?[...container.querySelectorAll('.parent-comment')]:[];
  const rootIds=[];
  for(const parent of rootNodes) {
    for(const element of [parent,parent.querySelector?.('.comment-item')]) {
      for(const attr of ['data-comment-id','data-commentid','data-id','id']) {
        const id=String(element?.getAttribute?.(attr)||'').match(/^(?:comment[-_])?([a-fA-F0-9]{16,40})$/)?.[1];
        if(id && !rootIds.includes(id) && rootIds.length<3)rootIds.push(id);
      }
    }
  }
  const count=e=>{
    const raw=String(e?.textContent||'').trim();
    const numeral='(?:[0-9]{1,7}|[0-9]{1,3}(?:,[0-9]{3}){1,2}|[0-9]{1,3}(?:\\.[0-9]{1,2})?[万千wWkK])\\+?';
    const format=new RegExp('^(?:评论|共\\s*'+numeral+'\\s*条?评论|'+numeral+'(?:\\s*条评论)?)$');
    const safe=raw.length<=40 && (raw.match(/[0-9]/g)||[]).length<=7 && format.test(raw);
    return {exists:!!e,visible:shown(e),text:safe?raw:null,textWithheld:!!raw&&!safe};
  };
  const selectors=['.total','.empty','.empty-text','.empty-state','.empty-container','.no-comment',
    '.no-comments','.comment-empty','.empty-comment','.comment-disabled','.comment-error','.open-app'];
  const messages=[];let unrecognizedUiText=false;
  if(container)for(const selector of selectors)for(const el of container.querySelectorAll(selector)) {
    if(!shown(el) || el.closest?.('.parent-comment,.comment-item,.comment-item-sub,.reply-container,.sub-comment-list'))continue;
    const raw=String(el.textContent||'').trim();
    if(!raw)continue;
    if(raw.length>80 || /[\d@]|微信|电话|手机|wx|wechat/i.test(raw)) {if(selector!=='.total')unrecognizedUiText=true;continue;}
    const empty=/^(?:暂无评论|暂时没有评论|还没有评论|没有评论)(?:[哦呀呢吧~～！!。,.，\s]*|[，,！!。]?快来(?:抢沙发|评论)(?:吧)?[~～！!。]*)$/.test(raw);
    const closed=/^(?:(?:作者|博主)已?关闭(?:了)?评论|评论(?:区)?已关闭|已关闭评论)[。！!\s]*$/.test(raw);
    const app=/^(?:请)?(?:打开|前往)(?:小红书)?\s*APP(?:内)?(?:查看|查看更多)(?:全部)?评论[。！!\s]*$/i.test(raw);
    if(empty||closed||app) {
      const type=empty?'empty':closed?'closed':'app_required';
      if(!messages.some(x=>x.type===type&&x.text===raw))messages.push({selector,type,text:raw});
    } else if(selector!=='.total')unrecognizedUiText=true;
  }
  // Read-only structure probe for the exact two-part empty UI. Never return
  // ordinary note/comment text or turn this diagnostic into an end decision.
  const noteScroller=scope?.querySelector('.note-scroller');
  const probeLimits={scannedNodes:240,matches:4,ancestors:4,classes:6,classCharacters:64};
  const classes=el=>String(el?.getAttribute?.('class') || el?.className || '').split(/\s+/)
    .filter(value=>value.length<=probeLimits.classCharacters && /^[a-zA-Z_][\w-]*$/.test(value)
      && !/[a-f0-9]{16}/i.test(value)).slice(0,probeLimits.classes);
  const nodeInfo=el=>({tag:['DIV','SPAN','P','BUTTON','SECTION'].includes(el?.tagName)?el.tagName:'OTHER',classes:classes(el)});
  const exactUi=el=>{
    const value=String(el?.textContent || '');
    return value.length<=80 && value.replace(/\s+/g,'')==='这是一片荒地点击评论';
  };
  const emptyUiProbe={diagnosticOnly:true,phraseKind:'wasteland_click_comment',
    scopeBasis:'fixed_comment_and_body_selectors',
    scrollerVisible:shown(noteScroller),limits:probeLimits,scannedNodes:0,truncated:false,nodes:[]};
  if(emptyUiProbe.scrollerVisible){
    const all=[...noteScroller.querySelectorAll('div,span,p,button,section')];
    const inspected=all.slice(0,probeLimits.scannedNodes);
    emptyUiProbe.scannedNodes=inspected.length;emptyUiProbe.truncated=all.length>inspected.length;
    const matched=inspected.filter(el=>shown(el) && noteScroller.contains(el) && exactUi(el));
    const leaves=matched.filter(el=>!matched.some(child=>child!==el && el.contains(child)));
    if(leaves.length>probeLimits.matches)emptyUiProbe.truncated=true;
    for(const el of leaves.slice(0,probeLimits.matches)){
      const ancestors=[];
      for(let parent=el.parentElement;parent && noteScroller.contains(parent) && ancestors.length<probeLimits.ancestors;parent=parent.parentElement){
        ancestors.push(nodeInfo(parent));if(parent===noteScroller)break;
      }
      emptyUiProbe.nodes.push({...nodeInfo(el),ancestors,
        insideComment:!!el.closest?.('.parent-comment,.comment-item,.comment-item-sub,.sub-comment-list,.reply-container'),
        insideBody:!!el.closest?.('#detail-desc,#detail-title,.note-text,.desc'),
        insideExistingCommentContainer:!!container?.contains(el)});
    }
  }
  return {diagnosticOnly:true,pageVisible:!document.hidden,commentContainer:{exists:!!container,visible:shown(container),geometry:box(container)},
    rootNodeCount:rootNodes.length,rootIds,
    loadingVisible:!!container&&[...container.querySelectorAll('.loading,.loading-container,[aria-busy="true"]')].some(shown),
    countHeading:count(container?.querySelector('.total')),
    interactionCount:count(scope?.querySelector('.interact-container .chat-wrapper .count')),
    messages:messages.slice(0,6),unrecognizedUiText,
    endSignalVisible:!!container&&[...container.querySelectorAll('.end-container,.end,.list-end,.no-more')]
      .some(e=>shown(e)&&/^(?:没有更多了?|已显示全部|到底了|\s*-?\s*THE END\s*-?)$/.test(String(e.textContent||'').trim())),
    noteScroller:box(noteScroller),emptyUiProbe,
    documentTimeOrigin:typeof performance?.timeOrigin==='number'?performance.timeOrigin:null,
    collectionPermitted:false,repairVerified:false};
}

// Exact observed-comment media only. Focus/open are distinct, bounded actions;
// state/viewer diagnostics never interact, fetch pixels or infer visual reading.
export function commentMediaStateDom(opts) {
  const action=opts.action || 'comment_media_state';
  if (!['comment_media_state','comment_media_focus','comment_media_open','comment_media_viewer_state',
    'comment_media_display_state','comment_media_enlarge','comment_media_restore'].includes(action))
    return {error:'COMMENT_MEDIA_ACTION_NOT_ALLOWED'};
  if (!/^\/explore\/[a-f0-9]{24}\/?$/.test(opts.expectedPath || '') ||
      !/^[a-f0-9]{24}$/.test(opts.commentId || '') || !/^[a-f0-9]{24}$/.test(opts.uid || ''))
    return {error:'COMMENT_MEDIA_TARGET_REQUIRED'};
  let source;
  try {source=new URL(opts.imageUrl);} catch {return {error:'OBSERVED_COMMENT_IMAGE_REQUIRED'};}
  if (source.protocol!=='https:' || !/(?:^|\.)xhscdn\.com$/.test(source.hostname) || source.port ||
      source.username || source.password || !source.pathname.includes('/comment/'))
    return {error:'OBSERVED_COMMENT_IMAGE_REQUIRED'};
  const guard=()=>GUARD({...opts,guardOnly:true});
  const checked=guard();
  if (checked.error) return checked;
  const base={readOnly:true,notAVisualReview:true,notANoMediaProof:true,pixelReadNotPerformed:true,
    uid:checked.uid,url:checked.url,guardVisible:checked.visible,commentId:opts.commentId,observedImageUrl:opts.imageUrl};
  const domId=e=>{
    for (const name of ['data-comment-id','data-commentid','data-id','id']) {
      const match=String(e?.getAttribute?.(name) || '').match(/^(?:comment[-_])?([a-f0-9]{24})$/);
      if (match) return match[1];
    }
    return '';
  };
  const rows=[];
  const add=(item,parent,isReply)=>{
    const id=domId(item) || (!isReply ? domId(parent) : '');
    if (item && id===opts.commentId && !rows.some(row=>row.item===item)) rows.push({item,parent,isReply});
  };
  for (const parent of document.querySelectorAll('.parent-comment')) {
    add(parent.querySelector('.comment-item'),parent,false);
    for (const reply of parent.querySelectorAll('.reply-container .comment-item-sub, .sub-comment-list .comment-item'))
      add(reply,parent,true);
  }
  if (rows.length!==1) return {...base,gap:rows.length?'COMMENT_ID_AMBIGUOUS':'COMMENT_NOT_RENDERED',
    matchingCommentNodes:rows.length};
  const {item,parent,isReply}=rows[0];
  const attachmentScope='.comment-pic, .reply-pic, .comment-image, .reply-image, .comment-img, .reply-img, [class*="comment-pic"], [class*="reply-pic"], [class*="comment-image"], [class*="reply-image"]';
  const finite=v=>Number.isFinite(v)?v:null;
  const geometry=e=>{
    const r=e.getBoundingClientRect?.(),style=getComputedStyle(e),width=finite(window.innerWidth),height=finite(window.innerHeight);
    const hasRect=!!e.getClientRects?.().length && !!r && r.width>0 && r.height>0;
    const cssVisible=style.display!=='none' && !['hidden','collapse'].includes(style.visibility) && style.opacity!=='0';
    return {rect:r?{x:finite(r.x),y:finite(r.y),top:finite(r.top),left:finite(r.left),right:finite(r.right),
      bottom:finite(r.bottom),width:finite(r.width),height:finite(r.height)}:null,
      hasRect,cssVisible,viewportWidth:width,viewportHeight:height,
      intersectsViewport:hasRect && cssVisible && width!==null && height!==null && r.right>0 && r.bottom>0 && r.left<width && r.top<height,
      display:style.display,visibility:style.visibility,opacity:style.opacity,cursor:style.cursor || '',
      position:style.position || '',zIndex:style.zIndex || '',pointerEvents:style.pointerEvents || ''};
  };
  const images=[];
  let excludedInlineOrAvatar=0,excludedOutsideAttachment=0;
  for (const image of item.querySelectorAll('img')) {
    if (image.classList?.contains('avatar-item') || image.classList?.contains('note-content-emoji') ||
        image.closest('.content, .note-text')) {excludedInlineOrAvatar++;continue;}
    if (image.closest('.comment-item, .comment-item-sub')!==item) {excludedOutsideAttachment++;continue;}
    const wrapper=image.closest(attachmentScope);
    if (!wrapper) {excludedOutsideAttachment++;continue;}
    const src=image.currentSrc || image.src || image.getAttribute('data-src') || '';
    images.push({image,wrapper,src});
  }
  const candidates=images.map(({image,wrapper,src})=>({src,className:String(image.className || ''),
    attachmentClassName:String(wrapper.className || ''),matchesObservedSource:src===opts.imageUrl}));
  const matches=images.filter(image=>image.src===opts.imageUrl);
  if (matches.length!==1) return {...base,isReply,parentCommentId:domId(parent.querySelector('.comment-item')) || domId(parent),
    gap:matches.length?'COMMENT_MEDIA_AMBIGUOUS':'COMMENT_MEDIA_SOURCE_MISMATCH',matchingImages:matches.length,
    candidates,excludedInlineOrAvatar,excludedOutsideAttachment};
  const {image,wrapper,src}=matches[0];
  const loaded=image.complete===true && image.naturalWidth>1 && image.naturalHeight>1;
  const ancestors=[];
  for (let node=image;node && ancestors.length<8;node=node.parentElement) {
    const attributes={};
    for (const name of ['role','tabindex','href','data-preview','data-index','data-src','data-origin','data-original','data-url','aria-label','title']) {
      const value=node.getAttribute?.(name);
      if (value!==null && value!==undefined && value!=='') attributes[name]=String(value).slice(0,1200);
    }
    ancestors.push({tagName:node.tagName || '',className:String(node.className || ''),attributes,
      hasOnclickAttribute:!!node.hasAttribute?.('onclick'),isAttachmentWrapper:node===wrapper,...geometry(node)});
    if (node===item) break;
  }
  const result={...base,isReply,parentCommentId:isReply?(domId(parent.querySelector('.comment-item')) || domId(parent)):'',
    matchState:'matched',gap:loaded?null:'COMMENT_MEDIA_UNLOADED',matchingImages:1,
    candidates,excludedInlineOrAvatar,excludedOutsideAttachment,
    media:{src,currentSrc:image.currentSrc || '',srcAttribute:image.getAttribute('src') || '',
      dataSrc:image.getAttribute('data-src') || '',className:String(image.className || ''),complete:image.complete===true,
      naturalWidth:finite(image.naturalWidth),naturalHeight:finite(image.naturalHeight),loaded,
      loading:image.loading || '',...geometry(image)},
    ancestors,previewReadiness:'not_tested_no_interaction'};
  if (['comment_media_display_state','comment_media_enlarge','comment_media_restore'].includes(action)) {
    if (opts.selector || opts.css || opts.styles) return {error:'COMMENT_DISPLAY_OPTIONS_NOT_ALLOWED'};
    const imgBox=image.parentElement;
    if (image.tagName!=='IMG' || imgBox?.tagName!=='DIV' || wrapper.tagName!=='DIV' ||
        !image.classList?.contains('inner') || !imgBox.classList?.contains('img-box') ||
        imgBox.parentElement!==wrapper || !wrapper.classList?.contains('comment-picture'))
      return {...result,gap:'COMMENT_DISPLAY_LAYOUT_UNCONFIRMED',displayMutationSubmitted:false};
    const properties=['width','height','max-width','max-height','object-fit','overflow'];
    const targets=[{role:'image',node:image},{role:'imgBox',node:imgBox},{role:'commentPicture',node:wrapper}];
    if (targets.some(({node})=>!['getPropertyValue','getPropertyPriority','setProperty','removeProperty'].every(key=>typeof node.style?.[key]==='function')))
      return {...result,gap:'COMMENT_DISPLAY_STYLE_UNCONFIRMED',displayMutationSubmitted:false};
    const binding={path:opts.expectedPath.replace(/\/$/,''),uid:checked.uid,commentId:opts.commentId,imageUrl:opts.imageUrl};
    const captureDisplay=()=>{const access=guard();if (access.error) return access;return {format:'comment-media-display-v1',binding,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,
      loaded:image.complete===true && image.naturalWidth>1 && image.naturalHeight>1,
      guardVisible:access.visible,allowedProperties:properties,
      nodes:targets.map(({role,node})=>({role,tagName:node.tagName,className:String(node.className || ''),
        inline:Object.fromEntries(properties.map(property=>[property,{value:node.style.getPropertyValue(property),priority:node.style.getPropertyPriority(property)}])),
        ...geometry(node)}))};};
    const current=captureDisplay();if (current.error) return current;
    const validState=state=>state?.format==='comment-media-display-v1' && state.binding &&
      Object.keys(binding).every(key=>state.binding[key]===binding[key]) && Array.isArray(state.nodes) && state.nodes.length===3 &&
      state.nodes.every((node,index)=>node.role===targets[index].role && node.tagName===current.nodes[index].tagName &&
        node.className===current.nodes[index].className && node.inline && Object.keys(node.inline).length===properties.length &&
        properties.every(property=>Object.hasOwn(node.inline,property) && typeof node.inline[property]?.value==='string' &&
          node.inline[property].value.length<=2048 && !node.inline[property].value.includes('\u0000') &&
          ['', 'important'].includes(node.inline[property].priority) && (node.inline[property].value || !node.inline[property].priority)));
    const sameInline=(a,b)=>a.nodes.every((node,index)=>properties.every(property=>
      node.inline[property].value===b.nodes[index].inline[property].value && node.inline[property].priority===b.nodes[index].inline[property].priority));
    const style=getComputedStyle(wrapper),number=value=>Number.parseFloat(value)||0;
    const paddingX=number(style.paddingLeft)+number(style.paddingRight),paddingY=number(style.paddingTop)+number(style.paddingBottom);
    const borderY=number(style.borderTopWidth)+number(style.borderBottomWidth);
    const displayViewport=()=>{
      const bounds={left:8,top:8,right:(window.innerWidth || 0)-8,bottom:(window.innerHeight || 0)-8},ancestors=[];
      for(let node=wrapper.parentElement,depth=0;node && depth++<16 && !['BODY','HTML'].includes(node.tagName);node=node.parentElement) {
        const css=getComputedStyle(node),x=css.overflowX || css.overflow || 'visible',y=css.overflowY || css.overflow || 'visible';
        const clipX=/^(auto|scroll|hidden|clip|overlay)$/.test(x),clipY=/^(auto|scroll|hidden|clip|overlay)$/.test(y);
        if (!clipX && !clipY) continue;
        const rect=node.getBoundingClientRect(),left=rect.left+(node.clientLeft || 0),top=rect.top+(node.clientTop || 0);
        const right=left+(Number.isFinite(node.clientWidth)?node.clientWidth:rect.width);
        const bottom=top+(Number.isFinite(node.clientHeight)?node.clientHeight:rect.height);
        if(clipX){bounds.left=Math.max(bounds.left,left);bounds.right=Math.min(bounds.right,right);}
        if(clipY){bounds.top=Math.max(bounds.top,top);bounds.bottom=Math.min(bounds.bottom,bottom);}
        ancestors.push({tagName:node.tagName,className:String(node.className || ''),overflowX:x,overflowY:y,left,top,right,bottom});
      }
      return {bounds,ancestors};
    };
    const visibleRegion=displayViewport();
    const wrapperBox=geometry(wrapper),contentWidth=(wrapper.clientWidth || wrapperBox.rect?.width || 0)-paddingX;
    const width=Math.min(contentWidth,image.naturalWidth,visibleRegion.bounds.right-Math.max(visibleRegion.bounds.left,wrapperBox.rect?.left || 0)-paddingX);
    const height=width*image.naturalHeight/image.naturalWidth;
    const fitGap=!current.loaded?'COMMENT_MEDIA_UNLOADED':image.naturalHeight>image.naturalWidth?'COMMENT_DISPLAY_LONG_IMAGE_UNSUPPORTED'
      :image.getAttribute('srcset')?'COMMENT_DISPLAY_RESPONSIVE_SOURCE_UNSUPPORTED'
      :targets.some(({node})=>{const value=getComputedStyle(node).transform;return value && value!=='none';})?'COMMENT_DISPLAY_TRANSFORM_UNSUPPORTED'
      :targets.some(({node})=>!node.style.getPropertyValue('overflow') &&
        (node.style.getPropertyValue('overflow-x') || node.style.getPropertyValue('overflow-y')))?'COMMENT_DISPLAY_OVERFLOW_LONGHAND_UNSUPPORTED'
      :!checked.visible || !geometry(image).intersectsViewport?'COMMENT_MEDIA_NOT_VISIBLE'
      :!Number.isFinite(height) || width<64 || height<32 || height+paddingY+borderY>(window.innerHeight || 0)-32 ||
        wrapperBox.rect.left<visibleRegion.bounds.left || wrapperBox.rect.top<visibleRegion.bounds.top ||
        wrapperBox.rect.top+height+paddingY+borderY>visibleRegion.bounds.bottom?'COMMENT_DISPLAY_VIEWPORT_TOO_SMALL':'';
    const plan={mode:'decoded_landscape_or_square_single_viewport_only',width,height,
      availableContentWidth:contentWidth,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,
      clippingBounds:visibleRegion.bounds,clippingAncestors:visibleRegion.ancestors,gap:fitGap || null,notAVisualReview:true};
    if (action==='comment_media_display_state') return {...result,displayState:current,displayPlan:plan,
      gap:fitGap || null,displayMutationSubmitted:false};
    if (!/^[A-Za-z0-9_-]{8,128}$/.test(opts.displayIntentId || ''))
      return {...result,displayState:current,gap:'COMMENT_DISPLAY_SAVED_INTENT_REQUIRED',displayMutationSubmitted:false};
    if (!validState(opts.expectedDisplayState) || !sameInline(opts.expectedDisplayState,current))
      return {...result,displayState:current,gap:'COMMENT_DISPLAY_STATE_CHANGED',displayMutationSubmitted:false};
    if (action==='comment_media_restore' && !validState(opts.originalDisplayState))
      return {...result,displayState:current,gap:'COMMENT_DISPLAY_ORIGINAL_STATE_REQUIRED',displayMutationSubmitted:false};
    if (action==='comment_media_enlarge' && (fitGap || opts.expectedDisplayState.naturalWidth!==image.naturalWidth ||
        opts.expectedDisplayState.naturalHeight!==image.naturalHeight))
      return {...result,displayState:current,displayPlan:plan,gap:fitGap || 'COMMENT_DISPLAY_NATURAL_SIZE_CHANGED',displayMutationSubmitted:false};
    const stillBound=()=>{
      const check=guard();if (check.error) throw Object.assign(new Error(check.error),{displayGuard:true});
      if (image.isConnected===false || image.parentElement!==imgBox || imgBox.parentElement!==wrapper ||
          image.closest('.comment-item, .comment-item-sub')!==item ||
          (image.currentSrc || image.src || image.getAttribute('data-src') || '')!==opts.imageUrl)
        throw Object.assign(new Error('COMMENT_MEDIA_SOURCE_CHANGED'),{displayGuard:true});
    };
    const px=value=>(Math.round(value*1000)/1000)+'px';
    const desired=action==='comment_media_restore'?opts.originalDisplayState:{...current,nodes:current.nodes.map((node,index)=>({
      ...node,inline:Object.fromEntries(properties.map(property=>[property,{value:
        property==='width'||property==='max-width'?px(index===2 && style.boxSizing==='border-box'?wrapperBox.rect.width:width):
        property==='height'||property==='max-height'?px(index===2 && style.boxSizing==='border-box'?height+paddingY+borderY:height):
        property==='object-fit'?'contain':'visible',priority:'important'}]))}))};
    const apply=state=>{
      for (let index=0;index<targets.length;index++) {
        stillBound();
        for (const property of properties) {
          stillBound();
          const value=state.nodes[index].inline[property];
          if (value.value) targets[index].node.style.setProperty(property,value.value,value.priority);
          else targets[index].node.style.removeProperty(property);
        }
      }
      stillBound();
    };
    try {apply(desired);} catch(error) {
      if (error.displayGuard) return {error:error.message,displayMutationSubmitted:true};
      try {apply(current);} catch {throw new Error('COMMAND_OUTCOME_UNKNOWN');}
      return {...result,gap:'COMMENT_DISPLAY_MUTATION_FAILED',displayMutationSubmitted:true,rolledBack:true,displayState:captureDisplay()};
    }
    let after=captureDisplay();if (after.error) return after;
    const rect=after.nodes[0].rect;
    const afterBounds=displayViewport().bounds;
    const fullImage=after.loaded && after.nodes[0].cssVisible && rect && rect.width>0 && rect.height>0 &&
      rect.left>=afterBounds.left && rect.top>=afterBounds.top && rect.right<=afterBounds.right && rect.bottom<=afterBounds.bottom &&
      Math.abs(rect.width/rect.height-image.naturalWidth/image.naturalHeight)<.02;
    const matchesPlan=rect && Math.abs(rect.width-width)<1 && Math.abs(rect.height-height)<1;
    if (!sameInline(desired,after) || action==='comment_media_enlarge' && (!fullImage || !matchesPlan)) {
      try {apply(current);after=captureDisplay();if (after.error) return after;} catch {throw new Error('COMMAND_OUTCOME_UNKNOWN');}
      return {...result,gap:'COMMENT_DISPLAY_FULL_IMAGE_UNCONFIRMED',displayMutationSubmitted:true,rolledBack:true,displayState:after};
    }
    return {...result,readOnly:false,action,gap:null,displayIntentId:opts.displayIntentId,displayMutationSubmitted:true,
      enlarged:action==='comment_media_enlarge',restored:action==='comment_media_restore',fullImageGeometryConfirmed:fullImage,
      originalDisplayState:action==='comment_media_enlarge'?current:opts.originalDisplayState,displayState:after,displayPlan:plan,
      pixelReadNotPerformed:true,notAVisualReview:true,noAutomaticRetry:true};
  }
  const assetBase=value=>{
    let u;try{u=new URL(value);}catch{return null;}
    if (u.protocol!=='https:' || u.hostname!==source.hostname || u.port || u.username || u.password ||
        u.search!==source.search || u.hash!==source.hash) return null;
    // Only the explicit CDN transform suffix can vary. Signed path, host,
    // actual comment asset ID, query and fragment remain exact observations.
    const match=u.pathname.match(/^(\/\d{12,14}\/[a-f0-9]{32}\/comment\/[a-zA-Z0-9]{16,100})(![a-zA-Z0-9_,.\-]{1,160})?$/);
    return match ? {base:u.origin+match[1]+u.search+u.hash,transform:match[2] || ''} : null;
  };
  const expectedAsset=assetBase(opts.imageUrl);
  if (action==='comment_media_viewer_state') {
    if (!expectedAsset) return {...result,gap:'COMMENT_ASSET_BASE_UNCONFIRMED'};
    const sameAsset=[];
    for (const candidate of document.querySelectorAll('img')) {
      const candidateSrc=candidate.currentSrc || candidate.src || candidate.getAttribute('data-src') || '';
      const identity=assetBase(candidateSrc);
      if (!identity || identity.base!==expectedAsset.base || candidate.closest('.comment-item, .comment-item-sub')) continue;
      const evidence={src:candidateSrc,assetBase:identity.base,transform:identity.transform,
        associationRule:'exact_comment_asset_base_query_fragment_only_transform_suffix_may_differ',
        className:String(candidate.className || ''),complete:candidate.complete===true,
        naturalWidth:finite(candidate.naturalWidth),naturalHeight:finite(candidate.naturalHeight),
        loaded:candidate.complete===true && candidate.naturalWidth>1 && candidate.naturalHeight>1,...geometry(candidate)};
      const chain=[],controls=[],seenControls=new Set();
      for (let node=candidate;node && chain.length<8 && !['BODY','HTML'].includes(node.tagName);node=node.parentElement) {
        chain.push({tagName:node.tagName || '',className:String(node.className || ''),
          role:node.getAttribute?.('role') || '',ariaLabel:node.getAttribute?.('aria-label') || '',...geometry(node)});
        // Inspect actual visible labels/classes within this asset's ancestors.
        // These are diagnostic candidates, never a selector or permission to close.
        for (const control of [...node.querySelectorAll('*')].slice(0,120)) {
          if (seenControls.has(control)) continue;
          seenControls.add(control);
          const label=[control.getAttribute?.('aria-label') || '',control.getAttribute?.('title') || ''].join(' ').trim();
          const cls=String(control.className || '');
          const text=String(control.innerText || '').trim();
          const semanticClose=/关闭|(?:^|\b)close(?:\b|$)/i.test(label) ||
            /(?:^|[\s_\-])(?:close|dismiss)(?:$|[\s_\-])/i.test(cls) ||
            (['BUTTON','A'].includes(control.tagName) || control.getAttribute?.('role')==='button') && /^[×✕✖关闭]{1,2}$/.test(text);
          if (!semanticClose) continue;
          const box=geometry(control);
          if (!box.intersectsViewport) continue;
          controls.push({tagName:control.tagName || '',className:cls,ariaLabel:control.getAttribute?.('aria-label') || '',
            title:control.getAttribute?.('title') || '',role:control.getAttribute?.('role') || '',
            text:text.slice(0,80),hasOnclickAttribute:!!control.hasAttribute?.('onclick'),
            observedWithinAncestor:{tagName:node.tagName || '',className:String(node.className || ''),depth:chain.length},...box});
        }
      }
      sameAsset.push({...evidence,ancestors:chain,closeControlCandidates:controls});
      if (sameAsset.length>=12) break;
    }
    const visible=sameAsset.filter(candidate=>candidate.intersectsViewport && candidate.loaded);
    let unverifiedPreviewDiagnostics;
    if (visible.length!==1 && !opts.skipUnverifiedDiagnostic) {
      const started=Date.now(),budgetMs=500,maxNodes=1056;
      const classOf=node=>String(node.getAttribute?.('class') ||
        (typeof node.className==='string'?node.className:node.className?.baseVal || ''));
      const observedUrl=value=>{
        const text=String(value || '');
        return {value:text.slice(0,4096),length:text.length,truncated:text.length>4096};
      };
      const describe=node=>({tagName:node.tagName || '',className:classOf(node),
        role:node.getAttribute?.('role') || '',ariaLabel:node.getAttribute?.('aria-label') || '',...geometry(node)});
      const backgroundUrls=node=>{
        const value=getComputedStyle(node).backgroundImage || '';
        return [...value.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)].slice(0,4).map(match=>observedUrl(match[1]));
      };
      const ancestry=node=>{
        const chain=[];
        for (let current=node;current && chain.length<6 && !['BODY','HTML'].includes(current.tagName);current=current.parentElement)
          chain.push(describe(current));
        return chain;
      };
      const controlsIn=node=>{
        const controls=[],descendants=[...node.querySelectorAll('*')];
        for (const control of descendants.slice(0,300)) {
          if (Date.now()-started>=budgetMs || controls.length>=16) break;
          const cls=classOf(control),aria=control.getAttribute?.('aria-label') || '',title=control.getAttribute?.('title') || '';
          const href=control.getAttribute?.('href') || control.getAttribute?.('xlink:href') || '';
          const controlTag=String(control.tagName || '').toUpperCase();
          const mightBeControl=['BUTTON','A','SVG','USE'].includes(controlTag) || control.getAttribute?.('role')==='button' ||
            /关闭|close|dismiss/i.test(aria+' '+title+' '+cls+' '+href);
          const text=mightBeControl?String(control.innerText || '').trim():'';
          const closeHint=/关闭|(?:^|\b)close(?:\b|$)/i.test(aria+' '+title+' '+href) ||
            /(?:^|[\s_\-])(?:close|dismiss)(?:$|[\s_\-])/i.test(cls) || /^[×✕✖]$/.test(text);
          if (!closeHint && !['BUTTON','SVG','USE'].includes(controlTag) && control.getAttribute?.('role')!=='button') continue;
          const box=geometry(control);if (!box.intersectsViewport) continue;
          controls.push({tagName:control.tagName || '',className:cls,ariaLabel:aria,title,role:control.getAttribute?.('role') || '',
            href:observedUrl(href),text:text.slice(0,80),closeHintOnly:closeHint,
            hasOnclickAttribute:!!control.hasAttribute?.('onclick'),observedWithinContainer:{tagName:node.tagName || '',className:classOf(node)},
            notAnActionTarget:true,...box});
        }
        return {candidates:controls,descendants:descendants.length,scanTruncated:descendants.length>300};
      };
      const mediaNodes=[...document.querySelectorAll('img,canvas,video,iframe')];
      const allNodes=[...document.querySelectorAll('*')];
      const topLevelNodes=[...document.querySelectorAll('body > *, body > * > *, [role="dialog"]')];
      const domOrder=new Map(allNodes.map((node,index)=>[node,index]));
      let inspected=0,timeBudgetReached=false;
      const viewportArea=(window.innerWidth || 0)*(window.innerHeight || 0);
      const visibleArea=box=>box.rect?Math.max(0,Math.min(box.rect.right,window.innerWidth)-Math.max(box.rect.left,0))*
        Math.max(0,Math.min(box.rect.bottom,window.innerHeight)-Math.max(box.rect.top,0)):0;
      const numericZ=node=>{const n=Number.parseInt(getComputedStyle(node).zIndex,10);return Number.isFinite(n)?n:0;};
      const ranking=(node,box)=>{
        let overlay=false,maxZIndex=0,depth=0;
        for (let ancestor=node;ancestor && depth++<8 && !['BODY','HTML'].includes(ancestor.tagName);ancestor=ancestor.parentElement) {
          const style=getComputedStyle(ancestor);
          overlay=overlay || style.position==='fixed' || ancestor.getAttribute?.('role')==='dialog';
          maxZIndex=Math.max(maxZIndex,numericZ(ancestor));
        }
        return {withinFixedOrDialog:overlay,withinOverlay:overlay || maxZIndex>=100,maxZIndex,
          visibleArea:visibleArea(box),domIndex:domOrder.get(node) ?? -1};
      };
      const compare=(a,b)=>Number(b.rank.withinOverlay)-Number(a.rank.withinOverlay) ||
        b.rank.maxZIndex-a.rank.maxZIndex || b.rank.visibleArea-a.rank.visibleArea || b.rank.domIndex-a.rank.domIndex;
      // Discover prepended and nested overlays too. This is a bounded style /
      // geometry probe, not a guessed viewer selector or an asset association.
      const priorityOverlays=[],priorityProbeLimit=5000,priorityProbeTimeMs=150;
      let priorityProbed=0,priorityProbeTimeBudgetReached=false;
      for (const node of allNodes.slice(0,priorityProbeLimit)) {
        if (Date.now()-started>=priorityProbeTimeMs) {priorityProbeTimeBudgetReached=true;break;}
        priorityProbed++;
        if (['BODY','HTML'].includes(node.tagName) || node.closest?.('.comment-item, .comment-item-sub')) continue;
        const style=getComputedStyle(node),role=node.getAttribute?.('role');
        if (style.position!=='fixed' && role!=='dialog' && numericZ(node)<100) continue;
        const box=geometry(node);
        if (!box.intersectsViewport || box.rect.width<160 || box.rect.height<120 || visibleArea(box)<viewportArea*.025) continue;
        priorityOverlays.push({node,rank:ranking(node,box)});
      }
      priorityOverlays.sort(compare);
      const priorityMediaNodes=priorityOverlays.slice(0,24).flatMap(({node})=>
        [...node.querySelectorAll('img,canvas,video,iframe')].slice(0,64)).slice(0,256);
      const samples=[...new Set([...priorityOverlays.map(value=>value.node),...priorityMediaNodes,...topLevelNodes.slice(-128).reverse(),
        ...allNodes.slice(-800).reverse(),...mediaNodes.slice(-256).reverse(),...allNodes.slice(0,128)])].slice(0,maxNodes);
      const mediaPool=[],containerPool=[];
      for (const node of samples) {
        if (Date.now()-started>=budgetMs) {timeBudgetReached=true;break;}
        inspected++;
        if (['BODY','HTML'].includes(node.tagName) || node.closest?.('.comment-item, .comment-item-sub')) continue;
        const box=geometry(node);
        if (!box.intersectsViewport || box.rect.width<160 || box.rect.height<120 || visibleArea(box)<viewportArea*.025) continue;
        const tag=String(node.tagName || '').toUpperCase();
        const backgrounds=backgroundUrls(node);
        const rank=ranking(node,box);
        if (['IMG','CANVAS','VIDEO','IFRAME'].includes(tag) || backgrounds.length) mediaPool.push({node,tag,box,backgrounds,rank});
        const largeOverlay=(box.position==='fixed' || node.getAttribute?.('role')==='dialog' || rank.maxZIndex>=100) &&
          visibleArea(box)>=viewportArea*.1;
        if (largeOverlay) containerPool.push({node,box,backgrounds,rank});
      }
      // Output limits are applied after collecting and ranking the whole bounded
      // sample, so early feed/carousel images cannot occupy all eight slots.
      mediaPool.sort(compare);containerPool.sort(compare);
      const containerCandidates=containerPool.slice(0,6).map(({node,box,backgrounds,rank})=>({tagName:node.tagName || '',className:classOf(node),
        role:node.getAttribute?.('role') || '',backgroundUrls:backgrounds,ancestors:ancestry(node),rank,
        visibleControls:controlsIn(node),association:'unverified_container_not_an_asset_binding',notAVisualReview:true,...box}));
      const mediaCandidates=mediaPool.slice(0,8).map(({node,tag,box,backgrounds,rank})=>({tagName:node.tagName || '',className:classOf(node),
        src:observedUrl(node.currentSrc || node.src || node.getAttribute?.('src') || ''),backgroundUrls:backgrounds,
        canvasSize:tag==='CANVAS'?{width:finite(node.width),height:finite(node.height)}:null,
        naturalWidth:finite(node.naturalWidth),naturalHeight:finite(node.naturalHeight),complete:node.complete===true,
        hasOpenShadowRoot:!!node.shadowRoot,association:'unverified_not_linked_to_comment_asset',notAVisualReview:true,
        ancestors:ancestry(node),rank,...box}));
      unverifiedPreviewDiagnostics={readOnly:true,notAVisualReview:true,notAnAssetAssociation:true,notANoMediaProof:true,
        guardVisible:checked.visible,scope:'current_guarded_note_dom_only_no_foreign_document_or_pixels_read',
        mediaCandidates,containerCandidates,inspection:{mediaNodes:mediaNodes.length,totalDomNodes:allNodes.length,
          sampledNodes:samples.length,inspectedNodes:inspected,maxNodes,timeBudgetMs:budgetMs,elapsedMs:Date.now()-started,
          samplingStrategy:'top_level_fixed_dialog_high_z_then_dom_tail_and_media_ranked_after_scan',
          topLevelNodes:topLevelNodes.length,priorityMediaNodes:priorityMediaNodes.length,
          priorityProbedNodes:priorityProbed,priorityProbeLimit,priorityProbeTimeMs,
          priorityProbeTimeBudgetReached,priorityProbeTruncated:priorityProbed<allNodes.length,priorityOverlayMatches:priorityOverlays.length,
          eligibleMediaCandidates:mediaPool.length,eligibleContainerCandidates:containerPool.length,
          mediaOutputTruncated:mediaPool.length>8,containerOutputTruncated:containerPool.length>6,
          timeBudgetReached:timeBudgetReached || Date.now()-started>=budgetMs,sampleTruncated:samples.length<allNodes.length},
        next:'Inspect actual DOM evidence and screenshots; no automatic asset association, click, close or visual review.'};
    }
    const after=guard();if (after.error) return after;
    return {...result,guardVisible:after.visible,matchState:'comment_thumbnail_reconfirmed',previewCandidates:sameAsset,
      visibleLoadedPreviewCandidates:visible.length,
      gap:visible.length===1?null:visible.length?'COMMENT_VIEWER_AMBIGUOUS':'COMMENT_VIEWER_NOT_OBSERVED',
      ...(unverifiedPreviewDiagnostics?{unverifiedPreviewDiagnostics}:{}),
      previewReadiness:'diagnostic_only_no_visual_read_no_close_action',assetBase:expectedAsset.base};
  }
  if (action==='comment_media_focus' || action==='comment_media_open') {
    if (!expectedAsset) return {...result,gap:'COMMENT_ASSET_BASE_UNCONFIRMED'};
    const existing=commentMediaStateDom({...opts,action:'comment_media_viewer_state',skipUnverifiedDiagnostic:true});
    if (existing.error) return existing;
    if (existing.previewCandidates?.length) return {...result,gap:'COMMENT_VIEWER_ALREADY_OBSERVED',
      previewCandidates:existing.previewCandidates,interactionSubmitted:false};
    const before=guard();if (before.error) return before;
    if (image.isConnected===false || (image.currentSrc || image.src || image.getAttribute('data-src') || '')!==opts.imageUrl ||
        image.closest('.comment-item, .comment-item-sub')!==item)
      return {...result,gap:'COMMENT_MEDIA_SOURCE_CHANGED',interactionSubmitted:false};
    if (action==='comment_media_focus') {
      try {image.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});}
      catch {throw new Error('COMMAND_OUTCOME_UNKNOWN');}
      const after=commentMediaStateDom({...opts,action:'comment_media_state'});
      if (after.error) return after;
      return {...after,readOnly:false,action,focusSubmitted:true,interactionSubmitted:true,
        openSubmitted:false,previewReadiness:'focus_only_requires_fresh_visible_check_before_open'};
    }
    const box=geometry(image);
    if (!loaded || !before.visible || !box.intersectsViewport)
      return {...result,guardVisible:before.visible,gap:loaded?'COMMENT_MEDIA_NOT_VISIBLE':'COMMENT_MEDIA_UNLOADED',interactionSubmitted:false,
        visibilityDiagnostic:{guardVisible:before.visible,mediaIntersectsViewport:box.intersectsViewport,cssVisible:box.cssVisible,loaded}};
    const imgBox=image.parentElement;
    if (!image.classList?.contains('inner') || !imgBox?.classList?.contains('img-box') ||
        imgBox.parentElement!==wrapper || !wrapper.classList?.contains('comment-picture') ||
        ![box.cursor,getComputedStyle(imgBox).cursor].includes('zoom-in'))
      return {...result,gap:'COMMENT_MEDIA_OPEN_CONTROL_UNCONFIRMED',interactionSubmitted:false};
    if (ancestors.some(node=>node.tagName==='A' || node.attributes.href || node.attributes.role==='link'))
      return {...result,gap:'COMMENT_MEDIA_LINK_NOT_ALLOWED',interactionSubmitted:false};
    // Exactly one ordinary click. No waits, retries, navigation or fallback.
    // The caller journals its open intention first; uncertain outcome is only
    // followed by viewer_state, never by automatic resubmission of this action.
    try {image.click();} catch {throw new Error('COMMAND_OUTCOME_UNKNOWN');}
    const after=guard();if (after.error) return after;
    return {...result,readOnly:false,action,openSubmitted:true,interactionSubmitted:true,clicksSubmitted:1,
      previewReadiness:'open_submitted_requires_read_only_viewer_diagnostic',noAutomaticRetry:true};
  }
  return result;
}

export function advanceDom(opts) {
  // Same guard executes immediately before any interaction, never an arbitrary selector.
  const checked = GUARD(opts);
  if (checked.error) return checked;
  const visible = e => !!e.getClientRects().length;
  if (opts.sort) {
    const labels = {general:'综合',latest:'最新',popular:'最多点赞'};
    const panels = [...document.querySelectorAll('.filter-container,.filter-panel')].filter(visible);
    const panel = panels[0];
    if (!panel) {
      const filters = [...document.querySelectorAll('span,div,button')].filter(e=>visible(e) && /^(?:筛选|已筛选)$/.test(e.innerText?.trim() || ''));
      const leaf = filters.find(e=>!filters.some(c=>c!==e && e.contains(c)));
      if (!leaf) return {error:'SORT_CONTROL_UNAVAILABLE'};
      leaf.click();return {action:'filter_opened'};
    }
    const choices = [...panel.querySelectorAll('span,div,button')].filter(e=>visible(e) && e.innerText?.trim()===labels[opts.sort]);
    const leaf = choices.find(e=>!choices.some(c=>c!==e && e.contains(c)));
    if (!leaf || !labels[opts.sort]) return {error:'SORT_CONTROL_UNAVAILABLE'};
    const selected=[...panel.querySelectorAll('.active,[aria-checked="true"]')]
      .some(e=>visible(e) && e.innerText?.trim()===labels[opts.sort]);
    if(selected)return {action:'sort_already_selected',sort:opts.sort};
    leaf.click();return {action:'sort_clicked',sort:opts.sort};
  }
  if (opts.stage==='search') {
    const panel=[...document.querySelectorAll('.filter-panel')].find(visible);
    if(panel){
      const controls=[...panel.querySelectorAll('span,div,button')].filter(e=>visible(e) && e.innerText?.trim()==='收起');
      const close=controls.find(e=>!controls.some(c=>c!==e && e.contains(c)));
      if(close){close.click();return {action:'filter_closed'};}
    }
  }
  if(opts.dismissFilters)return {action:'filter_already_closed'};
  const selectedRoot=selectResults();
  if(!selectedRoot)return {error:'CONTENT_LOADING'};
  const {scope,scroller}=selectedRoot;
  const resultsScope=scope.closest?.('.search-layout__main,.search-layout') || scope;
  if([...resultsScope.querySelectorAll('.loading,.loading-container')].some(visible))return {error:'CONTENT_LOADING'};
  const before=scroller.scrollTop;
  let anchorRestored=false;
  if(opts.anchor && /^[a-f0-9]{24}$/.test(opts.anchor)) {
    const link=[...scope.querySelectorAll('a[href]')].find(a=>a.href?.split('?')[0].endsWith('/'+opts.anchor));
    if(link && typeof link.scrollIntoView==='function'){link.scrollIntoView({block:'end',behavior:'instant'});anchorRestored=true;}
  }
  if(!anchorRestored && Number.isFinite(opts.resumeTop) && opts.resumeTop>=0)
    scroller.scrollTo({top:opts.resumeTop,behavior:'instant'});
  else if(!anchorRestored)scroller.scrollBy({top:Math.max(500,scroller.clientHeight*.8),behavior:'instant'});
  return {action:'scroll',before,after:scroller.scrollTop,anchorRestored};
}

export function carouselDom(opts) {
  const roots=[...document.querySelectorAll('.note-slider')].filter(e=>e.getClientRects().length);
  if(roots.length!==1)return {error:'MEDIA_CONTAINER_UNAVAILABLE'};
  const root=roots[0];
  const slides=[...root.querySelectorAll('.swiper-slide')];
  const indexOf=e=>{const s=e?.getAttribute('data-swiper-slide-index');return /^\d+$/.test(s||'')?Number(s):null;};
  const indices=[...new Set(slides.map(indexOf).filter(x=>x!==null))].sort((a,b)=>a-b);
  const active=slides.filter(e=>e.classList.contains('swiper-slide-active'));
  const current=active.length===1?indexOf(active[0]):null;
  if(!indices.length || indices.length>100 || indices.some((v,i)=>v!==i) || current===null)return {error:'MEDIA_INDEX_UNCONFIRMED'};
  // An indexed slide can be a blank shell. Diagnose actual decoded media;
  // never infer an image merely because the slide does not contain a video.
  const slide=active[0];
  const box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
  const layout=e=>{
    const rect=box(e),css=getComputedStyle(e);
    return {rect,display:css.display,visibility:css.visibility,opacity:css.opacity,
      visible:!!e.getClientRects().length && rect.width>0 && rect.height>0 && css.display!=='none' &&
        !['hidden','collapse'].includes(css.visibility) && Number(css.opacity || 1)>0};
  };
  const slideLayout=layout(slide),rootLayout=layout(root);
  const images=[...slide.querySelectorAll('img')].map((e,i)=>{
    const view=layout(e),src=e.currentSrc || e.src || '';
    const width=Number(e.naturalWidth)||0,height=Number(e.naturalHeight)||0;
    const placeholder=width<=2 && height<=2;
    return {ordinal:i+1,src,attributeSrc:e.getAttribute('src') || '',complete:e.complete===true,
      naturalWidth:width,naturalHeight:height,loading:e.loading || '',placeholderSuspected:placeholder,
      ...view,loaded:!!src && e.complete===true && width>0 && height>0 && !placeholder && view.visible};
  });
  const videos=[...slide.querySelectorAll('video')].map((e,i)=>{
    const view=layout(e),src=e.currentSrc || e.src || '',width=Number(e.videoWidth)||0,height=Number(e.videoHeight)||0;
    return {ordinal:i+1,src,poster:e.poster || '',readyState:Number(e.readyState)||0,networkState:Number(e.networkState)||0,
      videoWidth:width,videoHeight:height,...view,loaded:!!src && e.readyState>=2 && width>0 && height>0 && view.visible};
  });
  const backgrounds=[slide,...slide.querySelectorAll('*')].map((e,i)=>{
    const value=getComputedStyle(e).backgroundImage || 'none';
    const urls=[...value.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)/g)].map(m=>(m[1] || m[2] || m[3] || '').trim()).filter(Boolean);
    return urls.length ? {nodeOrdinal:i,tagName:e.tagName,backgroundImage:value,urls,...layout(e),loadState:'not_observable_from_css'} : null;
  }).filter(Boolean);
  const visible=rootLayout.visible && slideLayout.visible;
  const kind=visible && videos.length ? (videos.some(v=>v.loaded)?'video':'empty_or_unloaded') : visible && images.some(v=>v.loaded) ? 'image' : 'empty_or_unloaded';
  const state={index:current,count:indices.length,kind,screenshotReady:kind!=='empty_or_unloaded',
    mediaStatus:kind==='empty_or_unloaded'?'empty_or_unloaded':'rendered_decoded_media',
    mediaEvidence:{root:rootLayout,activeSlide:slideLayout,images,videos,backgrounds,
      potentialKinds:[...(images.length?['image']:[]),...(videos.length?['video']:[]),...(backgrounds.length?['background_image']:[])],
      inspectionScope:'active slide img/video/elements background-image; CSS decoding, pseudo-elements and canvas not verified',
      notANoMediaProof:true,notAVisualReview:true}};
  if(opts.action==='media_state')return state;
  if(opts.action!=='media_select' || !Number.isInteger(opts.index) || !indices.includes(opts.index))return {error:'MEDIA_INDEX_INVALID'};
  if(opts.expectedIndex!==current || opts.expectedCount!==indices.length)return {error:'MEDIA_POSITION_CHANGED'};
  if(opts.index===current && !state.screenshotReady)return {...state,error:'MEDIA_EMPTY_OR_UNLOADED'};
  if(!root.swiper || typeof root.swiper.slideToLoop!=='function')return {error:'MEDIA_CONTROL_UNAVAILABLE'};
  if(opts.index!==current)root.swiper.slideToLoop(opts.index,0);
  return {...state,requestedIndex:opts.index,selectionSubmitted:true};
}

async function main(cfg) {
  if (cfg.action==='active_user_tab') {
    const pkg=path.resolve(cfg.packageRoot);
    const load=p=>import(pathToFileURL(path.join(pkg,'dist/src',p)).href);
    const {requestDaemon}=await load('browser/daemon-transport.js');
    requirePinnedProfile(await connectorStatus(requestDaemon),cfg.contextId,cfg.extensionVersion);
    const {sendCommand}=await load('browser/daemon-client.js');
    const rows=await sendCommand('chrome-call',{contextId:cfg.contextId,chromeMethod:'tabs.query',
      chromeArgs:[{active:true,lastFocusedWindow:true,url:'https://www.xiaohongshu.com/*'}]});
    if(!Array.isArray(rows)||rows.length!==1)throw new Error('MANUAL_UNIQUE_ACTIVE_TAB_REQUIRED');
    const t=rows[0];publicUrl(t.url);
    if(!Number.isInteger(t.id)||!Number.isInteger(t.windowId)||t.active!==true)throw new Error('MANUAL_NATIVE_IDS_REQUIRED');
    return {tabId:t.id,windowId:t.windowId,url:t.url,active:true,readOnly:true,pageContentRead:false};
  }
  if (cfg.action==='adopt_user_tab') {
    if(cfg.surface!=='browser' || !Number.isInteger(cfg.chromeTabId) || !Number.isInteger(cfg.windowId))throw new Error('EXPLICIT_USER_TAB_REQUIRED');
    publicUrl(cfg.expectedUrl);
    const pkg=path.resolve(cfg.packageRoot);
    const load=p=>import(pathToFileURL(path.join(pkg,'dist/src',p)).href);
    const {requestDaemon}=await load('browser/daemon-transport.js');
    requirePinnedProfile(await connectorStatus(requestDaemon),cfg.contextId,cfg.extensionVersion);
    const {sendCommand,sendCommandFull}=await load('browser/daemon-client.js');
    const native=(method,args)=>sendCommand('chrome-call',{contextId:cfg.contextId,chromeMethod:method,chromeArgs:args});
    const before=await native('tabs.get',[cfg.chromeTabId]);
    if(before.id!==cfg.chromeTabId || before.windowId!==cfg.windowId || before.url!==cfg.expectedUrl || before.active!==true)
      throw new Error('USER_TAB_CHANGED');
    const focused=await native('tabs.query',[{active:true,lastFocusedWindow:true,url:cfg.expectedUrl}]);
    if(focused.length!==1 || focused[0].id!==cfg.chromeTabId)throw new Error('USER_TAB_NOT_FOCUSED');
    const result=await sendCommandFull('bind',{contextId:cfg.contextId,session:cfg.session,surface:'browser'});
    if(result.data?.url!==cfg.expectedUrl || !/^[A-Fa-f0-9]{32}$/.test(result.page || ''))throw new Error('USER_TAB_BIND_MISMATCH');
    const after=await native('tabs.get',[cfg.chromeTabId]);
    if(after.id!==before.id || after.windowId!==before.windowId || after.url!==before.url || after.active!==true)
      throw new Error('USER_TAB_CHANGED');
    return {page:result.page,url:after.url,chromeTabId:after.id,windowId:after.windowId,owned:false};
  }
  if (!['profiles','doctor','create','lease_status','probe_registered_target','locate_registered_url','registered_window_state','metadata','identity','capture','advance','navigate','reload','activate','close','deep','screenshot','media_state','media_select','scroll_diagnose','comment_diagnose','comment_media_state','comment_media_focus','comment_media_open','comment_media_viewer_state','comment_media_display_state','comment_media_enlarge','comment_media_restore'].includes(cfg.action)) throw new Error('ACTION_NOT_ALLOWED');
  const pkg=path.resolve(cfg.packageRoot);
  const meta=JSON.parse(fs.readFileSync(path.join(pkg,'package.json'),'utf8'));
  if (meta.name!=='@jackwener/opencli' || meta.version!=='2.0.2') throw new Error('CAPABILITY_MISMATCH');
  const load = p => import(pathToFileURL(path.join(pkg,'dist/src',p)).href);
  if (cfg.action==='doctor') {
    const {Page}=await load('browser/page.js');
    const capabilities=['setActivePage','tabs','newTab','evaluateOnce','cdp','closeTab','selectTab'];
    if (!capabilities.every(k=>typeof Page.prototype[k]==='function')) throw new Error('CAPABILITY_MISMATCH');
    const adapter=await import(pathToFileURL(path.join(pkg,'clis/xiaohongshu/comments.js')).href);
    buildDeepExtractor(adapter.buildCommentsExtractJs,{url:'https://www.xiaohongshu.com/explore/000000000000000000000001'});
    const result={version:meta.version,capabilities,deepAdapterCompatible:true,
      sessionStrategy:'one persistent adapter lease per project role',
      capabilityGaps:['shared_container_not_project_window','window_id_not_exposed_by_tabs','closed_tab_not_provable_from_missing_lease']};
    if(cfg.startConnector) {
      const {requestDaemon}=await load('browser/daemon-transport.js');
      const {spawnDaemonProcess}=await load('browser/daemon-lifecycle.js');
      const status=await startConnector(()=>connectorStatus(requestDaemon),spawnDaemonProcess,meta.version);
      result.connector={running:true,version:status.daemonVersion};
    }
    return result;
  }
  const {requestDaemon}=await load('browser/daemon-transport.js');
  const fetchDaemonStatus=()=>connectorStatus(requestDaemon);
  if (cfg.action==='profiles') {
    const status=await fetchDaemonStatus();
    return {daemonRunning:!!status,daemonVersion:status?.daemonVersion || null,
      profiles:(status?.profiles || []).map(p=>({contextId:p.contextId,extensionVersion:p.extensionVersion}))};
  }
  if (!/^[\w-]{1,128}$/.test(cfg.contextId || '') || !/^xhs-[\w-]{1,100}$/.test(cfg.session || '')) throw new Error('EXPLICIT_BINDING_REQUIRED');
  const {setDaemonCommandTimeoutSeconds}=await load('browser/daemon-client.js');
  setDaemonCommandTimeoutSeconds(30);
  requirePinnedProfile(await fetchDaemonStatus(),cfg.contextId,cfg.extensionVersion);
  if (cfg.action==='locate_registered_url') {
    publicUrl(cfg.url);
    if (cfg.url.includes('*')) throw new Error('EXACT_URL_REQUIRED');
    const {sendCommand}=await load('browser/daemon-client.js');
    const tabs=await sendCommand('chrome-call',{contextId:cfg.contextId,
      chromeMethod:'tabs.query',chromeArgs:[{url:cfg.url}]});
    return {matches:(Array.isArray(tabs)?tabs:[]).filter(t=>t.url===cfg.url)
      .map(t=>({tabId:t.id,windowId:t.windowId,url:t.url}))};
  }
  // Upstream can replace a stale daemon, including SIGKILL. Never do that to a
  // shared connector. Also require explicit doctor --start-connector if offline.
  const {daemonLifecycleHooks}=await load('browser/daemon-lifecycle.js');
  daemonLifecycleHooks.requestDaemonShutdown=async()=>{throw new Error('DAEMON_CHANGED_RECHECK_REQUIRED');};
  daemonLifecycleHooks.spawnDaemonProcess=()=>{throw new Error('CONNECTOR_OFFLINE');};
  const {BrowserBridge}=await load('browser/bridge.js');
  const bridge=new BrowserBridge();
  try {
    const page=await bridge.connect({contextId:cfg.contextId,session:cfg.session,windowMode:'background',timeout:10,
      surface:cfg.surface || 'adapter',siteSession:'persistent'});
    if (cfg.action==='create') {
      if(cfg.surface==='browser')throw new Error('USER_TAB_CREATE_NOT_ALLOWED');
      const tab=await page.newTab('https://www.xiaohongshu.com/explore');
      if (!tab) throw new Error('TAB_CREATE_UNCONFIRMED');
      return {page:tab,owned:true};
    }
    const tabs=await page.tabs();
    if (cfg.action==='probe_registered_target') {
      if (!/^[A-F0-9]{32}$/.test(cfg.tab || '')) throw new Error('REGISTERED_TARGET_REQUIRED');
      if(tabs.length) throw new Error('LEASE_NOT_EMPTY');
      // Audited extension 1.0.26 tabs/select resolves the exact target first;
      // an absent lease then returns an error, without creating any tab or
      // selecting a fallback. Never use exec/navigate/CDP for this probe:
      // their upstream resolver can create a replacement tab automatically.
      const {sendCommandFull}=await load('browser/daemon-client.js');
      try {
        await sendCommandFull('tabs',{op:'select',page:cfg.tab,contextId:cfg.contextId,
          session:cfg.session,surface:cfg.surface || 'adapter',siteSession:'persistent',windowMode:'background'});
        return {state:'exact_registered_target_selected'};
      } catch(error) {
        const message=String(error?.message || error);
        if(message.includes(`Page not found: ${cfg.tab}`)) return {state:'registered_target_absent',page:cfg.tab};
        if(message.includes('Page is not in the automation container')) return {state:'registered_target_exists_but_lease_missing',page:cfg.tab};
        throw error;
      }
    }
    if (cfg.action==='lease_status') {
      // This is the explicit role lease only, never a browser-wide inventory.
      // Unrelated URLs and all titles are withheld. No page is selected/read.
      return {expectedPage:cfg.tab, matching:tabs.filter(t=>t.page===cfg.tab).length,
        leaseTabCount:tabs.length, tabs:tabs.map(t=>{
          try {return {page:t.page,url:publicUrl(t.url)};}
          catch {return {page:t.page,urlWithheld:true};}
        })};
    }
    const match=tabs.filter(t=>t.page===cfg.tab);
    if (match.length!==1) throw new Error('TARGET_MISSING');
    const target=match[0];
    if (cfg.action==='metadata') return {page:cfg.tab,url:target.url,windowId:target.windowId ?? null};
    publicUrl(target.url);
    page.setActivePage(cfg.tab);
    const {sendCommandFull}=await load('browser/daemon-client.js');
    const {buildEvaluateExpression}=await load('browser/utils.js');
    let actualPage;
    const evaluateOnce=async code=>{
      const result=await executeBoundDom(sendCommandFull,cfg,buildEvaluateExpression(code,[]));
      actualPage=result.page;
      return result.data;
    };
    const opts={uid:cfg.uid,guardOnly:true};
    const check=await evaluateOnce(`(${domSource(inspectDom)})(${JSON.stringify(opts)})`);
    if (check.error) throw new Error(check.error);
    if (cfg.action==='identity') return {...check,page:actualPage,windowId:target.windowId ?? null};
    if (cfg.action==='registered_window_state') {
      const {sendCommand}=await load('browser/daemon-client.js');
      return await registeredWindowState(sendCommand,cfg,target.url,{...check,page:actualPage},async()=>{
        const after=await evaluateOnce(`(${domSource(inspectDom)})(${JSON.stringify(opts)})`);
        return {...after,page:actualPage};
      });
    }
    if (cfg.action==='activate') {await page.selectTab(cfg.tab);return {activated:cfg.tab,windowFocusRequested:false};}
    if (cfg.action==='close') {if(cfg.surface==='browser')throw new Error('USER_TAB_CLOSE_NOT_ALLOWED');await page.closeTab(cfg.tab);return {closed:cfg.tab};}
    if (cfg.action==='navigate') {
      publicUrl(cfg.url);
      const {sendCommandFull}=await load('browser/daemon-client.js');
      const result=await sendCommandFull('navigate',{contextId:cfg.contextId,session:cfg.session,page:cfg.tab,
        surface:cfg.surface || 'adapter',siteSession:'persistent',windowMode:'background',url:cfg.url});
      if(result.page!==cfg.tab)throw new Error('TARGET_MISMATCH');
      return {navigationSubmitted:true};
    }
    if (cfg.action==='reload') {
      return await evaluateOnce(`(()=>{const c=(${domSource(inspectDom)})(${JSON.stringify(opts)});if(c.error)return c;setTimeout(()=>location.reload(),50);return {reloadSubmitted:true};})()`);
    }
    const args={uid:cfg.uid,stage:cfg.stage,query:cfg.query,expectedPath:cfg.expectedPath,
      ...(cfg.backgroundText===true?{backgroundText:true}:{})};
    if (cfg.action==='comment_diagnose') {
      if(cfg.stage!=='body' || !/^\/(?:explore|search_result)\/[a-f0-9]{24}\/?$/.test(cfg.expectedPath||''))throw new Error('NOTE_TARGET_REQUIRED');
      return await evaluateOnce(`(()=>{const c=(${domSource(inspectDom)})(${JSON.stringify({...args,guardOnly:true})});if(c.error)return c;return {url:c.url,uid:c.uid,...(${commentStructureDom.toString()})()}})()`);
    }
    if (['comment_media_state','comment_media_focus','comment_media_open','comment_media_viewer_state','comment_media_display_state','comment_media_enlarge','comment_media_restore'].includes(cfg.action)) {
      const source=commentMediaStateDom.toString().replace('GUARD({...opts,guardOnly:true})',
        `(${domSource(inspectDom)})({...opts,guardOnly:true})`);
      try {return await evaluateOnce(`(${source})(${JSON.stringify({...args,action:cfg.action,commentId:cfg.commentId,imageUrl:cfg.imageUrl,
        displayIntentId:cfg.displayIntentId,expectedDisplayState:cfg.expectedDisplayState,originalDisplayState:cfg.originalDisplayState})})`);}
      catch(error) {
        if (['comment_media_focus','comment_media_open','comment_media_enlarge','comment_media_restore'].includes(cfg.action))
          throw Object.assign(new Error('COMMAND_OUTCOME_UNKNOWN'),{detail:String(error.detail || error.message).slice(0,300)});
        throw error;
      }
    }
    if (['media_state','media_select'].includes(cfg.action)) {
      if (!cfg.expectedPath || !/^\/explore\/[a-f0-9]{24}\/?$/.test(cfg.expectedPath)) throw new Error('NOTE_TARGET_REQUIRED');
      const mediaArgs={action:cfg.action,index:cfg.index,expectedIndex:cfg.expectedIndex,expectedCount:cfg.expectedCount};
      const source=`(()=>{const c=(${domSource(inspectDom)})(${JSON.stringify({...args,guardOnly:true})});if(c.error)return c;return (${carouselDom.toString()})(${JSON.stringify(mediaArgs)});})()`;
      return await evaluateOnce(source);
    }
    if (cfg.action==='screenshot') {
      const searchTarget=cfg.stage==='search' && typeof cfg.query==='string' && cfg.query.trim() && /^\/search_result\/?$/.test(cfg.expectedPath || '');
      if (!searchTarget && (!cfg.expectedPath || !/^\/(?:explore|search_result|user\/profile\/[a-f0-9]{24})\/[a-f0-9]{24}\/?$/.test(cfg.expectedPath))) throw new Error('NOTE_TARGET_REQUIRED');
      const {sendCommandFull}=await load('browser/daemon-client.js');
      return await captureBoundScreenshot(sendCommandFull,cfg,
        `(${domSource(inspectDom)})(${JSON.stringify({...args,guardOnly:true})})`);
    }
    if (cfg.action==='scroll_diagnose') {
      if (cfg.stage!=='search' || !cfg.query || !/^\/search_result\/?$/.test(cfg.expectedPath || '')) throw new Error('QUERY_TARGET_REQUIRED');
      return await evaluateOnce(`(()=>{const c=(${domSource(inspectDom)})(${JSON.stringify({...args,guardOnly:true})});if(c.error)return c;return {url:c.url,uid:c.uid,...(${domSource(scrollDiagnoseDom)})({})}})()`);
    }
    if (cfg.action==='capture') return await evaluateOnce(`(${domSource(inspectDom)})(${JSON.stringify(args)})`);
    if (cfg.action==='advance') {
      const source=domSource(advanceDom).replace('GUARD(opts)',`(${domSource(inspectDom)})({...opts,guardOnly:true})`);
      return await evaluateOnce(`(${source})(${JSON.stringify({...args,sort:cfg.sort,resumeTop:cfg.resumeTop,anchor:cfg.anchor,dismissFilters:cfg.dismissFilters})})`);
    }
    publicUrl(cfg.url);
    const noteId=value=>new URL(value).pathname.match(/^\/(?:explore|search_result|user\/profile\/[a-f0-9]{24})\/([a-f0-9]{24})\/?$/)?.[1];
    if (!noteId(cfg.url) || noteId(target.url)!==noteId(cfg.url)) throw new Error('TARGET_MISMATCH');
    if (!check.visible && cfg.backgroundText!==true) await page.selectTab(cfg.tab);
    const adapter=await import(pathToFileURL(path.join(pkg,'clis/xiaohongshu/comments.js')).href);
    const expression=buildDeepExtractor(adapter.buildCommentsExtractJs,{url:cfg.url,roots:30,maxReplyClicks:2,budgetMs:8000,progress:cfg.progress || {},accountUid:cfg.uid,yieldOnWait:true});
    // No goto()/upstream stale-target fallback, no network replay or stealth injection.
    let data;
    try {data=await evaluateOnce(expression);}
    catch(e) {
      if(/^EXEC_TARGET_|^EXEC_REGISTERED_TARGET_/.test(e.message))throw e;
      const error=new Error(/XHS_ACCESS_RESTRICTED/.test(e.message)?'ACCESS_STOP':/ACCOUNT_MISMATCH/.test(e.message)?'ACCOUNT_MISMATCH':'DEEP_READ_FAILED');error.detail=String(e.message).slice(0,300);throw error;
    }
    if (data?.scopeChanged) throw new Error('SCOPE_CHANGED');
    return data;
  } finally {await bridge.close();}
}

async function respond(cfg) {
  try { return {ok:true,data:await main(cfg),requestId:cfg.requestId}; }
  catch(e) {
    const codes={'profile-disconnected':'SPECIFIED_PROFILE_OFFLINE','extension-not-connected':'SPECIFIED_PROFILE_OFFLINE',
      'daemon-not-running':'CONNECTOR_OFFLINE','tab_gone':'TARGET_MISSING','command_result_unknown':'COMMAND_OUTCOME_UNKNOWN',
      'command_lost':'COMMAND_OUTCOME_UNKNOWN','result_evicted':'COMMAND_OUTCOME_UNKNOWN','session_busy':'ACCOUNT_BUSY'};
    const code=codes[e.code] || (/^[A-Z][A-Z0-9_]+$/.test(e.message)?e.message:'CONNECTOR_ERROR');
    return {ok:false,requestId:cfg.requestId,error:{code,detail:String(e.detail || e.message).slice(0,300)}};
  }
}
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url) {
  if(process.argv.includes('--stream')) {
    const {createInterface}=await import('node:readline');
    for await (const line of createInterface({input:process.stdin,crlfDelay:Infinity})) {
      let cfg;
      try { cfg=JSON.parse(line); } catch { console.log(JSON.stringify({ok:false,error:{code:'INVALID_REQUEST'}}));continue; }
      console.log(JSON.stringify(await respond(cfg)));
    }
  } else {
    const result=await respond(JSON.parse(fs.readFileSync(0,'utf8')));
    console.log(JSON.stringify(result)); if(!result.ok)process.exitCode=1;
  }
}

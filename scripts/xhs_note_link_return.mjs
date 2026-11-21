import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {requirePinnedProfile,publicUrl,executeBoundDom} from './xhs_bridge_driver.mjs';
import {connectorStatus} from './xhs_connector_health.mjs';

export function validateError(errorUrl,failedUrl,targetUrl) {
  publicUrl(failedUrl);publicUrl(targetUrl);
  const failed=new URL(failedUrl),target=new URL(targetUrl),e=new URL(errorUrl);
  const id=p=>p.match(/\/(?:explore|search_result)\/([a-f0-9]{24})\/?$/)?.[1];
  if(!id(failed.pathname)||!id(target.pathname)||id(failed.pathname)===id(target.pathname))throw new Error('DISTINCT_OBSERVED_NOTE_REQUIRED');
  const allowed=['source','noteId','errorCode','xsec_token','xsec_source'];
  if(e.origin!=='https://www.xiaohongshu.com'||e.pathname!=='/404'||e.username||e.password||e.hash||
     [...e.searchParams.keys()].some(k=>!allowed.includes(k)||e.searchParams.getAll(k).length!==1)||
     e.searchParams.get('source')!=='note'||!['-510001','-510000'].includes(e.searchParams.get('errorCode'))||
     e.searchParams.get('noteId')!==id(failed.pathname)||
     ['xsec_token','xsec_source'].some(k=>e.searchParams.get(k)!==failed.searchParams.get(k)))throw new Error('EXACT_NOTE_LINK_ERROR_REQUIRED');
}

export function inspectError(opts) {
  if(location.href!==opts.errorUrl)return {error:'TARGET_MISMATCH'};
  const visible=e=>!!e.getClientRects().length;
  const body=document.body?.innerText||'';
  if(/安全验证|滑动验证|验证码|访问受限|请求太频繁|操作频繁|账号异常/.test(body)||
    [...document.querySelectorAll('.login-container,.login-modal,.captcha-container,[role="dialog"]')].some(e=>visible(e)&&/登录|验证|限制/.test(e.innerText||'')))return {error:'ACCESS_STOP'};
  const me=[...document.querySelectorAll('a[href*="/user/profile/"]')].find(e=>(e.innerText||'').trim()==='我');
  const uid=me?.href?.match(/\/user\/profile\/([a-f0-9]{24})/)?.[1];
  if(uid&&uid!==opts.uid)return {error:'ACCOUNT_MISMATCH'};
  if(!/笔记不存在|当前笔记暂时无法浏览|页面不见了|无法浏览|无法查看|笔记已删除/.test(body))return {error:'NOTE_ERROR_TEXT_UNCONFIRMED',text:body.slice(0,1000)};
  return {url:location.href,uid:uid||null,text:body.slice(0,1000),noteErrorObserved:true};
}

export async function returnFromNoteError(cfg,native,send,evaluate) {
  validateError(cfg.errorUrl,cfg.failedUrl,cfg.targetUrl);
  if(!Number.isInteger(cfg.chromeTabId)||!Number.isInteger(cfg.windowId)||!/^[a-fA-F0-9]{32}$/.test(cfg.page))throw new Error('REGISTERED_TARGET_REQUIRED');
  const tab=await native('tabs.get',[cfg.chromeTabId]);
  if(tab.id!==cfg.chromeTabId||tab.windowId!==cfg.windowId||tab.url!==cfg.errorUrl)throw new Error('NATIVE_TARGET_CHANGED');
  const proof=await evaluate(inspectError,{errorUrl:cfg.errorUrl,uid:cfg.uid});
  if(proof.error)throw Object.assign(new Error(proof.error),{observation:proof});
  const again=await native('tabs.get',[cfg.chromeTabId]);
  if(again.id!==tab.id||again.windowId!==tab.windowId||again.url!==cfg.errorUrl)throw new Error('NATIVE_TARGET_CHANGED');
  const result=await send('navigate',{contextId:cfg.contextId,session:cfg.session,page:cfg.page,surface:'browser',siteSession:'persistent',windowMode:'background',url:cfg.targetUrl});
  if(result.page!==cfg.page)throw new Error('TARGET_MISMATCH');
  return {errorObservation:proof,navigationSubmitted:true,navigationCalls:1};
}

if(process.argv[1]&&pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url) {
  try {
    const cfg=JSON.parse(fs.readFileSync(0,'utf8'));
    const load=p=>import(pathToFileURL(path.join(cfg.packageRoot,'dist/src',p)).href);
    const {requestDaemon}=await load('browser/daemon-transport.js');
    requirePinnedProfile(await connectorStatus(requestDaemon),cfg.contextId,cfg.extensionVersion);
    const {sendCommand,sendCommandFull}=await load('browser/daemon-client.js');
    const {buildEvaluateExpression}=await load('browser/utils.js');
    const evaluate=async(fn,args)=>{
      const r=await executeBoundDom(sendCommandFull,{...cfg,tab:cfg.page,surface:'browser'},buildEvaluateExpression(`(${fn.toString()})(${JSON.stringify(args)})`,[]));
      return r.data;
    };
    const result=await returnFromNoteError(cfg,(method,args)=>sendCommand('chrome-call',{contextId:cfg.contextId,chromeMethod:method,chromeArgs:args}),sendCommandFull,evaluate);
    console.log(JSON.stringify({ok:true,data:result}));
  } catch(e) { console.log(JSON.stringify({ok:false,error:/^[A-Z][A-Z0-9_]+$/.test(e.message)?e.message:'NOTE_RETURN_OUTCOME_UNKNOWN',observation:e.observation}));process.exitCode=1; }
}

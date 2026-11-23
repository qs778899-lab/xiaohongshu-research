import assert from 'node:assert/strict';
import {validateError,returnFromNoteError,inspectError} from './xhs_note_link_return.mjs';
const failed='https://www.xiaohongshu.com/explore/'+'a'.repeat(24)+'?xsec_token=test&xsec_source=pc_feed';
const target='https://www.xiaohongshu.com/explore/'+'b'.repeat(24)+'?xsec_token=other';
const error='https://www.xiaohongshu.com/404?source=note&noteId='+'a'.repeat(24)+'&errorCode=-510001&xsec_token=test&xsec_source=pc_feed';
validateError(error,failed,target);
validateError(error.replace('-510001','-510000'),failed,target);
for(const url of [error.replace('-510001','300031'),error.replace('-510001','-510002'),error+'&error_code=300017',error.replace('noteId=','noteId=b'),error+'&extra=1',error+'&errorCode=-510001',error.replace('token=test','token=bad')])assert.throws(()=>validateError(url,failed,target));
const cfg={errorUrl:error,failedUrl:failed,targetUrl:target,chromeTabId:1,windowId:2,page:'A'.repeat(32),uid:'c'.repeat(24)};
for(const fault of [null,'native','account','access','unknown','text']) {
 const calls=[];
 const native=async()=>({id:fault==='native'?7:1,windowId:2,url:error});
 const evaluate=async()=>fault==='account'?{error:'ACCOUNT_MISMATCH'}:fault==='access'?{error:'ACCESS_STOP'}:fault==='text'?{error:'NOTE_ERROR_TEXT_UNCONFIRMED'}:{noteErrorObserved:true,url:error};
 const send=async(action,args)=>{calls.push(action);assert.equal(args.url,target);if(fault==='unknown')throw Error('UNKNOWN');return {page:cfg.page};};
 if(fault)await assert.rejects(()=>returnFromNoteError(cfg,native,send,evaluate));else await returnFromNoteError(cfg,native,send,evaluate);
 assert.equal(calls.length,['unknown',null].includes(fault)?1:0);
}
globalThis.location={href:error};
for(const body of ['笔记不存在','安全验证 笔记不存在','无关页面']) {
 globalThis.document={body:{innerText:body},querySelectorAll:()=>[]};
 const r=inspectError({errorUrl:error,uid:cfg.uid});
 assert.equal(r.noteErrorObserved===true,body==='笔记不存在');
}
console.log('18 note-error parsing, DOM restriction and bounded navigation scenarios passed');

const expired='https://www.xiaohongshu.com/website-login/error?'+new URLSearchParams({redirectPath:failed,error_code:'300017',error_msg:'当前访问链接已失效',uuid:'a'.repeat(36),verifyMsg:''});
validateError(expired,failed,target);
for(const bad of [expired.replace('300017','300031'),expired+'&extra=1',expired.replace('verifyMsg=','verifyMsg=captcha'),expired+'&error_code=300017'])assert.throws(()=>validateError(bad,failed,target));
globalThis.location={href:expired};
for(const body of ['安全限制 当前访问链接已失效 300017','当前访问链接已失效 验证码','无关页面']) {
 globalThis.document={body:{innerText:body},querySelectorAll:()=>[]};
 assert.equal(inspectError({errorUrl:expired,uid:cfg.uid}).noteErrorObserved===true,body==='安全限制 当前访问链接已失效 300017');
}
console.log('Exact expired-link and verification rejection scenarios passed');

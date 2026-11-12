import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {validateBoundTarget} from './deep_comments.mjs';

const tid='A'.repeat(32), endpoint='http://127.0.0.1:9222';
const ws='ws://127.0.0.1:9222/devtools/page/'+tid;
const note='https://www.xiaohongshu.com/explore/'+'a'.repeat(24);
const base={endpoint,targetId:tid,targetWebSocketUrl:ws};
assert.equal(validateBoundTarget(base),ws);
for(const changed of [{targetId:'B'.repeat(32)},{targetId:''},{targetWebSocketUrl:'ws://example.com:9222/devtools/page/'+tid},
  {targetWebSocketUrl:'ws://127.0.0.1:9333/devtools/page/'+tid},{targetWebSocketUrl:ws+'?x=1'},
  {targetWebSocketUrl:ws+'#x'},{targetWebSocketUrl:ws.replace('/page/','/browser/')},
  {targetWebSocketUrl:ws.replace('127.0.0.1','u@127.0.0.1')},{endpoint:'http://example.com:9222'}])
  assert.throws(()=>validateBoundTarget({...base,...changed}));

const temp=fs.mkdtempSync(path.join(os.tmpdir(),'xhs-target-tests-'));
const scripts=path.dirname(fileURLToPath(import.meta.url));
const pkg=path.join(temp,'package');
fs.mkdirSync(path.join(pkg,'dist/src/browser'),{recursive:true});
fs.mkdirSync(path.join(pkg,'clis/xiaohongshu'),{recursive:true});
fs.writeFileSync(path.join(pkg,'package.json'),JSON.stringify({name:'@jackwener/opencli',type:'module',version:'test'}));
const bridge=path.join(pkg,'dist/src/browser/cdp.js');
fs.writeFileSync(bridge,`
import fs from 'node:fs';
const log=value=>fs.appendFileSync(process.env.XHS_TEST_TARGET_LOG,JSON.stringify(value)+'\\n');
export class CDPBridge {
 async connect({cdpEndpoint}) {
   log({connect:cdpEndpoint});
   if(cdpEndpoint!==${JSON.stringify(ws)}) throw new Error('not bound ws');
   return {evaluate:async expression=>{log({evaluate:expression});
     if(expression==='location.href') return process.env.XHS_TEST_TARGET_URL;
     if(expression.includes('url: location.href')) return {url:process.env.XHS_TEST_TARGET_URL,visible:'visible'};
     if(expression==='document.hidden') return false;
     throw new Error('unexpected page read');},goto:async url=>log({goto:url}),wait:async()=>{}};
 }
 async send(method,params) {log({send:method}); if(method==='Runtime.evaluate') return {result:{value:{results:[],deepCoverage:{},commentTotal:0}}}; return {};}
 async close(){log({close:true});}
}
`);
fs.writeFileSync(path.join(pkg,'clis/xiaohongshu/comments.js'),`
export const buildCommentsExtractJs=()=>'(async () => { const driveScroll = () => {}; const clean = () => ""; const readCommentTotal = () => 0; const expandReplyThreads = async (root) => {}; })()';
export const normalizeCommentRows=x=>x;
export const assertCommentsLoaded=()=>{};
export const parseXhsProfileHref=()=>'';
export const buildXhsProfileUrl=()=>'';
`);
let serial=0;
function invoke(script,args,url){
 const log=path.join(temp,'log-'+serial++);
 const run=spawnSync(process.execPath,[path.join(scripts,script),...args],{encoding:'utf8',env:{...process.env,XHS_TEST_TARGET_LOG:log,XHS_TEST_TARGET_URL:url},timeout:10000});
 const rows=fs.existsSync(log)?fs.readFileSync(log,'utf8').trim().split('\n').filter(Boolean).map(x=>JSON.parse(x)):[];
 return {...run,rows};
}
try {
 const config={...base,url:note,opencliRoot:pkg,budgetMs:1000,roots:1,maxReplyClicks:0};
 let run=invoke('deep_comments.mjs',[JSON.stringify(config)],note);
 assert.equal(run.status,0,run.stderr+run.stdout);
 assert.deepEqual(run.rows[0],{connect:ws});
 assert.equal(run.rows.filter(x=>x.send==='Runtime.evaluate').length,1);
 assert.equal(run.rows.filter(x=>x.goto).length,0);
 for(const url of ['https://www.xiaohongshu.com/chat','https://www.xiaohongshu.com/user/profile/'+'b'.repeat(24),'https://example.com/','https://www.xiaohongshu.com/explore?error_code=300017','http://www.xiaohongshu.com/explore','https://u@www.xiaohongshu.com/explore','https://www.xiaohongshu.com:443/explore']){
   run=invoke('deep_comments.mjs',[JSON.stringify(config)],url);
   assert.equal(run.status,1);
   assert.equal(run.rows.filter(x=>x.send || x.goto).length,0,'no focus, body or navigation on wrong scope');
   assert.equal(run.rows.filter(x=>x.evaluate).length,1,'URL-only read');
 }
 run=invoke('deep_comments.mjs',[JSON.stringify({...config,targetWebSocketUrl:ws.replace('9222','9333')})],note);
 assert.equal(run.status,1);assert.deepEqual(run.rows,[],'invalid target never connects');
 const cookies=path.join(temp,'cookies.json');fs.writeFileSync(cookies,JSON.stringify({web_session:'synthetic'}));
 run=invoke('prepare_browser.mjs',[bridge,endpoint,cookies,ws,tid],'about:blank');
 assert.equal(run.status,0,run.stderr);
 assert.equal(run.rows.filter(x=>x.send==='Network.setCookies').length,1);
 assert.deepEqual(run.rows[0],{connect:ws});
 run=invoke('prepare_browser.mjs',[bridge,endpoint,cookies,ws,tid],'https://www.xiaohongshu.com/chat');
 assert.equal(run.status,1);assert.equal(run.rows.filter(x=>x.send || x.goto).length,0,'no cookies or navigation on private page');
 run=invoke('prepare_browser.mjs',[bridge,endpoint,cookies,ws,tid],'https://www.xiaohongshu.com/explore?error_code=300031');
 assert.equal(run.status,1);assert.equal(run.rows.filter(x=>x.send || x.goto).length,0,'no cookies or navigation after live restriction');
 run=invoke('prepare_browser.mjs',[bridge,endpoint,cookies,ws,tid],'https://www.xiaohongshu.com:443/explore');
 assert.equal(run.status,1);assert.equal(run.rows.filter(x=>x.send || x.goto).length,0,'explicit authority port rejected before cookies');
 process.stdout.write('Fixed-target JavaScript checks passed: strict ws, fixed connection, URL-first scope, private-page no body/focus, scoped bootstrap.\n');
} finally {fs.rmSync(temp,{recursive:true,force:true});}

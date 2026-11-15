import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {requirePinnedProfile,publicUrl} from './xhs_bridge_driver.mjs';
import {connectorStatus} from './xhs_connector_health.mjs';

export async function createProjectHome(cfg,native) {
  publicUrl(cfg.expectedUrl);
  if(!Number.isInteger(cfg.chromeTabId)||!Number.isInteger(cfg.windowId))throw new Error('REGISTERED_NATIVE_TAB_REQUIRED');
  const old=await native('tabs.get',[cfg.chromeTabId]);
  if(old.id!==cfg.chromeTabId||old.windowId!==cfg.windowId||old.url!==cfg.expectedUrl)throw new Error('PROJECT_TAB_PARENT_CHANGED');
  const win=await native('windows.get',[cfg.windowId]);
  if(win.id!==cfg.windowId||win.type!=='normal')throw new Error('PROJECT_TAB_WINDOW_UNCONFIRMED');
  // Single creation, fixed public home, same verified profile and window.
  const created=await native('tabs.create',[{windowId:cfg.windowId,url:'https://www.xiaohongshu.com/explore',active:true}]);
  if(!Number.isInteger(created.id)||created.id===cfg.chromeTabId)throw new Error('PROJECT_TAB_CREATE_OUTCOME_UNKNOWN');
  for(let i=0;i<10;i++) {
    const t=await native('tabs.get',[created.id]);
    if(t.id!==created.id||t.windowId!==cfg.windowId||t.active!==true)throw new Error('PROJECT_TAB_CREATE_MISMATCH');
    if(t.url==='https://www.xiaohongshu.com/explore')return {tabId:t.id,windowId:t.windowId,url:t.url,active:true};
    if(t.url && t.url!=='about:blank') {publicUrl(t.url);throw new Error('PROJECT_TAB_UNEXPECTED_URL');}
    await new Promise(r=>setTimeout(r,300));
  }
  throw new Error('PROJECT_TAB_HOME_NOT_READY');
}

if(process.argv[1]&&pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url) {
  try {
    const cfg=JSON.parse(fs.readFileSync(0,'utf8'));
    const load=p=>import(pathToFileURL(path.join(cfg.packageRoot,'dist/src',p)).href);
    const {requestDaemon}=await load('browser/daemon-transport.js');
    requirePinnedProfile(await connectorStatus(requestDaemon),cfg.contextId,cfg.extensionVersion);
    const {sendCommand}=await load('browser/daemon-client.js');
    const native=(method,args)=>sendCommand('chrome-call',{contextId:cfg.contextId,chromeMethod:method,chromeArgs:args});
    console.log(JSON.stringify({ok:true,data:await createProjectHome(cfg,native)}));
  } catch(e) {
    console.log(JSON.stringify({ok:false,error:/^[A-Z][A-Z0-9_]+$/.test(e.message)?e.message:'PROJECT_TAB_CREATE_OUTCOME_UNKNOWN'}));
    process.exitCode=1;
  }
}

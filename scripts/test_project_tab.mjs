import test from 'node:test';import assert from 'node:assert/strict';
import {createProjectHome,probeMissingPrebind,dispatchProjectHome} from './xhs_project_tab.mjs';
const home='https://www.xiaohongshu.com/explore';const cfg={chromeTabId:11,windowId:22,expectedUrl:home};
test('creates exactly one home in registered window',async()=>{const calls=[];const result=await createProjectHome(cfg,async(m,a)=>{calls.push([m,a]);if(m==='windows.get')return {id:22,type:'normal'};if(m==='tabs.create')return {id:12};return {id:a[0],windowId:22,url:home,active:true};});assert.equal(result.tabId,12);assert.equal(calls.filter(x=>x[0]==='tabs.create').length,1);assert.deepEqual(calls.find(x=>x[0]==='tabs.create')[1],[{windowId:22,url:home,active:true}]);});
test('changed old tab never creates',async()=>{let created=false;await assert.rejects(()=>createProjectHome(cfg,async(m)=>{if(m==='tabs.create')created=true;return {id:11,windowId:23,url:home};}),/PARENT_CHANGED/);assert.equal(created,false);});
test('unexpected new url is a failure not a second create',async()=>{let creates=0;await assert.rejects(()=>createProjectHome(cfg,async(m,a)=>{if(m==='windows.get')return {id:22,type:'normal'};if(m==='tabs.create'){creates++;return{id:12};}return {id:a[0],windowId:22,url:a[0]===11?home:'https://www.xiaohongshu.com/login',active:true};}),/ACCESS_STOP/);assert.equal(creates,1);});

test('mismatched returned tab id is rejected',async()=>{await assert.rejects(()=>createProjectHome(cfg,async(m,a)=>{if(m==='windows.get')return {id:22,type:'normal'};if(m==='tabs.create')return {id:12};return {id:a[0]===11?11:999,windowId:22,url:home,active:true};}),/CREATE_MISMATCH/);});

const probeCfg={...cfg,missingTabId:12};
function nativeProbe(mode,calls) {return async(m,a)=>{calls.push([m,a]);if(m==='windows.get')return{id:22,type:'normal'};if(m==='tabs.create')throw new Error('Unexpected creation');if(a[0]===11)return{id:11,windowId:22,url:home};if(mode==='present')return{id:12,windowId:22,url:home};if(mode==='wrong-id')return{id:99};throw new Error(mode==='missing'?'chrome.tabs.get: No tab with id: 12.':mode);};}
test('prebind missing probe only checks exact target without creating or selecting',async()=>{const calls=[];assert.deepEqual(await probeMissingPrebind(probeCfg,nativeProbe('missing',calls)),{state:'native_target_absent',tabId:12,windowId:22});assert.deepEqual(calls.map(x=>x[0]),['tabs.get','windows.get','tabs.get']);});
test('prebind existing target is not absence',async()=>{assert.deepEqual(await probeMissingPrebind(probeCfg,nativeProbe('present',[])),{state:'native_target_present',tabId:12});});
test('prebind unknown errors and different target errors stay unknown',async()=>{for(const msg of ['Connection timed out','chrome.tabs.get: No tab with id: 99.'])await assert.rejects(()=>probeMissingPrebind(probeCfg,nativeProbe(msg,[])));});
test('prebind mismatched returned target is rejected',async()=>{await assert.rejects(()=>probeMissingPrebind(probeCfg,nativeProbe('wrong-id',[])),/TARGET_UNCONFIRMED/);});
test('prebind cannot probe the parent itself',async()=>{await assert.rejects(()=>probeMissingPrebind({...probeCfg,missingTabId:11},nativeProbe('missing',[])),/REGISTERED_NATIVE_TAB_REQUIRED/);});

test('actual probe action dispatch never creates a home',async()=>{const calls=[];const result=await dispatchProjectHome({...probeCfg,action:'probe_missing_prebind'},nativeProbe('missing',calls));assert.equal(result.state,'native_target_absent');assert.equal(calls.filter(x=>x[0]==='tabs.create').length,0);});
test('unknown action dispatch cannot silently create',async()=>{const calls=[];await assert.rejects(()=>dispatchProjectHome({...cfg,action:'unknown'},nativeProbe('missing',calls)),/ACTION_NOT_ALLOWED/);assert.equal(calls.length,0);});

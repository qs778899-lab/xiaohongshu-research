import assert from 'node:assert/strict';
import {advanceDom} from './xhs_bridge_driver.mjs';
globalThis.GUARD=()=>({});
let panelOpen=false,selection='综合',clicks=0,top=600;
const labels=['综合','最新','最多点赞'];
const choices=labels.map(label=>({innerText:label,getClientRects:()=>[{}],contains:()=>false,
  click(){clicks++;selection=label;top=0;}}));
const panel={getClientRects:()=>[{}],querySelectorAll(selector){
  if(selector==='span,div,button')return choices;
  if(selector==='.active,[aria-checked="true"]')return choices.filter(x=>x.innerText===selection);
  return [];
}};
const opener={innerText:'筛选',getClientRects:()=>[{}],contains:()=>false,click(){panelOpen=true;}};
globalThis.document={querySelectorAll(selector){
  if(selector==='.filter-container,.filter-panel')return panelOpen?[panel]:[];
  if(selector==='span,div,button')return [opener];
  return [];
}};
assert.equal(advanceDom({stage:'search',sort:'general'}).action,'filter_opened');
assert.equal(advanceDom({stage:'search',sort:'general'}).action,'sort_already_selected');
assert.equal(clicks,0);assert.equal(top,600);
assert.equal(advanceDom({stage:'search',sort:'latest'}).action,'sort_clicked');
assert.equal(clicks,1);assert.equal(top,0);
assert.equal(advanceDom({stage:'search',sort:'latest'}).action,'sort_already_selected');
assert.equal(clicks,1);
panelOpen=false;opener.innerText='已筛选';top=750;
assert.equal(advanceDom({stage:'search',sort:'latest'}).action,'filter_opened');
assert.equal(advanceDom({stage:'search',sort:'latest'}).action,'sort_already_selected');
assert.equal(clicks,1);assert.equal(top,750);
panelOpen=false;opener.innerText='清除全部已筛选';
assert.equal(advanceDom({stage:'search',sort:'latest'}).error,'SORT_CONTROL_UNAVAILABLE');
assert.equal(clicks,1);
globalThis.GUARD=()=>({error:'ACCOUNT_MISMATCH'});
assert.equal(advanceDom({stage:'search',sort:'popular'}).error,'ACCOUNT_MISMATCH');
assert.equal(clicks,1);
console.log('PASS sort opens, observes existing selection without reclick, changes only different sort, and guards account');

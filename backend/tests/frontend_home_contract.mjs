import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const noop = () => {};
const nodes = new Map();
const sandbox = {
  console, __routeSeq: 1, RENDER: {}, route: {query:{}},
  window:{addEventListener:noop}, document:{hidden:false,querySelector:()=>null},
  localStorage:{getItem:()=>null}, location:{hash:'#/'},
  setInterval:()=>0, clearInterval:noop, setTimeout, clearTimeout,
  $:sel=>nodes.get(sel)||null, $$:()=>[], esc:s=>String(s??''),
  api:async()=>({ok:false}), toast:noop,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
vm.runInContext(`
  sfRenderLoginAll = () => {};
  sfRenderHomeLogin = () => {};
  sfBindBrowserChannel = () => {};
  sfBrowserChannelBlock = () => '';
  sfSetBoss = () => {};
`, sandbox);
let html = 'another page';
const main = {get innerHTML(){return html;},set innerHTML(value){
  html=value; nodes.clear();
  for(const [,id] of value.matchAll(/id="([^"]+)"/g)) {
    const handlers={};
    nodes.set('#'+id,{value:'',innerHTML:'',handlers,addEventListener:(event,fn)=>{handlers[event]=fn;}});
  }
}};
let release;
sandbox.sfLoadHome = () => new Promise(resolve=>{release=resolve;});
sandbox.sfLoadBrowserChannel = async()=>{};
const stale = sandbox.RENDER.home(main);
sandbox.__routeSeq=2;
release({}); await stale;
assert.equal(html,'another page','stale async home must not replace a newer route');
sandbox.sfLoadHome=async()=>({});
await sandbox.RENDER.home(main);
let submitted=0, prevented=false;
sandbox.sfSubmitSearch=()=>{submitted++;};
nodes.get('#sf-search').handlers.submit({preventDefault(){prevented=true;}});
assert.equal(submitted,1);
assert.equal(prevented,true);
assert.ok(nodes.get('#sf-q').handlers.input);
console.log('Home navigation race and live submit handler passed');

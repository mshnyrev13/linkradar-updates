const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(require('path').join(__dirname,'../static/app.js'),'utf8');
const fn=source.slice(source.indexOf('async function quickAction('),source.indexOf("$('plotter-setup').onclick"));
let starts=0,stops=0;
const fields={'plotter-address':{value:'example.com'},'stream-port':{value:1935},'focus-end':{value:''}};
const state={snapshot:{active_id:'old',session:{id:'old',config:{interval:.5}},targets:[{host:'example.com'}]}};
const ctx={URLSearchParams,state,$:id=>fields[id],interval:{value:'.5'},selected:()=>state.snapshot.targets[0],Streaming:{endpoint:host=>({host,port:1935})},streamProfile:()=>({port:1935}),syncQuick:()=>{},toast:()=>{},localStorage:{setItem:()=>{}},waitForStop:async()=>{stops++;state.snapshot.active_id=null},refresh:async()=>{},api:async(path)=>{if(path==='/api/start'){starts++;state.snapshot.active_id='new';state.snapshot.session.id='new';return {id:'new'}}return {};}};
vm.createContext(ctx);vm.runInContext(fn,ctx);
(async()=>{await ctx.quickAction({type:'click'});assert.equal(stops,1);assert.equal(starts,0);await ctx.quickAction();assert.equal(starts,1);await ctx.quickAction();assert.equal(stops,2);assert.equal(starts,1);assert.equal(state.snapshot.active_id,null);console.log('Click stop → start → stop: OK');})();

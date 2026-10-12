const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname, '../..');
async function fixture(page, options={}) {
  const calls=[];
  const room={id:'fixture/alpha', title:'Alpha', runtime:'codex', controllable:true,
    commands:[{name:'/model'}], draft:{body:'',version:0}, approvals:[],
    ownership:{state:'atlas_owned'}, native:{thread_id:'native-alpha',active_turn:null,
      model:'previous-model',catalog:{available:true,models:[{model:'new-model',efforts:['high']}]}}};
  let attempts=0, refreshed=false;
  await page.route('**/*',async route=>{
    const u=new URL(route.request().url());
    if(u.origin!=='https://fixture.test')return route.abort();
    if(u.pathname.startsWith('/api/')) {
      const body=route.request().postDataJSON();
      calls.push({path:u.pathname,body});
      if(u.pathname.endsWith('/command')) {
        attempts++;
        if(attempts===1 || options.alwaysRefuse || (options.refuseUntilRefreshed && !refreshed)) return route.fulfill({status:502,
          json:{error:options.error || 'unsupported_model',message:'Model is unavailable on this connection.'}});
        room.native.model='new-model';
        return route.fulfill({json:{command:{state:'updated',message:'Model updated for later turns.',native:{model:'new-model'}}}});
      }
      if(u.pathname.endsWith('/connection/refresh')) {
        if(options.hold) await options.hold;
        if(options.busy) return route.fulfill({status:409,json:{error:'connection_busy',message:'The session is working.'}});
        refreshed=true;
        return route.fulfill({json:{connection:{state:'refreshed',scope:'conversation',room:body.room}}});
      }
      if(u.pathname.endsWith('/room/fixture/alpha')) {
        return route.fulfill({json:options.retarget?{...room,native:{...room.native,thread_id:'native-other'}}:room});
      }
      if(u.pathname.endsWith('/history'))return route.fulfill({json:{items:[],complete:true}});
      return route.fulfill({json:{}});
    }
    const name=u.pathname==='/'?'index.html':u.pathname.slice(1), file=path.join(root,'app/console',name);
    if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
    let body=fs.readFileSync(file);
    if(name==='index.html')body=body.toString().replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g,'');
    if(name==='app.js')body=body.toString().replace('\nboot();','\n/* synthetic fixture */');
    return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('https://fixture.test/');
  await page.waitForFunction(()=>window.__atlas);
  await page.evaluate(room=>{
    Object.assign(state,{room:room.id,detail:room,csrf:'fixture',agent:'remote',agentGen:1,roomSeq:1,
      agents:[{id:'remote',label:'Remote'}],roomRefreshing:false,eventRecovery:false,connKind:'live',
      freshness:{room:Date.now(),history:Date.now(),roomError:'',historyError:''}});
    document.querySelector('#draft').value='unsent words';
  },room);
  return {calls,room};
}
const writes=calls=>calls.filter(c=>c.body);
test('model selection refreshes only its older connection and retries once',async({page})=>{
  const f=await fixture(page);
  expect(await page.evaluate(()=>sendCommand('/model new-model',false))).toEqual({ok:true});
  const sent=writes(f.calls);
  expect(sent.map(c=>c.path)).toEqual([
    '/api/agents/remote/api/room/fixture/alpha/command',
    '/api/agents/remote/api/connection/refresh',
    '/api/agents/remote/api/room/fixture/alpha/command']);
  expect(sent[1].body).toEqual({room:'fixture/alpha'});
  expect(sent[0].body).toEqual(sent[2].body);
  expect(await page.evaluate(()=>state.detail.native.model)).toBe('new-model');
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});
for(const options of [{error:'upstream_timeout'}, {retarget:true}, {unadvertised:true}]) {
  test('model recovery refuses unsafe retry '+JSON.stringify(options),async({page})=>{
    const f=await fixture(page,options);
    if(options.unadvertised)await page.evaluate(()=>state.detail.native.catalog.models=[]);
    const result=await page.evaluate(()=>sendCommand('/model new-model',false));
    expect(result.ok).toBe(false);
    expect(writes(f.calls)).toHaveLength(1);
    await expect(page.locator('#draft')).toHaveValue('unsent words');
  });
}
test('persistent model refusal ends after one refresh with an explanatory error',async({page})=>{
  const f=await fixture(page,{alwaysRefuse:true});
  const result=await page.evaluate(()=>sendCommand('/model new-model',false));
  expect(result).toEqual({ok:false,message:'Model is unavailable on this connection.'});
  expect(writes(f.calls)).toHaveLength(3);
});
test('busy refresh queues the selection then recovers when idle',async({page})=>{
  const options={busy:true,refuseUntilRefreshed:true};const f=await fixture(page,options);
  expect(await page.evaluate(()=>sendCommand('/model new-model',false))).toEqual({ok:true,queued:true});
  options.busy=false;
  // The writer still refuses until refreshed; the queue uses the same recovery.
  await page.evaluate(()=>drainCommandQueue());
  expect(writes(f.calls).filter(c=>c.path.endsWith('/connection/refresh'))).toHaveLength(2);
  expect(await page.evaluate(()=>queuedCommands()[0].state)).toBe('complete');
});
test('navigation during recovery cannot redirect the model change or overwrite a new draft',async({page})=>{
  let release; const hold=new Promise(resolve=>release=resolve);
  const f=await fixture(page,{hold});
  await page.evaluate(()=>{window.change=sendCommand('/model new-model',false);});
  await expect.poll(()=>writes(f.calls).length).toBe(2);
  await page.evaluate(()=>{state.room='fixture/beta';state.roomSeq++;state.agent='another';state.agentGen++;
    state.detail={id:'fixture/beta',native:{thread_id:'native-beta'}};
    document.querySelector('#draft').value='new beta draft';});
  release();await page.evaluate(()=>window.change);
  expect(writes(f.calls).at(-1).path).toBe('/api/agents/remote/api/room/fixture/alpha/command');
  expect(await page.evaluate(()=>state.room)).toBe('fixture/beta');
  await expect(page.locator('#draft')).toHaveValue('new beta draft');
});

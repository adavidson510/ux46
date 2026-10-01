const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname, '../..');
async function fixture(page, accountStatus=200) {
  const calls=[];
  await page.route('**/*',async route=>{
    const u=new URL(route.request().url());
    if(u.origin!=='https://fixture.test')return route.abort();
    if(u.pathname.startsWith('/api/')){
      calls.push([route.request().method(),u.pathname]);
      if(u.pathname.endsWith('/account-usage'))return route.fulfill({status:accountStatus,json:accountStatus===200?{
        state:'reported',checked_at:Date.now()/1000,source:'saved_login',available_resets:3,
        buckets:[{id:'codex',credits_available:true,windows:[{used_percent:100,minutes:10080,reset_at:Date.now()/1000+600}]}]
      }:{error:'not_found'}});
      if(u.pathname.endsWith('/usage'))return route.fulfill({json:{usage:{input_tokens:1234,output_tokens:56}}});
      return route.fulfill({json:{id:'fixture/alpha',native_terminal:null}});
    }
    const name=u.pathname==='/'?'index.html':u.pathname.slice(1), file=path.join(root,'app/console',name);
    if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
    let body=fs.readFileSync(file);
    if(name==='index.html')body=body.toString().replace(/<script src="\/(?!app.js|efficiency.js)[^"]+"><\/script>/g,'');
    if(name==='app.js')body=body.toString().replace('\nboot();','\n/* controlled fixture */');
    return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('https://fixture.test/');
  await page.waitForFunction(()=>window.__atlas && window.UX46Efficiency);
  await page.evaluate(()=>{
    Object.assign(state,{roomRefreshing:false,eventRecovery:false,connKind:'live',freshness:{room:Date.now(),history:Date.now(),roomError:'',historyError:''},agent:'aster',room:'fixture/alpha',detail:{id:'fixture/alpha',title:'Alpha',controllable:true,commands:[],runtime:'codex',native:{thread_id:'native-a',active_turn:'running'},ownership:{state:'atlas_owned'},account_status:{state:'limited',checked_at:Date.now()/1000}},agents:[{id:'aster',label:'Aster'}]});
    document.querySelector('#draft').value='/usage';
    settleDraftAfterSend=async()=>{document.querySelector('#draft').value='';};
  });
  return calls;
}
test('usage opens during a turn on the selected agent, without native dispatch or queue',async({page})=>{
  const calls=await fixture(page);
  expect(await page.evaluate(()=>commandSupported('/usage'))).toBe(true);
  await page.evaluate(()=>sendCommand('/usage',true));
  const dialog=page.locator('#efficiencyDialog');await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('0% remaining');await expect(dialog).toContainText('Credits available');
  await expect(dialog).toContainText('Weekly allowance');await expect(dialog).toContainText('1,234');
  await expect(page.locator('#draft')).toHaveValue('');
  expect(calls.filter(([method])=>method!=='GET')).toEqual([]);
  expect(calls.filter(([,url])=>url.includes('/account-usage')).map(([,url])=>url)).toEqual(['/api/agents/aster/api/account-usage']);
  expect(await page.evaluate(()=>queuedCommands())).toEqual([]);
});
test('unavailable quota stays unknown, and arguments leave the draft intact',async({page})=>{
  await fixture(page,404);
  await page.evaluate(()=>sendCommand('/usage reset',true));
  await expect(page.locator('#efficiencyDialog')).not.toBeVisible();await expect(page.locator('#draft')).toHaveValue('/usage');
  await page.evaluate(()=>sendCommand('/usage',true));
  await expect(page.locator('#efficiencyDialog')).toContainText('Unknown does not mean exhausted');
  await expect(page.locator('#efficiencyDialog')).not.toContainText('0% remaining');
});
test('an exhausted allowance does not relabel a successful reply as a failed turn',async({page})=>{
  await fixture(page);
  const activity=await page.evaluate(()=>{
    state.detail.native.active_turn=null;state.items=state.tail=[{id:'answer',type:'agentMessage',phase:'final_answer',turn_id:'turn',text:'Done'}];
    return activityState();
  });
  expect(activity.kind).toBe('answered');expect(activity.text).toBe('Answered');
});
test('mobile allowance controls fit the dialog',async({page})=>{
  await page.setViewportSize({width:390,height:844});await fixture(page);
  await page.evaluate(()=>sendCommand('/usage',true));await expect(page.getByText('0% remaining')).toBeVisible();
  const size=await page.locator('#efficiencyDialog').evaluate(node=>({width:node.scrollWidth,client:node.clientWidth}));
  expect(size.width).toBeLessThanOrEqual(size.client+1);
  await page.getByRole('button',{name:'Close usage and continuity'}).click();await expect(page.locator('#efficiencyDialog')).not.toBeVisible();
});

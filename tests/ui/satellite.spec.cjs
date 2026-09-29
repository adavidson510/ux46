const {test,expect}=require('@playwright/test');
const fs=require('node:fs'),path=require('node:path');
const root=process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname,'../..');
async function fixture(page,satellite=true){
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.origin!=='https://fixture.test')return route.abort();
  if(u.pathname.startsWith('/api/'))return route.fulfill({json:{}});
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1),file=path.join(root,'app/console',name);
  if(!fs.existsSync(file))return route.fulfill({status:404});
  let body=fs.readFileSync(file,'utf8');
  if(name==='index.html')body=body.replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g,'');
  if(name==='app.js')body=body.replace('\nboot();','\n/* controlled fixture */');
  return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
 });
 await page.goto('https://fixture.test/?agent=local&room=project/at'+(satellite?'&satellite=1':''));
 await page.waitForFunction(()=>Boolean(window.__atlas));
 await page.evaluate(()=>{state.agents=[{id:'local',label:'Local'}];state.room='project/at';state.tabs=readTabs();renderTabs();});
}
test('satellite preserves shared tabs and current room through remote layout changes and close',async({page})=>{
 const writes=[];page.on('request',r=>{if(r.method()!=='GET')writes.push(r.url());});
 await fixture(page);
 const before=await page.evaluate(()=>{localStorage.setItem('atlas.tabs.2','original');localStorage.setItem('atlas.room','project/main');return localStorage.getItem('atlas.tabs.2');});
 await page.evaluate(async()=>{
  shared.applied={tabs:[],desktopId:'default',generation:0};state.desk.available=true;
  openTab('local','project/at',{title:'AT'});rememberRoom('project/at');rememberAgent(false);
  await applySharedLayout({tabs:[{agent:'local',room:'project/other'}],active:{agent:'local',room:'project/other'}});
  await refreshSharedLayout();await checkDesktopDevice(true);
 });
 expect(await page.evaluate(()=>({room:state.room,tabs:state.tabs.map(t=>t.room),ops:shared.ops.length,saved:localStorage.getItem('atlas.room'),tabsSaved:localStorage.getItem('atlas.tabs.2')}))).toEqual({room:'project/at',tabs:['project/at'],ops:0,saved:'project/main',tabsSaved:before});
 expect(await page.evaluate(()=>openSession('local','project/other'))).toBe(false);
 await page.evaluate(async()=>{flushDraft=async()=>{};window.close=()=>{window.didClose=true;};await closeTab(state.tabs[0]);});
 expect(await page.evaluate(()=>window.didClose)).toBe(true);expect(writes).toEqual([]);
 await expect(page.locator('#side')).not.toBeVisible();await expect(page.locator('#satelliteLabel')).toBeVisible();
});
test('opening a satellite targets the exact agent and room without moving the main view',async({page})=>{
 await fixture(page,false);
 const opened=await page.evaluate(()=>{
  window.open=(...args)=>{window.openedArgs=args;};
  openSatellite({agent:'remote',room:'project/at'});
  return {args:window.openedArgs,room:state.room,ops:shared.ops.length};
 });
 const u=new URL(opened.args[0]);expect(u.searchParams.get('agent')).toBe('remote');expect(u.searchParams.get('room')).toBe('project/at');expect(u.searchParams.get('satellite')).toBe('1');expect(opened.args[2]).toContain('noopener');expect(opened.room).toBe('project/at');expect(opened.ops).toBe(0);
});

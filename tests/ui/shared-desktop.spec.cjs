const {test, expect} = require('@playwright/test');
const fs = require('node:fs'), path = require('node:path');
const root = process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname, '../..');
async function fixture(page, envelope, width = 1440) {
  await page.setViewportSize({width, height: 900});
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'https://fixture.test') return route.abort();
    if (url.pathname.startsWith('/api/')) return route.fulfill({json: {}});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1), file = path.join(root, 'app/console', name);
    if (!fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
    let body = fs.readFileSync(file, 'utf8');
    if (name === 'index.html') body = body.replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g, '');
    if (name === 'app.js') body = body.replace('\nboot();', '\n/* fixture controls boot */');
    await route.fulfill({body, contentType: name.endsWith('.js') ? 'application/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
  });
  await page.goto('https://fixture.test/');
  await page.waitForFunction(() => Boolean(window.__atlas));
  await page.route('**/api/desktop-state', async route => {
    if (route.request().method() === 'PUT') {
      const p = route.request().postDataJSON();
      if (p.base_version !== envelope.version) return route.fulfill({status:409,json:{error:'desktop_conflict',detail:envelope}});
      envelope.state = p.state; envelope.version++;
    }
    return route.fulfill({json:envelope});
  });
  await page.evaluate(async envelope => {
    state.device = 'browser-' + Math.random(); state.agents=[{id:'local',label:'Local'}];
    state.room='project/keep'; state.agent='local'; state.detail={controllable:true,native:{}};
    state.connKind='live'; state.eventRecovery=false; state.roomRefreshing=false;
    state.freshness={room:Date.now(),history:Date.now(),roomError:'',historyError:''};
    selectLiveDesktop('default'); adoptDesktopState(envelope);
    state.tabs=JSON.parse(JSON.stringify(envelope.state.liveDesktops[0].layout.tabs));
    shared.applied=layoutRecord(envelope.state); renderTabs();
    document.querySelector('#draft').value='Keep this unsent draft';
    openSession=async()=>true;
  }, envelope);
}
const tab = room => ({agent:'local',room:'project/'+room,title:room,controllable:false});
const makeEnvelope = () => ({version:1,state:{schema_version:1,aliases:[],desktops:[],liveDesktops:[{id:'default',name:'Desktop 1',layout:{version:1,generation:0,tabs:[tab('keep'),tab('closed')],active:{agent:'local',room:'project/keep'},customizations:{version:1}}}]}});
const rooms = envelope => envelope.state.liveDesktops[0].layout.tabs.map(t=>t.room);

test('a stale phone adding another tab cannot resurrect a tab closed on desktop', async ({browser}) => {
  const envelope=makeEnvelope(); const desktopContext=await browser.newContext(), phoneContext=await browser.newContext();
  const desktop=await desktopContext.newPage(), phone=await phoneContext.newPage();
  await fixture(desktop,envelope);await fixture(phone,envelope,390);
  await desktop.evaluate(async()=>{forgetTab('local','project/closed');clearTimeout(shared.timer);await flushLayoutOps();});
  expect(rooms(envelope)).toEqual(['project/keep']);
  await phone.evaluate(async()=>{openTab('local','project/new',{title:'New'});clearTimeout(shared.timer);await flushLayoutOps();});
  expect(rooms(envelope)).toEqual(['project/keep','project/new']);
  await desktop.evaluate(()=>refreshSharedLayout());
  expect(await phone.evaluate(()=>state.tabs.map(t=>t.room))).toEqual(rooms(envelope));
  expect(await desktop.evaluate(()=>state.tabs.map(t=>t.room))).toEqual(rooms(envelope));
  await expect(phone.locator('#draft')).toHaveValue('Keep this unsent draft');
  // An explicit new open after learning of the close is still allowed.
  await phone.evaluate(async()=>{openTab('local','project/closed',{title:'Reopened'});clearTimeout(shared.timer);await flushLayoutOps();});
  expect(rooms(envelope)).toContain('project/closed');
  await desktopContext.close();await phoneContext.close();
});

test('a metadata read cannot hide a changed layout from the next sync, even from another window on this device', async ({page}) => {
  const envelope=makeEnvelope();await fixture(page,envelope);
  envelope.state.liveDesktops[0].layout.tabs=[tab('keep')];envelope.version++;
  envelope.state.liveDesktops[0].layout.updated_by=await page.evaluate(()=>state.device);
  await page.evaluate(async()=>{await loadDesktopState();await refreshSharedLayout();});
  expect(await page.evaluate(()=>state.tabs.map(t=>t.room))).toEqual(['project/keep']);
  await expect(page.locator('#draft')).toHaveValue('Keep this unsent draft');
});

test('closing the viewed tab elsewhere keeps its draft but remembering the view cannot reopen it', async ({page}) => {
  const envelope=makeEnvelope();await fixture(page,envelope,390);
  await page.evaluate(()=>{state.room='project/closed';renderTabs();});
  envelope.state.liveDesktops[0].layout.tabs=[tab('keep')];envelope.version++;
  await page.evaluate(async()=>{await refreshSharedLayout();rememberRoom(state.room);});
  expect(await page.evaluate(()=>state.tabs.map(t=>t.room))).toEqual(['project/keep']);
  expect(await page.evaluate(()=>state.room)).toBe('project/closed');
  expect(await page.evaluate(()=>shared.ops.length)).toBe(0);
  await expect(page.locator('#draft')).toHaveValue('Keep this unsent draft');
});

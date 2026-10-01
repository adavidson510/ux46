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

test('startup ignores a closed restored tab, honors a fresh link, and keeps an empty shared desktop empty', async ({page}) => {
  const envelope=makeEnvelope();await fixture(page,envelope,390);
  expect(await page.evaluate(()=>{
    const layout={tabs:[{agent:'local',room:'project/keep'}],active:{agent:'local',room:'project/keep'}};
    const before=[{agent:'local',room:'project/closed'}];
    return ['navigate','reload','back_forward'].map(type=>sharedStartupTarget(layout,'local','project/closed',before,type));
  })).toEqual(Array(3).fill({agent:'local',room:'project/keep'}));
  expect(await page.evaluate(()=>sharedStartupTarget({tabs:[],active:null},'local','project/new',[],'navigate'))).toEqual({agent:'local',room:'project/new'});
  await page.route('**/api/rooms?**',route=>route.fulfill({json:{rooms:[]}}));
  await page.evaluate(async()=>{
    state.tabs=[];shared.applied.tabs=[];shared.applied.active=null;
    // The old URL and last room remain, as on a reopened phone app.
    history.replaceState(null,'','?room=project/closed');localStorage.setItem('atlas.room','project/closed');
    await enterAgent({voice:{},seq:0},'',{sharedStartup:true,toTail:true,connect:true});
  });
  expect(await page.evaluate(()=>state.room)).toBeNull();
  expect(await page.evaluate(()=>state.tabs)).toEqual([]);
});

test('shared names beat stale tab copies and renaming persists across phone, reload and saved layouts', async ({browser}) => {
  const envelope=makeEnvelope();
  envelope.state.aliases=[{agent:'local',room:'project/keep',label:'Already saved'}];
  envelope.state.liveDesktops[0].layout.tabs[0].customLabel='Old tab name';
  envelope.state.desktops=[{id:'snapshot',name:'Saved desktop',tabs:[{...tab('keep'),customLabel:'Even older name'}]}];
  const desktopContext=await browser.newContext(),phoneContext=await browser.newContext();
  const desktop=await desktopContext.newPage(),phone=await phoneContext.newPage();
  await fixture(desktop,envelope);await fixture(phone,envelope,390);
  await expect(desktop.locator('#tabs .on .tname')).toHaveText('Already saved');
  await desktop.getByRole('button',{name:'Options for Already saved',exact:true}).click();
  const input=desktop.getByRole('textbox',{name:'Display name for this session'});
  await input.fill('New room name');
  await expect(desktop.locator('.tabmenu-rename .primary')).toBeVisible();
  await desktop.screenshot({path:test.info().outputPath('rename-highlight.png')});
  await desktop.getByRole('button',{name:'Rename',exact:true}).click();
  await expect(desktop.locator('#tabMenu')).toBeHidden();
  expect(envelope.state.aliases[0].label).toBe('New room name');
  expect(envelope.state.desktops[0].tabs[0].customLabel).toBeUndefined();
  await phone.evaluate(()=>refreshSharedLayout());
  await expect(phone.locator('#mobileTabName')).toHaveText('New room name');
  // A stale tab carrying its old name cannot override the persisted alias.
  await phone.evaluate(()=>{state.tabs[0].customLabel='Stale phone name';openTab('local','project/new',{title:'Another'});clearTimeout(shared.timer);return flushLayoutOps();});
  const reopened=await desktopContext.newPage();await fixture(reopened,envelope);
  await expect(reopened.locator('#tabs .on .tname')).toHaveText('New room name');
  await reopened.getByRole('button',{name:'Options for New room name',exact:true}).click();
  await reopened.getByRole('button',{name:'Reset name',exact:true}).click();
  await expect(reopened.locator('#tabMenu')).toBeHidden();
  await phone.evaluate(()=>refreshSharedLayout());await expect(phone.locator('#mobileTabName')).toHaveText('keep');
  await expect(desktop.locator('#draft')).toHaveValue('Keep this unsent draft');
  await desktopContext.close();await phoneContext.close();
});

test('a failed rename keeps the typed name visible for retry and does not pretend it saved', async ({page}) => {
  const envelope=makeEnvelope();await fixture(page,envelope);
  let fail=true;
  await page.route('**/api/desktop-state',route=>{
    if(route.request().method()==='PUT'&&fail)return route.fulfill({status:503,json:{message:'Workspace unavailable'}});
    return route.fallback();
  });
  await page.getByRole('button',{name:'Options for keep',exact:true}).click();
  const input=page.getByRole('textbox',{name:'Display name for this session'});await input.fill('My room');
  await page.getByRole('button',{name:'Rename',exact:true}).click();
  await expect(input).toHaveValue('My room');await expect(input).toBeEnabled();
  await expect(page.locator('.tabmenu-error')).toContainText('Workspace unavailable');
  await expect(page.locator('#tabs .on .tname')).toHaveText('keep');
  fail=false;await page.getByRole('button',{name:'Rename',exact:true}).click();
  await expect(page.locator('#tabMenu')).toBeHidden();await expect(page.locator('#tabs .on .tname')).toHaveText('My room');
});

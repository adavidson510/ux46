const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const detail = (room, extra = {}) => ({
  id: room, title: room, runtime: 'codex', controllable: true,
  native: {thread_id: room, active_turn: null}, ownership: {state: 'atlas_owned'},
  draft: {body: '', version: 0}, approvals: [], ...extra,
});
const item = id => ({id, type: 'agentMessage', text: id, phase: 'final_answer', turn_id: id});
const history = (ids, next = null) => ({items: ids.map(item), complete: !next, next_cursor: next});

// Run the shipped DOM, styling and functions. Only automatic boot is withheld
// so each journey controls delayed transport deterministically; no native agent
// or owner's state directory is involved, and all external requests are refused.
async function fixture(page) {
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'https://fixture.test') return route.abort();
    if (url.pathname.startsWith('/api/')) return route.fulfill({json: {}});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    const file = path.join(root, 'app/console', name);
    if (!fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
    let body = fs.readFileSync(file);
    if (name === 'index.html') body = body.toString().replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g, '');
    if (name === 'app.js') body = body.toString().replace('\nboot();', '\n/* boot controlled by synthetic journey */');
    const contentType = name.endsWith('.js') ? 'application/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html';
    await route.fulfill({body, contentType});
  });
  await page.goto('https://fixture.test/');
  await page.waitForFunction(() => Boolean(window.__atlas));
  await seed(page, 'fixture/alpha');
}
async function seed(page, room, agent = '') {
  await page.evaluate(({room, agent, d, rows}) => {
    Object.assign(__atlas.state, {agent, agentGen: __atlas.state.agentGen + 1,
      room, roomSeq: __atlas.state.roomSeq + 1, detail: d,
      items: rows, tail: rows, ids: new Set(rows.map(i => i.id)), cursor: 'older',
      eventRecovery: false, connKind: 'live', roomRefreshing: false,
      following: false, freshness: {room: Date.now(), history: Date.now(), roomError: '', historyError: ''}});
    document.querySelector('#draft').value = 'unsent words';
    __atlas.renderStream();
  }, {room, agent, d: detail(room), rows: [item(room + '-now')]});
}

for (const change of ['room', 'agent with same room', 'generation']) {
  test(`late older history cannot cross ${change}`, async ({page}) => {
    await fixture(page);
    let release;
    const held = new Promise(resolve => release = resolve);
    let requested;
    const started = new Promise(resolve => requested = resolve);
    await page.route('**/api/room/**/history?**', async route => {
      requested(); await held;
      await route.fulfill({json: history(['wrong-old-room'], 'wrong-cursor')});
    });
    await page.evaluate(() => { window.pendingEarlier = __atlas.loadEarlier(); });
    await started;
    const room = change === 'room' ? 'fixture/beta' : 'fixture/alpha';
    await seed(page, room, change === 'agent with same room' ? 'other-agent' : '');
    const before = await page.evaluate(() => ({items: __atlas.state.items, cursor: __atlas.state.cursor,
      draft: document.querySelector('#draft').value, scroll: document.querySelector('#stream').scrollTop}));
    release();
    await page.evaluate(() => window.pendingEarlier);
    const after = await page.evaluate(() => ({items: __atlas.state.items, cursor: __atlas.state.cursor,
      draft: document.querySelector('#draft').value, scroll: document.querySelector('#stream').scrollTop}));
    expect(after).toEqual(before);
    await expect(page.locator('#stream')).not.toContainText('wrong-old-room');
  });
}

test('newer room read wins; failed refresh retains content with visible stale state', async ({page}) => {
  await fixture(page);
  let release, requested;
  const held = new Promise(resolve => release = resolve);
  const started = new Promise(resolve => requested = resolve);
  let reads = 0;
  await page.route('**/api/room/fixture/alpha', async route => {
    const first = ++reads === 1;
    if (first) { requested(); await held; }
    await route.fulfill({json: detail('fixture/alpha', {title: first ? 'obsolete' : 'current'})});
  });
  await page.evaluate(() => { window.firstRead = __atlas.refreshRoomState(); });
  await started;
  await page.evaluate(() => __atlas.refreshRoomState());
  release(); await page.evaluate(() => window.firstRead);
  expect(await page.evaluate(() => __atlas.state.detail.title)).toBe('current');
  await page.route('**/api/room/fixture/alpha', route => route.fulfill({status: 503, json: {error: 'fixture unavailable'}}));
  await page.evaluate(() => __atlas.refreshRoomState());
  await expect(page.locator('#activity')).toContainText('Last known view');
  await page.evaluate(() => setConn('reachable', 'live'));
  await expect(page.locator('#activity')).toContainText('Conversation could not be checked');
  await page.screenshot({path: test.info().outputPath('last-known-view.png')});
  expect(await page.evaluate(() => roomStatusOf({agent: agentId(), room: state.room}).current)).toBe(false);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('history catches up across pages and ignores an older tail read', async ({page}) => {
  await fixture(page);
  const calls = [];
  await page.route('**/api/room/**/history?**', async route => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor'); calls.push(cursor);
    await route.fulfill({json: cursor ? history(['older-new', 'fixture/alpha-now']) : history(['newest'], 'page-2')});
  });
  await page.evaluate(() => __atlas.refreshTail());
  expect(calls).toEqual([null, 'page-2']);
  expect(await page.evaluate(() => __atlas.state.items.map(i => i.id))).toEqual(['fixture/alpha-now', 'older-new', 'newest']);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
  let release, requested;
  const held = new Promise(resolve => release = resolve);
  const started = new Promise(resolve => requested = resolve);
  let n = 0;
  await page.route('**/api/room/**/history?**', async route => {
    const first = ++n === 1;
    if (first) { requested(); await held; }
    await route.fulfill({json: history([first ? 'stale-tail' : 'current-tail', 'newest'])});
  });
  await page.evaluate(() => { window.oldTail = __atlas.refreshTail(); });
  await started;
  await page.evaluate(() => __atlas.refreshTail());
  release(); await page.evaluate(() => window.oldTail);
  await expect(page.locator('#stream')).not.toContainText('stale-tail');
  await expect(page.locator('#stream')).toContainText('current-tail');
});

test('epoch change and reconnect reconcile snapshots without replaying the draft', async ({page}) => {
  await fixture(page);
  const writes = [], cursors = [];
  let poll = 0;
  await page.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url());
    if (req.method() !== 'GET') writes.push(url.pathname);
    if (url.pathname === '/api/events') {
      cursors.push(url.searchParams.get('after'));
      if (++poll > 2) return; // bounded fixture holds the next long poll
      await route.fulfill({json: {seq: poll, epoch: 'new-process', gap: poll === 1,
        more: poll === 1, events: []}});
    } else if (url.pathname.endsWith('/history')) {
      await route.fulfill({json: history(['completed-after-restart', 'fixture/alpha-now'])});
    } else if (url.pathname === '/api/room/fixture/alpha') {
      await route.fulfill({json: detail('fixture/alpha')});
    } else await route.fulfill({json: {}});
  });
  await page.evaluate(() => { state.eventEpoch = 'old-process'; state.seq = 500; state.connKind = 'off'; void pollEvents(); });
  await expect.poll(() => page.evaluate(() => state.eventRecovery)).toBe(false);
  await expect(page.locator('#stream')).toContainText('completed-after-restart');
  await expect.poll(() => cursors.slice(0, 3)).toEqual(['500', '1', '2']);
  expect(writes).toEqual([]);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('unavailable history stays stale after successful event transport, then Read latest recovers', async ({page}) => {
  await fixture(page);
  await page.route('**/api/room/**/history?**', route => route.fulfill({json: {unavailable: true, items: [], message: 'fixture reader failed'}}));
  expect(await page.evaluate(() => __atlas.refreshTail())).toBe(false);
  await page.evaluate(() => setConn('reachable', 'live'));
  await expect(page.locator('#activity')).toContainText('History could not be checked');
  await expect(page.locator('#stream')).toContainText('fixture/alpha-now');
  await page.route('**/api/room/**/history?**', route => route.fulfill({json: history(['recovered-answer'])}));
  await page.route('**/api/room/fixture/alpha', route => route.fulfill({json: detail('fixture/alpha')}));
  await page.getByRole('button', {name: 'Read latest', exact: true}).click();
  await expect(page.locator('#stream')).toContainText('recovered-answer');
  await expect(page.locator('#activity')).not.toContainText('Last known view');
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('unclosed history gap is bounded and leaves the previous view intact', async ({page}) => {
  await fixture(page);
  let calls = 0;
  await page.route('**/api/room/**/history?**', route => {
    calls++;
    return route.fulfill({json: history(['unjoined-' + calls], 'page-' + calls)});
  });
  expect(await page.evaluate(() => __atlas.refreshTail())).toBe(false);
  expect(calls).toBe(20);
  expect(await page.evaluate(() => state.items.map(i => i.id))).toEqual(['fixture/alpha-now']);
  await expect(page.locator('#activity')).toContainText('Last known view');
  await expect(page.getByRole('button', {name: 'Read latest', exact: true})).toBeVisible();
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('visible agent refresh keeps its target through navigation and shares the persisted receipt', async ({page}) => {
  await fixture(page);
  const writes=[];
  let receipt;
  await page.route('**/api/recovery/**', async route => {
    if (route.request().method()==='POST') {
      const body=route.request().postDataJSON();writes.push(body);
      receipt={id:body.request_id,mode:body.mode,agent:body.agent,state:'running',message:'Checking owned connections',connections:[]};
      await route.fulfill({status:202,json:{job:receipt}});
    } else await route.fulfill({json:{supported:true,job:receipt}});
  });
  await page.locator('#btnAgentRefresh').click();
  await expect.poll(()=>writes.length).toBe(1);
  expect(writes[0].mode).toBe('agent');expect(writes[0].agent).toBe('local');
  await seed(page,'fixture/beta','other-agent');
  receipt={...receipt,state:'partial',message:'One busy connection was deferred',connections:[{agent:'local',room:'fixture/alpha',state:'deferred'}]};
  await page.evaluate(()=>checkWorkspaceRecovery());
  await expect(page.locator('#workspaceRecoveryStatus')).toContainText('One busy connection was deferred');
  await expect(page.getByRole('button',{name:'Open full UX46 recovery'})).toBeVisible();
  expect(writes).toHaveLength(1);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('explicit full recovery is one request with an interruption label and no second confirmation', async ({page}) => {
  await fixture(page);
  const writes=[];
  await page.route('**/api/recovery/**', route => {
    if (route.request().method()==='POST') {
      const body=route.request().postDataJSON();writes.push(body);
      return route.fulfill({status:202,json:{job:{id:body.request_id,mode:'all',agent:null,state:'complete',message:'Recovery finished without replay',services:[{id:'console',state:'ready'}]}}});
    }
    return route.fulfill({json:{supported:true,job:null}});
  });
  await page.evaluate(()=>{ window.confirm=()=>{throw new Error('Unexpected second confirmation');}; openWorkspaceRecovery(); });
  await expect(page.locator('#workspaceRecoveryEffect')).toContainText('Active owned work may be interrupted');
  await page.getByRole('button',{name:'Recover UX46 — may interrupt owned work',exact:true}).click();
  await expect(page.locator('#workspaceRecoveryStatus')).toContainText('Recovery finished without replay');
  expect(writes).toHaveLength(1);
  expect(Object.keys(writes[0]).sort()).toEqual(['agent','mode','request_id']);
  expect(writes[0].agent).toBeNull();expect(writes[0].mode).toBe('all');
  await expect(page.locator('#draft')).toHaveValue('unsent words');
  await page.screenshot({path:test.info().outputPath('workspace-recovery.png')});
});

test('login refresh summary counts conversations separately from the background connection',async({page})=>{
 await fixture(page);
 const items=[...Array.from({length:9},(_,i)=>({room:'fixture/room'+i,title:'Conversation '+i,state:'refreshed'})),...Array.from({length:3},(_,i)=>({room:'fixture/idle'+i,title:'Saved conversation '+i,state:'skipped',detail:'Not connected; its next connection uses the current login.'})),{room:'',title:'Session list connection',state:'refreshed'}];
 await page.evaluate(items=>{agentActions.job={agent:'local',state:'complete',items};renderAgentRefresh();document.querySelector('#agentActionsDialog').showModal();},items);
 const status=page.locator('#agentRefreshStatus');
 await expect(status).toContainText('9 conversations refreshed · 3 not running');
 await expect(status).toContainText('when you open them');
 await expect(status).toContainText('background session list was refreshed too');
 await expect(status).not.toContainText('10 refreshed');
 await page.getByText('Conversation details',{exact:true}).click();
 await expect(status).toContainText('Saved conversation 0 · Not running');
 await page.screenshot({path:test.info().outputPath('refresh-summary.png')});
 await page.evaluate(items=>{document.querySelector('#agentActionsDialog').close();openWorkspaceRecovery('agent','local');renderWorkspaceRecovery({mode:'agent',agent:'local',state:'partial',connections:items});},[{room:'fixture/current',state:'deferred'},{room:'fixture/idle',state:'not_connected'},{room:'fixture/uncertain',state:'unknown'}]);
 await expect(page.locator('#workspaceRecoveryStatus')).toContainText('1 not running · 1 waiting to finish · 1 needs attention');
 await expect(page.locator('#workspaceRecoveryStatus')).not.toContainText('Refresh complete');
});

test('customization opens the local source project even while another agent is selected', async ({page}) => {
  await fixture(page); await seed(page,'fixture/alpha','other-agent');
  const writes=[];
  await page.route('**/api/customize/start', async route => {
    writes.push(route.request().postDataJSON());
    await route.fulfill({json:{state:'created',agent:'local',new_room:{id:'ux46-workspace/customize-fixture',title:'Customize my workspace'},customization:{recovery_point:'fixture123'}}});
  });
  await page.evaluate(()=>{window.opened=[];openSession=async(...args)=>window.opened.push(args);$('#customizeWorkspace').click();});
  await expect(page.locator('#customizeDialog')).toContainText('A recovery point is saved first');
  await page.screenshot({path:test.info().outputPath('customize-source.png')});
  await page.locator('#customizeStart').click();
  await expect.poll(()=>writes.length).toBe(1);
  expect(Object.keys(writes[0])).toEqual(['client_id']);
  await expect.poll(()=>page.evaluate(()=>window.opened)).toEqual([['local','ux46-workspace/customize-fixture',{toTail:true,connect:true}]]);
  expect(await page.evaluate(()=>localStorage.getItem('ux46.customize.request'))).toBeNull();
});

test('uncertain customization is not automatically repeated and manual check keeps its ID', async ({page}) => {
  await fixture(page);const writes=[];
  await page.route('**/api/customize/start', async route => {writes.push(route.request().postDataJSON());await route.abort();});
  await page.evaluate(()=>$('#customizeWorkspace').click());await page.locator('#customizeStart').click();
  await expect(page.locator('#customizeStatus')).toContainText('nothing was retried');
  expect(writes).toHaveLength(1);
  await expect(page.locator('#customizeStart')).toHaveText('Check previous request');
  await page.locator('#customizeStart').click();
  await expect.poll(()=>writes.length).toBe(2);expect(writes[0]).toEqual(writes[1]);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('listening survives redraws, seeks, and releases media when changing agents', async ({page}) => {
  await fixture(page);
  // Forty seconds of silent PCM exercises actual browser media/Range behavior.
  const wav = Buffer.alloc(44 + 8000 * 2 * 40);
  wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24); wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
  wav.write('data', 36); wav.writeUInt32LE(wav.length - 44, 40);
  let release, started;
  const held = new Promise(resolve => release = resolve);
  const requested = new Promise(resolve => started = resolve);
  await page.route('**/api/room/**/speak', async route => {
    started(); await held;
    await route.fulfill({json: {audio_url: '/api/audio/fixture.wav', complete: true}});
  });
  await page.route('**/api/audio/fixture.wav', async route => {
    await route.fulfill({body: wav, contentType: 'audio/wav', headers: {'Accept-Ranges': 'bytes'}});
  });
  await page.evaluate(() => { state.voice.enabled = true; renderStream(); });
  await page.getByRole('button', {name: 'Listen to response · local voice'}).click();
  await requested;
  await expect(page.getByText('Preparing audio…')).toBeVisible();
  await page.evaluate(() => renderStream());
  await expect(page.getByText('Preparing audio…')).toBeVisible();
  release();
  const seek = page.getByRole('slider', {name: 'Audio position'});
  await expect(seek).toBeEnabled();
  await page.evaluate(() => {
    const audio = document.querySelector('audio'); audio.pause(); audio.currentTime = 15;
    window.playingElement = audio; renderStream();
  });
  expect(await page.evaluate(() => document.querySelector('audio') === window.playingElement)).toBe(true);
  await page.getByRole('button', {name: 'Forward 10 seconds'}).click();
  expect(await page.evaluate(() => document.querySelector('audio').currentTime)).toBeCloseTo(25, 0);
  await page.getByRole('combobox', {name: 'Playback speed'}).selectOption('1.5');
  expect(await page.evaluate(() => document.querySelector('audio').playbackRate)).toBe(1.5);
  await page.screenshot({path: test.info().outputPath('response-listening.png')});
  await page.evaluate(() => resetAgentState());
  await expect(page.locator('audio')).toHaveCount(0);
  expect(await page.evaluate(() => state.audio.size)).toBe(0);
});

test('earlier chapter notice follows unambiguous replacements without moving drafts', async ({page}) => {
  await fixture(page);
  await page.setViewportSize({width: 390, height: 844});
  await page.evaluate(() => {
    state.desk.raw = {chapters: [
      {agent: agentId(), previous_room: state.room, room: 'fixture/new', mode: 'replace'},
      {agent: agentId(), previous_room: 'fixture/new', room: 'fixture/newest', mode: 'replace'},
      {agent: agentId(), previous_room: state.room, room: 'fixture/branch', mode: 'branch'},
    ]}; renderChapterNotice();
  });
  await expect(page.locator('#chapterNotice')).toContainText('Earlier chapter');
  await expect(page.getByRole('button', {name: 'Open current chapter'})).toBeVisible();
  expect(await page.evaluate(() => currentChapter(agentId(), state.room))).toBe('fixture/newest');
  await expect(page.locator('#draft')).toHaveValue('unsent words');
  expect(await page.evaluate(() => state.room)).toBe('fixture/alpha');
  await page.evaluate(() => {
    state.desk.raw.chapters.push({agent: agentId(), previous_room: state.room, room: 'fixture/ambiguous', mode: 'replace'});
    renderChapterNotice();
  });
  await expect(page.locator('#chapterNotice')).toBeHidden();
});


test('public UI preserves the configured local identity and its existing draft keys', async ({page}) => {
  await fixture(page);
  await page.route('**/api/agents', route => route.fulfill({json: {
    default: 'owner-agent', agents: [{id: 'owner-agent', kind: 'local', label: 'My agent'}]}}));
  await page.evaluate(() => loadAgents());
  expect(await page.evaluate(() => agentId())).toBe('owner-agent');
  expect(await page.evaluate(() => apiUrl('/api/bootstrap'))).toBe('/api/bootstrap');
  expect(await page.evaluate(() => agentKey('atlas.room'))).toBe('atlas.room');
  await expect(page.locator('#draft')).toHaveValue('unsent words');
  // A later catalog update cannot reinterpret the selected identity in flight.
  await page.route('**/api/agents', route => route.fulfill({json: {
    default: 'replacement', agents: [{id: 'replacement', kind: 'local'}]}}));
  await page.evaluate(() => loadAgents());
  expect(await page.evaluate(() => agentId())).toBe('owner-agent');
});

async function desktopFixture(page) {
  await fixture(page);
  const tab = room => ({agent:'local',room,project:'fixture',title:room,controllable:true});
  const layout = rooms => ({version:1,tabs:rooms.map(tab),active:rooms.length?{agent:'local',room:rooms[0]}:null,customizations:{version:1}});
  let envelope = {version:1,state:{schema_version:1,aliases:[],desktops:[],notes:[{id:'keep-note',text:'keep'}],liveDesktops:[
    {id:'default',name:'Desktop 1',layout:layout(['fixture/old'])},
    {id:'second',name:'Desktop 2',layout:layout(['fixture/two'])},
    {id:'third',name:'Desktop 3',layout:layout(['fixture/three'])},
    {id:'fourth',name:'Accidental fourth',layout:layout(['fixture/alpha','fixture/beta'])},
  ]}};
  const writes=[];
  await page.route('**/api/desktop-state', async route => {
    if (route.request().method()==='PUT') {
      const payload=route.request().postDataJSON();writes.push(payload);
      if (payload.base_version!==envelope.version) return route.fulfill({status:409,json:{error:'desktop_conflict',detail:envelope}});
      envelope={version:envelope.version+1,state:payload.state};
    }
    await route.fulfill({json:envelope});
  });
  await page.evaluate(tabs=>{
    state.device='fixture-browser';state.agents=[{id:'local',kind:'local',label:'Local'}];
    state.tabs=tabs;state.draft={body:'unsent words',version:1};
    selectLiveDesktop('fourth');
    openDesktopDialog();
  },[tab('fixture/alpha'),tab('fixture/beta')]);
  await expect(page.getByRole('radio',{name:'Accidental fourth',exact:true})).toBeVisible();
  await page.locator('#deskManageToggle').click();
  return {read:()=>envelope,writes};
}

test('desktop footer saves edited names and keeps them after reopening', async ({page}) => {
  const f=await desktopFixture(page);
  await page.getByRole('textbox',{name:'Rename Desktop 1',exact:true}).fill('Main work');
  await page.getByRole('textbox',{name:'Rename Desktop 2',exact:true}).fill('Reading');
  // Device presence refreshes must not discard the edits awaiting Save.
  await page.evaluate(()=>{desktopChooser.manageRendered=false;renderDesktopDialog();});
  await expect(page.getByRole('textbox',{name:'Rename Desktop 1',exact:true})).toHaveValue('Main work');
  await page.locator('#deskApply').click();
  await expect(page.locator('#deskDialog')).not.toBeVisible();
  expect(f.read().state.liveDesktops.slice(0,2).map(d=>d.name)).toEqual(['Main work','Reading']);
  await page.evaluate(()=>openDesktopDialog());
  await expect(page.getByRole('radio',{name:'Main work',exact:true})).toBeVisible();
  await expect(page.getByRole('radio',{name:'Reading',exact:true})).toBeVisible();
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('save current tabs, clear, undo and remove affect arrangements only', async ({page}) => {
  const f=await desktopFixture(page);
  const nativeMutations=[];
  page.on('request',r=>{if(r.method()!=='GET' && /\/(release|interrupt|submit|attach|continue|refresh)$/.test(new URL(r.url()).pathname))nativeMutations.push(r.url());});
  await page.getByRole('button',{name:'Save current tabs here · Desktop 1',exact:true}).click();
  await expect(page.locator('#deskChoiceNote')).toContainText('Current tabs saved');
  expect(f.read().state.liveDesktops[0].layout.tabs.map(t=>t.room)).toEqual(['fixture/alpha','fixture/beta']);
  for(const name of ['Desktop 2','Desktop 3']) {
    await page.getByRole('button',{name:'Clear desktop · '+name,exact:true}).click();
    await expect(page.locator('#deskChoiceNote')).toContainText('Desktop cleared');
  }
  expect(f.read().state.liveDesktops.slice(1,3).map(d=>d.layout.tabs.length)).toEqual([0,0]);
  await page.getByRole('button',{name:'Undo layout change · Desktop 2',exact:true}).click();
  await expect(page.locator('#deskChoiceNote')).toContainText('Desktop restored');
  expect(f.read().state.liveDesktops[1].layout.tabs[0].room).toBe('fixture/two');
  await page.getByRole('button',{name:'Clear desktop · Desktop 2',exact:true}).click();
  await expect(page.locator('#deskChoiceNote')).toContainText('Desktop cleared');
  await page.getByRole('button',{name:'Remove desktop · Accidental fourth',exact:true}).click();
  await expect.poll(()=>f.read().state.liveDesktops[3].removed).toBe(true);
  await expect.poll(()=>page.evaluate(()=>desktopChooser.busy)).toBe(false);
  expect(f.read().state.liveDesktops[3].removed).toBe(true);
  expect(f.read().state.liveDesktops[3].layout.tabs).toHaveLength(2);
  expect(await page.evaluate(()=>selectedLiveDesktopId())).toBe('default');
  expect(f.read().state.notes).toEqual([{id:'keep-note',text:'keep'}]);
  expect(nativeMutations).toEqual([]);
  await page.screenshot({path:test.info().outputPath('desktop-management.png')});
});

test('removed desktops and cleared generations refuse delayed old layout writes', async ({page}) => {
  const f=await desktopFixture(page);
  await page.evaluate(()=>{
    shared.ops=[{desktopId:'second',layoutGeneration:0,kind:'tabs',base:[],tabs:[{agent:'local',room:'fixture/stale'}]}];
  });
  await page.getByRole('button',{name:'Clear desktop · Desktop 2',exact:true}).click();
  await expect(page.locator('#deskChoiceNote')).toContainText('Desktop cleared');
  await page.evaluate(async()=>{
    shared.ops=[{desktopId:'second',layoutGeneration:0,kind:'tabs',base:[],tabs:[{agent:'local',room:'fixture/stale'}]}];
    await flushLayoutOps();
  });
  expect(f.read().state.liveDesktops[1].layout.tabs).toEqual([]);
  await page.getByRole('button',{name:'Remove desktop · Desktop 3',exact:true}).click();
  await expect.poll(()=>f.read().state.liveDesktops[2].removed).toBe(true);
  await expect.poll(()=>page.evaluate(()=>desktopChooser.busy)).toBe(false);
  await page.evaluate(async()=>{
    shared.ops=[{desktopId:'third',layoutGeneration:0,kind:'active',agent:'local',room:'fixture/stale'}];
    await flushLayoutOps();
  });
  expect(f.read().state.liveDesktops[2].removed).toBe(true);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

test('failed name saves and unresolved drafts keep the desktop and input in place', async ({page}) => {
  const f=await desktopFixture(page);
  await page.getByRole('textbox',{name:'Rename Desktop 1',exact:true}).fill('My work');
  await page.route('**/api/desktop-state', route => route.request().method()==='PUT'
    ? route.fulfill({status:503,json:{error:'unavailable',message:'Workspace unavailable'}})
    : route.fulfill({json:f.read()}));
  await page.locator('#deskApply').click();
  await expect(page.locator('#deskApply')).toBeEnabled();
  await expect(page.locator('#deskDialog')).toBeVisible();
  await expect(page.getByRole('textbox',{name:'Rename Desktop 1',exact:true})).toHaveValue('My work');
  expect(f.read().state.liveDesktops[0].name).toBe('Desktop 1');
  await page.evaluate(()=>{desktopChooser.edits.clear();state.conflict={room:state.room,theirs:{body:'other draft',version:2}};});
  await page.getByRole('button',{name:'Clear desktop · Accidental fourth',exact:true}).click();
  await expect(page.locator('#deskChoiceNote')).toContainText('Your draft is kept here');
  expect(f.read().state.liveDesktops[3].layout.tabs).toHaveLength(2);
  await expect(page.locator('#draft')).toHaveValue('unsent words');
});

async function commandQueueFixture(page) {
  await fixture(page);
  let target=detail('fixture/alpha',{native:{thread_id:'native-alpha',active_turn:'busy-turn'}});
  const writes=[];
  await page.route('**/api/room/fixture/alpha',route=>route.fulfill({json:target}));
  await page.route('**/api/room/fixture/alpha/command',route=>{
    writes.push(route.request().postDataJSON());
    return route.fulfill({json:{command:{name:'goal',state:'updated',message:'Goal resumed.'}}});
  });
  await page.evaluate(d=>{state.detail=d;state.csrf='fixture-csrf';renderTarget();},target);
  return {writes,setTarget:d=>{target=d;},idle:()=>{target={...target,native:{...target.native,active_turn:null}};}};
}

test('busy goal command waits, stays bound to its original room, and preserves a later draft',async({page})=>{
  const f=await commandQueueFixture(page);
  expect(await page.evaluate(()=>sendCommand('/goal resume',false))).toEqual({ok:true,queued:true});
  await expect(page.locator('#commandQueue')).toContainText('queued');
  await page.evaluate(()=>drainCommandQueue());expect(f.writes).toHaveLength(0);
  await seed(page,'fixture/beta');
  await page.locator('#draft').fill('new unsent beta words');
  f.idle();await page.evaluate(()=>drainCommandQueue());
  expect(f.writes).toHaveLength(1);expect(f.writes[0].thread_id).toBe('native-alpha');
  await page.evaluate(()=>drainCommandQueue());expect(f.writes).toHaveLength(1);
  await expect(page.locator('#draft')).toHaveValue('new unsent beta words');
  expect(await page.evaluate(()=>state.room)).toBe('fixture/beta');
  await expect(page.locator('#commandQueue')).toBeHidden();
  expect(await page.evaluate(()=>queuedCommands()[0].state)).toBe('complete');
  await page.screenshot({path:test.info().outputPath('queued-command-result.png')});
});

test('queued commands survive reload and can be cancelled before dispatch',async({page})=>{
  const f=await commandQueueFixture(page);
  await page.evaluate(()=>sendCommand('/model example-model',false));
  await page.reload();
  await page.evaluate(()=>{state.csrf='fixture-csrf';renderCommandQueue();});
  await expect(page.locator('#commandQueue')).toContainText('/model example-model');
  await page.getByRole('button',{name:'Cancel queued /model example-model',exact:true}).click();
  f.idle();await page.evaluate(()=>drainCommandQueue());
  expect(f.writes).toHaveLength(0);
  await expect(page.locator('#commandQueue')).toBeHidden();
  expect(await page.evaluate(()=>queuedCommands()[0].state)).toBe('cancelled');
});

test('native target changes and pending approvals prevent queued execution',async({page})=>{
  const f=await commandQueueFixture(page);
  await page.evaluate(()=>sendCommand('/compact',false));
  f.setTarget(detail('fixture/alpha',{native:{thread_id:'native-alpha',active_turn:null},approvals:[{id:'approval-1'}]}));
  await page.evaluate(()=>drainCommandQueue());expect(f.writes).toHaveLength(0);
  f.setTarget(detail('fixture/alpha',{native:{thread_id:'native-replacement',active_turn:null}}));
  await page.evaluate(()=>drainCommandQueue());expect(f.writes).toHaveLength(0);
  await expect(page.locator('#commandQueue')).toContainText('different native session');
});

test('unknown command delivery is retained after reload and never automatically replayed',async({page})=>{
  const f=await commandQueueFixture(page);let posts=0;
  await page.route('**/api/room/fixture/alpha/command',route=>{posts++;return route.abort('failed');});
  await page.evaluate(()=>sendCommand('/goal resume',false));f.idle();
  await page.evaluate(()=>drainCommandQueue());expect(posts).toBe(1);
  await page.reload();await page.evaluate(()=>{state.csrf='fixture-csrf';return drainCommandQueue();});
  expect(posts).toBe(1);
  await expect(page.locator('#commandQueue')).toContainText('result unknown');
});

test('two UX46 windows dispatch a shared queued command only once',async({page,context})=>{
  const f=await commandQueueFixture(page);
  await page.evaluate(()=>sendCommand('/effort high',false));
  const other=await context.newPage();const g=await commandQueueFixture(other);
  f.idle();g.idle();
  await Promise.all([page.evaluate(()=>drainCommandQueue()),other.evaluate(()=>drainCommandQueue())]);
  expect(f.writes.length+g.writes.length).toBe(1);
  await other.close();
});

test('queuing a composer command clears only that command and preserves FIFO order',async({page})=>{
  const f=await commandQueueFixture(page);
  await page.route('**/api/room/fixture/alpha/draft',route=>route.fulfill({json:{body:route.request().postDataJSON()?.body || '',version:1}}));
  await page.locator('#draft').fill('/model first-model');
  await page.evaluate(()=>sendCommand('/model first-model',true));
  await expect(page.locator('#draft')).toHaveValue('');
  await page.locator('#draft').fill('my next message');
  await page.evaluate(()=>sendCommand('/effort high',false));f.idle();
  await page.evaluate(()=>drainCommandQueue());
  expect(f.writes.map(w=>w.command)).toEqual(['/model first-model']);
  await page.evaluate(()=>drainCommandQueue());
  expect(f.writes.map(w=>w.command)).toEqual(['/model first-model','/effort high']);
  await expect(page.locator('#draft')).toHaveValue('my next message');
});

test('abandoned dispatch is shown as unknown and a queued new conversation opens only on request',async({page})=>{
  const f=await commandQueueFixture(page);
  await page.locator('#draft').fill('');
  await page.evaluate(()=>sendCommand('/new blank',false));
  await page.route('**/api/room/fixture/alpha/command',route=>route.fulfill({json:{command:{name:'new',state:'created',message:'Conversation created.',new_room:{id:'fixture/new'}}}}));
  f.idle();await page.evaluate(()=>drainCommandQueue());
  expect(await page.evaluate(()=>state.room)).toBe('fixture/alpha');
  await expect(page.getByRole('button',{name:'Open new conversation',exact:true})).toBeVisible();
  await page.route('**/api/room/fixture/new',route=>route.fulfill({json:detail('fixture/new')}));
  const completed=await page.evaluate(()=>queuedCommands()[0]);
  await page.getByRole('button',{name:'Open new conversation',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>state.room)).toBe('fixture/new');
  await expect(page.locator('#commandQueue')).toBeHidden();
  await page.evaluate(entry=>{saveQueuedCommand({...entry,state:'running'});},completed);
  await page.evaluate(()=>drainCommandQueue());
  await expect(page.locator('#commandQueue')).toContainText('window closed before confirming');
});


test('queue timer applies a waiting resume once and removes the card without dismissal',async({page})=>{
  const f=await commandQueueFixture(page);
  await page.evaluate(()=>sendCommand('/goal resume',false));
  await expect(page.locator('#commandQueue')).toContainText('/goal resume · queued');
  f.idle();
  await expect.poll(()=>f.writes.length,{timeout:6000}).toBe(1);
  await expect(page.locator('#commandQueue')).toBeHidden();
  await page.reload();
  await page.evaluate(()=>{state.csrf='fixture-csrf';renderCommandQueue();return drainCommandQueue();});
  expect(f.writes).toHaveLength(1);
  await expect(page.locator('#commandQueue')).toBeHidden();
});

test('queue explains each waiting condition and preserves unknown delivery notices',async({page})=>{
  const f=await commandQueueFixture(page);
  await page.evaluate(()=>sendCommand('/goal resume',false));
  await page.evaluate(()=>drainCommandQueue());
  await expect(page.locator('#commandQueue')).toContainText('still working on its current reply');
  await expect(page.locator('#commandQueue')).toContainText('Last checked less than a minute ago');
  f.setTarget(detail('fixture/alpha',{native:{thread_id:'native-alpha',active_turn:null},approvals:[{id:'a'}]}));
  await page.evaluate(()=>drainCommandQueue());
  await expect(page.locator('#commandQueue')).toContainText('Waiting for your answer or approval');
  f.setTarget(detail('fixture/alpha',{native:{thread_id:'native-alpha',active_turn:null},ownership:{state:'idle'}}));
  await page.evaluate(()=>drainCommandQueue());
  await expect(page.locator('#commandQueue')).toContainText('not connected here');
  expect(f.writes).toHaveLength(0);
  await page.route('**/api/room/fixture/alpha',route=>route.abort('failed'));
  await page.evaluate(()=>drainCommandQueue());
  await expect(page.locator('#commandQueue')).toContainText('Cannot reach this conversation');
});

test('repeated waiting commands coalesce across windows without changing ordered commands',async({page,context})=>{
  const f=await commandQueueFixture(page);
  const other=await context.newPage();await commandQueueFixture(other);
  await Promise.all([page.evaluate(()=>sendCommand('/goal resume',false)),other.evaluate(()=>sendCommand('/goal resume',false))]);
  expect(await page.evaluate(()=>queuedCommands().filter(e=>e.state==='pending').length)).toBe(1);
  await page.evaluate(()=>sendCommand('/goal clear',false));
  await page.evaluate(()=>sendCommand('/goal resume',false));
  expect(await page.evaluate(()=>queuedCommands().map(e=>e.command))).toEqual(['/goal resume','/goal clear','/goal resume']);
  expect(f.writes).toHaveLength(0);
  await other.close();
});

test('a blocked goal and a working or finished reply are shown separately',async({page})=>{
  await commandQueueFixture(page);
  await page.evaluate(()=>{
    state.detail.goal_status={state:'known',goal:{status:'blocked',objective:'Finish the fixture'}};
    renderGoalBadge();
    state.goalPanelTarget={agent:agentId(),room:state.room};
    state.goalPanelStatus=state.detail.goal_status;
    state.goalPanel=state.goalPanelStatus.goal;
    state.commandMode='goal';renderCommands();
  });
  await expect(page.locator('#btnGoal')).toHaveText('Goal needs help');
  await expect(page.locator('#commandOptions')).toContainText('Automatic continuation is stopped');
  await expect(page.locator('#commandOptions')).toContainText('This turn: still working on a reply');
  await expect(page.locator('#btnGoalResume')).toHaveText('Resume after this reply');
  await page.evaluate(()=>{state.detail.native.active_turn=null;renderCommands();});
  await expect(page.locator('#commandOptions')).toContainText('This turn: finished. No reply is running.');
  await expect(page.locator('#btnGoal')).toHaveText('Goal needs help');
});


test('waiting commands use compact rows with optional details on desktop and phone',async({page})=>{
  await commandQueueFixture(page);
  await page.evaluate(()=>{state.detail.project_name='GlucaPet';return sendCommand('/goal resume',false);});
  await page.evaluate(()=>sendCommand('/effort high',false));
  await expect(page.locator('#commandQueue summary').first()).toHaveText('GlucaPet · /goal resume · queued');
  await expect(page.locator('#commandQueue details').first()).not.toHaveAttribute('open','');
  expect((await page.locator('#commandQueue').boundingBox()).height).toBeLessThan(100);
  await page.screenshot({path:test.info().outputPath('compact-queue-desktop.png')});
  await page.locator('#commandQueue summary').first().click();
  await page.evaluate(()=>drainCommandQueue());
  await expect(page.locator('#commandQueue details').first()).toHaveAttribute('open','');
  await expect(page.locator('#commandQueue .command-queue-note').first()).toBeVisible();
  await page.locator('#commandQueue summary').first().click();
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>__atlas.applyShell());
  await page.screenshot({path:test.info().outputPath('compact-queue-phone.png')});
  const box=await page.locator('#commandQueue').boundingBox();
  expect(box.width).toBeGreaterThan(300);expect(box.height).toBeLessThan(130);expect(box.x+box.width).toBeLessThanOrEqual(390);
});

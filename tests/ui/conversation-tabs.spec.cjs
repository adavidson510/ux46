const {test, expect} = require('@playwright/test');
const fs = require('node:fs'), path = require('node:path');
const root = process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname, '../..');
async function fixture(page, width = 1440) {
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
  await page.evaluate(() => {
    state.agents = [{id:'local', label:'Local'}, {id:'remote', label:'Remote'}]; state.agent = 'local'; state.room = 'project/room-3';
    state.tabs = Array.from({length:16}, (_,i) => ({agent:i === 12 ? 'remote' : 'local', room:'project/room-'+i, project:'Example project', title:'Conversation '+i+' with a long descriptive name', customLabel:i === 3 ? 'Current workspace' : '', controllable:true}));
    state.detail = {controllable:true,native:{}}; state.connKind='live'; state.roomRefreshing=false; state.eventRecovery=false;
    state.freshness={room:Date.now(),history:Date.now(),roomError:'',historyError:''};
    state.ui.left='wide'; state.ui.dock='closed'; applyShell();
    document.querySelector('#draft').value='Preserve this unsent message';
    window.chosen=[];
    openSession = async (agent,room) => { window.chosen.push({agent,room}); state.agent=agent; state.room=room; renderTabs(); return true; };
    renderTabs();
  });
}

test('crowded strip retains usable icons, selected controls and focus without shifting on hover', async ({page}) => {
  await fixture(page);
  const inactive = page.locator('#tabs .tabwrap:not(.on)').first(), selected = page.locator('#tabs .tabwrap.on');
  expect((await inactive.boundingBox()).width).toBeGreaterThanOrEqual(42);
  expect((await selected.boundingBox()).width).toBe(260);
  await expect(selected.locator('.tname')).toHaveText('Current workspace');
  await expect(selected.locator('.tmenu')).toBeVisible(); await expect(selected.locator('.tclose')).toBeVisible();
  const before = await inactive.boundingBox(); await inactive.hover();
  expect((await inactive.boundingBox()).width).toBe(before.width);
  await expect(inactive.locator('.tclose')).toBeHidden(); await expect(inactive.locator('.tmenu')).toBeHidden();
  await expect(page.locator('#tabs .tab-signal')).toHaveCount(0); // controllable does not mean working
  await selected.locator('.tab').focus();
  await page.evaluate(() => {window.focusedTab=document.activeElement;renderTabs();});
  expect(await page.evaluate(() => focusedTab===document.activeElement)).toBe(true);
  await page.screenshot({path:test.info().outputPath('compact-tabs.png')});
});

test('picker finds aliases, canonical names and owners, selects the exact target and keeps search during refresh', async ({page}) => {
  await fixture(page);
  await page.getByRole('button',{name:'All conversations (16)',exact:true}).click();
  const search=page.getByRole('searchbox',{name:'Search open conversations'});
  await expect(search).toBeFocused(); await search.fill('remote');
  await expect(page.locator('.conversation-choice')).toHaveCount(1);
  await page.evaluate(() => renderTabs()); await expect(search).toHaveValue('remote'); await expect(search).toBeFocused();
  await search.press('Enter'); await expect(page.locator('#conversationPicker')).not.toBeVisible();
  expect(await page.evaluate(()=>chosen)).toEqual([{agent:'remote',room:'project/room-12'}]);
  await expect(page.locator('#draft')).toBeFocused();
  await expect(page.locator('#draft')).toHaveValue('Preserve this unsent message');
  await page.getByRole('button',{name:'All conversations (16)',exact:true}).click();
  await search.fill('Conversation 3'); await expect(page.locator('.conversation-choice')).toHaveCount(1);
  await expect(page.locator('.conversation-copy')).toContainText('Current workspace');
  await search.fill('no match here'); await expect(page.locator('#conversationMatches')).toHaveText('No matching open conversations.');
  await search.press('Escape'); await expect(page.getByRole('button',{name:'All conversations (16)',exact:true})).toBeFocused();
});

test('phone picker provides readable names and separate options/close controls without sending a prompt', async ({page}) => {
  await fixture(page,390);
  await expect(page.locator('#mobileTabs')).toBeVisible();
  await page.getByRole('button',{name:'All conversations (16)',exact:true}).click();
  const search=page.getByRole('searchbox',{name:'Search open conversations'});await search.fill('Conversation 8');
  await page.screenshot({path:test.info().outputPath('phone-picker.png')});
  const row=page.locator('.conversation-choice');expect((await row.boundingBox()).width).toBeLessThan(390);
  await page.getByRole('button',{name:'Options for Conversation 8 with a long descriptive name on Local',exact:true}).click();
  await expect(page.locator('#conversationPicker')).not.toBeVisible();
  await expect(page.getByRole('textbox',{name:'Display name for this session'})).toBeVisible();
  await page.keyboard.press('Escape');
  await page.evaluate(()=>{state.tabs[8].controllable=false;renderTabs();});
  await page.getByRole('button',{name:'All conversations (16)',exact:true}).click();await search.fill('Conversation 8');
  await page.getByRole('button',{name:'Close Conversation 8 with a long descriptive name on Local',exact:true}).click();
  await expect(page.locator('#conversationMatches')).toHaveText('No matching open conversations.');
  expect(await page.evaluate(()=>chosen)).toEqual([]);
});

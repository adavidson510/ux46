const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname, '../..');

// A deep-link or layout room id is spliced into API paths. A malformed one
// ("../x", "p/s?x") must never move a request outside /api/room/<seg>/<seg>.
// Synthetic fixture only: every API answers with one placeholder room and all
// external requests are refused.
const seen = [];
async function fixture(page, search, {boot = false} = {}) {
  seen.length = 0;
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'https://fixture.test') return route.abort();
    if (url.pathname.startsWith('/api/')) {
      seen.push(url.pathname + url.search);
      return route.fulfill({json: {id: 'p/s', title: 'Fixture', draft: {body: '', version: 0},
        native: {}, ownership: {}, items: [], complete: true}});
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    const file = path.join(root, 'app/console', name);
    if (!fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
    let body = fs.readFileSync(file, 'utf8');
    if (name === 'index.html') body = body.replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g, '');
    if (name === 'app.js' && !boot) body = body.replace('\nboot();', '\n/* boot controlled by synthetic journey */');
    const contentType = name.endsWith('.js') ? 'application/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html';
    await route.fulfill({body, contentType});
  });
  await page.goto('https://fixture.test/' + search);
  await page.waitForFunction(() => Boolean(window.__atlas));
}
// Only the room's own two segments (and a known sub-resource) are allowed.
const roomRequests = () => seen.filter(p => p.includes('/api/room/') || p.includes('/agents/'));
const confined = p => /^\/api\/room\/[^/?#]+\/[^/?#]+(\/[a-z-]+)?(\?|$)/.test(p);

test('unbound native approvals explain where to answer without offering unsafe controls', async ({page}) => {
  await fixture(page, '');
  await page.evaluate(() => document.querySelector('#stream').append(approvalNode({
    key:'hermes-request',kind:'command',params:{command:'example'},answer_supported:false,
    answer_notice:'Answer in Hermes. This request cannot be safely approved from UX46 yet.'
  })));
  await expect(page.locator('[data-approval="hermes-request"]')).toContainText('Answer in Hermes');
  await expect(page.getByRole('button',{name:'Accept once',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Decline',exact:true})).toHaveCount(0);
});

const bad = ['../agents/remote1/api/room/p/s', '../x', 'p/s?x', 'p/s#x', 'p/..', './s', 'p\\..\\x/s', 'p/s/t'];

for (const room of bad) {
  test(`selectRoom refuses malformed room ${JSON.stringify(room)}`, async ({page}) => {
    await fixture(page, '');
    const result = await page.evaluate(async r => {
      state.agents = [{id: 'local', label: 'Local'}];
      try { return await selectRoom(r, {toTail: true}); } catch (e) { return 'threw'; }
    }, room);
    await page.waitForTimeout(200);
    expect(result).toBe(false);
    expect(await page.evaluate(() => state.room)).not.toBe(room);
    expect(roomRequests()).toEqual([]);
  });
}

for (const room of ['../x', 'p/s?x', '../agents/remote1/api/room/p/s']) {
  test(`deep link ?room=${room} stays inside /api/room/<seg>/<seg>`, async ({page}) => {
    await fixture(page, '?room=' + encodeURIComponent(room), {boot: true});
    await page.waitForTimeout(800);
    expect(await page.evaluate(() => state.room)).not.toBe(room);
    for (const p of roomRequests()) expect(confined(p), p).toBe(true);
    expect(seen.some(p => p.startsWith('/api/agents/'))).toBe(false);
  });
}

test('room API paths encode each segment; valid ids are unchanged', async ({page}) => {
  await fixture(page, '');
  const paths = await page.evaluate(() => [
    roomApiPath('fixture/alpha-1.2_x'), roomApiPath('p/s?x'), roomPathSegments('a#b/c'),
    validRoomId('fixture/alpha'), validRoomId('a.b/c..d'), validRoomId('../x'), validRoomId('p/s?x'),
    MARK_AGENT_ID.test('remote1'), MARK_AGENT_ID.test('../x'), MARK_AGENT_ID.test('Remote.1'),
  ]);
  expect(paths).toEqual(['/api/room/fixture/alpha-1.2_x', '/api/room/p/s%3Fx', 'a%23b/c',
    true, true, false, false, true, false, false]);
});

test('a valid deep link still opens its room', async ({page}) => {
  await fixture(page, '?room=' + encodeURIComponent('p/s'), {boot: true});
  await page.waitForFunction(() => state.room === 'p/s', null, {timeout: 5000});
  expect(seen).toContain('/api/room/p/s');
});

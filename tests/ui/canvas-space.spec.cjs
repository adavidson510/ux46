const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.UX46_TEST_UI_ROOT || path.resolve(__dirname, '../..');
// The full browser includes its PDF viewer; headless-shell does not.
test.use({channel: 'chromium'});

async function fixture(page) {
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (['chrome-extension:', 'chrome:'].includes(url.protocol)) return route.continue();
    if (url.origin !== 'https://fixture.test') return route.abort();
    if (url.pathname.startsWith('/api/')) return route.fulfill({json: {}});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    const file = path.join(root, 'app/console', name);
    if (!fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
    let body = fs.readFileSync(file, 'utf8');
    if (name === 'index.html') body = body.replace(/<script src="\/(?!app.js)[^"]+"><\/script>/g, '');
    if (name === 'app.js') body = body.replace('\nboot();', '\n/* synthetic fixture controls boot */');
    await route.fulfill({body, headers: name === 'index.html' ? {'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-src 'self' blob:; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"} : {}, contentType: name.endsWith('.js') ? 'application/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
  });
  await page.goto('https://fixture.test/');
  await page.waitForFunction(() => Boolean(window.__atlas));
  await seed(page);
}
async function seed(page) {
  await page.evaluate(() => {
    Object.assign(state, {room: 'example/research', detail: {id: 'example/research', project_id: 'example', session: 'research'}});
    Object.assign(state.ui, {left: 'wide', dock: 'board', overlay: isWide() ? null : 'dock', focus: false});
    state.board.data = {title: 'Research · illustrative data', reporter: 'Example', sections: [
      {title: 'Now · prepared, not running', items: [{label: 'Monday eight-branch comparison is prepared for review', value: '14 prior sessions · rules unchanged', state: 'info', detail: 'Fixture evidence, not real trading results.'}]},
      {title: 'Recent results · synthetic USD example', chart: {type: 'bar', labels: ['First comparison after fees', 'Second comparison after fees'], values: [-107.8, 42.1], unit: 'USD'}, items: []},
      {title: 'Next', items: [{label: 'Compare the new forecast with the earlier observations', value: 'Monday 11:00 / 11:30 am PT', state: 'todo'}]}
    ]};
    document.querySelector('#draft').value = 'Keep my unfinished message';
    state.board.key = boardContext().key; applyShell(); renderBoardPanel();
  });
}

test('drag, keyboard, persistent width and temporary expansion preserve the same canvas', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await fixture(page);
  const dock = page.locator('#dock'), handle = page.getByRole('separator', {name: 'Panel width'});
  const normal = (await dock.boundingBox()).width;
  const edge = await handle.boundingBox();
  await page.mouse.move(edge.x + 5, edge.y + 200);
  await page.mouse.down(); await page.mouse.move(edge.x - 175, edge.y + 200); await page.mouse.up();
  expect((await dock.boundingBox()).width).toBeCloseTo(normal + 180, 0);
  await handle.press('ArrowLeft');
  const wide = (await dock.boundingBox()).width;
  expect(wide).toBeCloseTo(normal + 200, 0);
  await page.locator('.board-item summary').first().click();
  await page.evaluate(() => { window.savedBoardNode = document.querySelector('#boardBody').firstChild; });
  await page.getByRole('button', {name: 'Expand panel', exact: true}).click();
  expect((await dock.boundingBox()).width).toBe(1440);
  await expect(dock).toHaveAttribute('aria-modal', 'true');
  expect(await page.locator('#side').evaluate(n => n.inert)).toBe(true);
  await page.screenshot({path: test.info().outputPath('canvas-expanded.png')});
  await page.keyboard.press('Escape');
  await expect(dock).toBeVisible();
  expect((await dock.boundingBox()).width).toBe(wide);
  expect(await page.evaluate(() => savedBoardNode === document.querySelector('#boardBody').firstChild)).toBe(true);
  await expect(page.locator('.board-item').first()).toHaveAttribute('open', '');
  await expect(page.locator('#draft')).toHaveValue('Keep my unfinished message');
  expect(await page.locator('#side').evaluate(n => n.inert)).toBe(false);
  await page.reload(); await page.waitForFunction(() => Boolean(window.__atlas)); await seed(page);
  expect((await dock.boundingBox()).width).toBe(wide);
  await page.getByRole('button', {name: 'Reset panel width'}).click();
  expect((await dock.boundingBox()).width).toBe(normal);
  expect(await page.evaluate(() => localStorage.getItem('atlas.dockWidth'))).toBe(null);
});

for (const width of [390, 1280]) test(`canvas reads without clipped labels or hidden values at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900}); await fixture(page);
  const label = page.locator('.board-label').first();
  expect(await label.evaluate(n => getComputedStyle(n).whiteSpace)).toBe('normal');
  expect(await label.evaluate(n => n.scrollWidth <= n.clientWidth)).toBe(true);
  await expect(page.locator('.canvas-chart-key')).toContainText('First comparison after fees');
  await expect(page.locator('.canvas-chart-key')).toContainText('-107.8 USD');
  expect(await page.locator('#boardBody').evaluate(n => n.scrollWidth <= n.clientWidth)).toBe(true);
  await page.screenshot({path: test.info().outputPath('canvas-readable.png')});
  const before = await page.locator('#dock').boundingBox();
  await page.getByRole('button', {name: 'Expand panel', exact: true}).click();
  expect((await page.locator('#dock').boundingBox()).width).toBe(width);
  await page.getByRole('button', {name: 'Restore panel size'}).click();
  expect((await page.locator('#dock').boundingBox()).width).toBe(before.width);
});

test('expansion keeps unsaved canvas edits; resize cancellation restores width', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900}); await fixture(page);
  await page.locator('#boardEdit').click();
  await page.getByRole('textbox', {name: 'Canvas title', exact: true}).fill('My unsaved view');
  await page.getByRole('button', {name: 'Expand panel', exact: true}).click();
  await page.getByRole('button', {name: 'Restore panel size'}).click();
  await expect(page.getByRole('textbox', {name: 'Canvas title', exact: true})).toHaveValue('My unsaved view');
  const dock = page.locator('#dock'), before = (await dock.boundingBox()).width;
  const edge = await page.locator('#dockResize').boundingBox();
  await page.mouse.move(edge.x + 5, edge.y + 200); await page.mouse.down();
  await page.mouse.move(edge.x - 100, edge.y + 200); await page.keyboard.press('Escape'); await page.mouse.up();
  expect((await dock.boundingBox()).width).toBe(before);
  await expect(dock).toBeVisible();
});

test('one History searches both by default, retains query across filters and ignores late results', async ({page}) => {
  await fixture(page);
  await page.evaluate(() => { state.detail.controllable = true; });
  const reads = [];
  await page.route('**/search?**', route => {
    const url = new URL(route.request().url()); reads.push([url.searchParams.get('kinds'), url.searchParams.get('q')]);
    return route.fulfill({json: {hits: [{item_id: 'example', snippet: url.searchParams.get('kinds') + ': ' + url.searchParams.get('q')}], source: 'thread_read', total: 1, searched_items: 5}});
  });
  await page.getByRole('tab', {name: 'History', exact: true}).click();
  await expect(page.locator('#navList')).toContainText('human,final:');
  await page.getByRole('searchbox', {name: 'Search this session'}).fill('release');
  await expect(page.locator('#navList')).toContainText('human,final: release');
  await page.getByLabel('History filter').selectOption('updates');
  await expect(page.locator('#navList')).toContainText('final: release');
  await expect(page.locator('#navSearch')).toHaveValue('release');
  await page.getByLabel('History filter').selectOption('turns');
  await expect(page.locator('#navList')).toContainText('human: release');
  expect(reads).toContainEqual(['human,final', 'release']);
  await expect(page.getByRole('tab', {name: 'Turns', exact: true})).toHaveCount(0);
  let release;
  const held = new Promise(resolve => release = resolve);
  await page.route('**/search?**', async route => {
    const old = new URL(route.request().url()).searchParams.get('q') === 'old';
    if (old) await held;
    await route.fulfill({json: {hits: old ? [{item_id: 'old', snippet: 'obsolete result'}] : [], source: 'thread_read', total: 0, searched_items: 5}});
  });
  await page.evaluate(() => { document.querySelector('#navSearch').value = 'old'; window.oldSearch = runNavSearch('old'); });
  await page.evaluate(async () => { document.querySelector('#navSearch').value = 'new'; await runNavSearch('new'); });
  release(); await page.evaluate(() => oldSearch);
  await expect(page.locator('#navList')).toHaveText('No match.');
});

const previewText = entry => 'Here is the result.\n\n```ux46-preview\n' + JSON.stringify(entry) + '\n```';

test('new final opens a turn preview once; Back retains overview and room switch clears it', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900}); await fixture(page);
  const text = previewText({type: 'chart', title: 'This turn comparison', chart: {type: 'bar', labels: ['A', 'B'], values: [3, 5], unit: 'examples'}});
  await page.evaluate(() => {
    state.detail.controllable = true; state.following = true; state.sel = null; state.anchor = null;
    state.items = [{id: 'earlier', type: 'agentMessage', phase: 'final_answer', text: 'Earlier reply'},
      {id: 'new-final', type: 'agentMessage', phase: 'final_answer', text: '```ux46-preview\n{"type":'}];
    state.ids = new Set(['earlier', 'new-final']); state.tail = state.items.slice();
    window.roomBoard = document.querySelector('#boardBody').firstChild;
  });
  await page.route('**/history?**', route => route.fulfill({json: {items: [{id: 'new-final', type: 'agentMessage', phase: 'final_answer', text}, {id: 'earlier', type: 'agentMessage', phase: 'final_answer', text: 'Earlier reply'}], complete: true}}));
  await page.evaluate(() => refreshTail());
  await expect(page.locator('#panelPreview')).toBeVisible();
  await expect(page.locator('#previewBody')).toContainText('5 examples');
  await page.getByRole('button', {name: 'Back to room Canvas'}).click();
  expect(await page.evaluate(() => roomBoard === document.querySelector('#boardBody').firstChild)).toBe(true);
  await expect(page.locator('#panelBoard')).toBeVisible();
  await page.evaluate(() => refreshTail());
  await expect(page.locator('#panelPreview')).toBeHidden();
  await page.getByRole('button', {name: 'Open preview', exact: true}).click();
  await expect(page.locator('#previewOrigin')).toContainText('This turn comparison');
  await page.evaluate(() => { state.room = 'example/other'; state.detail = null; renderStream(); });
  await expect(page.locator('#previewTab')).toBeHidden();
  await expect(page.locator('#previewBody')).toBeEmpty();
  await expect(page.locator('#draft')).toHaveValue('Keep my unfinished message');
});

test('managed PDF opens a browser preview; non-PDF bytes cannot render', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900}); await fixture(page);
  // Small valid synthetic PDF, with byte-accurate offsets; no private document.
  let pdf = '%PDF-1.4\n'; const offsets = [0];
  const content = 'BT /F1 24 Tf 30 150 Td (Turn preview example) Tj ET';
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 240] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${content.length} >>\nstream\n${content}\nendstream`];
  objects.forEach((body, i) => { offsets.push(Buffer.byteLength(pdf)); pdf += `${i + 1} 0 obj\n${body}\nendobj\n`; });
  const start = Buffer.byteLength(pdf);
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(n => String(n).padStart(10, '0') + ' 00000 n \n').join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  await page.route('**/api/atlas/files/sample/download?**', route => route.fulfill({body: pdf, contentType: 'application/pdf'}));
  const entry = {type: 'pdf', title: 'Example PDF', file_id: 'sample'};
  await page.evaluate(text => { document.querySelector('#thread').append(markdownFragment(text)); }, previewText(entry));
  await page.getByRole('button', {name: 'Open preview', exact: true}).click();
  await expect(page.locator('#previewBody iframe')).toHaveAttribute('src', /^blob:https:\/\/fixture.test\//);
  await page.getByRole('button', {name: 'Expand panel', exact: true}).click();
  await expect.poll(() => page.frames().some(frame => frame.url().startsWith('chrome-extension://'))).toBe(true);
  const viewer = page.frames().find(frame => frame.url().startsWith('chrome-extension://'));
  await expect(viewer.locator('pdf-viewer')).toBeVisible();
  await expect(viewer.locator('viewer-toolbar')).toContainText('1');
  await page.screenshot({path: test.info().outputPath('pdf-preview.png')});
  await page.route('**/api/atlas/files/bad/download?**', route => route.fulfill({body: '<html><script>window.bad=1</script></html>', contentType: 'text/html'}));
  await page.evaluate(() => openTurnPreview(parseTurnPreview(JSON.stringify({type: 'pdf', title: 'Invalid PDF', file_id: 'bad'}))));
  await expect(page.locator('#previewBody')).toContainText('could not be previewed');
  await expect(page.locator('#previewBody iframe')).toHaveCount(0);
  expect(await page.evaluate(() => window.bad)).toBeUndefined();
  await page.evaluate(() => {
    document.querySelector('#thread').append(markdownFragment('```ux46-preview\n{"type":"iframe","title":"External","url":"https://outside.test"}\n```'));
  });
  await expect(page.locator('#thread iframe')).toHaveCount(0);
});

test('phone previews open explicitly and preserve unsaved room edits', async ({page}) => {
  await page.setViewportSize({width: 390, height: 900}); await fixture(page);
  await page.locator('#boardEdit').click();
  await page.getByRole('textbox', {name: 'Canvas title', exact: true}).fill('Human overview');
  await page.evaluate(text => {
    state.items = [{id: 'new', type: 'agentMessage', phase: 'final_answer', text}];
    offerTurnPreview(new Set());
    document.querySelector('#thread').append(markdownFragment(text));
  }, previewText({type: 'markdown', title: 'A reply document', text: '# The document\n\nA useful answer.'}));
  await expect(page.locator('#panelPreview')).toBeHidden();
  await page.evaluate(() => document.querySelector('.preview-card button').click());
  await expect(page.locator('#previewBody')).toContainText('A useful answer.');
  await page.getByRole('button', {name: 'Back to room Canvas'}).click();
  await expect(page.getByRole('textbox', {name: 'Canvas title', exact: true})).toHaveValue('Human overview');
});

test('late PDF bytes cannot cross rooms or replace a newer preview', async ({page}) => {
  await fixture(page);
  await page.evaluate(async () => {
    const original = window.fetch;
    window.fetch = (url, options) => String(url).includes('/files/slow/download')
      ? new Promise(resolve => { window.finishOldPdf = () => resolve(new Response('%PDF-fixture', {status: 200})); })
      : original(url, options);
    window.oldPreview = openTurnPreview(parseTurnPreview(JSON.stringify({type: 'pdf', title: 'Old document', file_id: 'slow'})));
    await openTurnPreview(parseTurnPreview(JSON.stringify({type: 'markdown', title: 'Chosen document', text: 'Keep this preview'})));
    finishOldPdf(); await oldPreview;
  });
  await expect(page.locator('#previewBody')).toHaveText('Keep this preview');
  await expect(page.locator('#previewBody iframe')).toHaveCount(0);
  await page.evaluate(async () => {
    window.oldPreview = openTurnPreview(parseTurnPreview(JSON.stringify({type: 'pdf', title: 'Old room document', file_id: 'slow'})));
    state.room = 'example/other'; state.detail = null; renderStream();
    finishOldPdf(); await oldPreview;
  });
  await expect(page.locator('#previewBody')).toBeEmpty();
  await expect(page.locator('#previewTab')).toBeHidden();
});

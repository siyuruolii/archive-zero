const { _electron: electron, expect } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '..', '..');
  const directory = fs.mkdtempSync(path.join(root, 'artifacts', 'source-history-desktop-'));
  let includeNew = false;
  const server = http.createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'application/rss+xml' });
    const item = (id, title, date) => `<item><guid>${id}</guid><title>${title}</title><link>https://example.test/${id}</link>${date ? `<pubDate>${date}</pubDate>` : ''}</item>`;
    response.end('<rss><channel>' + item('old-one', '历史文章一', 'Wed, 01 Jan 2020 00:00:00 GMT') + item('old-two', '历史文章二', 'Thu, 02 Jan 2020 00:00:00 GMT') + item('unknown', '无日期文章', null) + (includeNew ? item('new', '订阅后新文章', new Date(Date.now() + 2000).toUTCString()) : '') + '</channel></rss>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const bootstrap = path.join(directory, 'launch.cjs');
  fs.writeFileSync(bootstrap, `require('electron').app.setPath('userData', ${JSON.stringify(path.join(directory, 'profile'))}); require(${JSON.stringify(path.join(root, 'desktop/electron/main.cjs'))});`);
  let app;
  const keys = () => JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps([r[0] for r in c.execute("select item_key from materials order by item_key")]))', path.join(directory, 'reading.db')], { encoding: 'utf8' }));
  try {
    app = await electron.launch({ args: [bootstrap], env: { ...process.env, AGENT_API_KEY: '', AGENT_DATA_DIR: directory, AGENT_TEST_MODE: '1' }, timeout: 40000 });
    const page = await app.firstWindow(); await page.waitForLoadState('networkidle');
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.getByRole('button', { name: '阅读来源', exact: true }).click();
    await page.getByRole('button', { name: '添加 RSS 来源', exact: true }).click();
    await page.getByLabel('名称', { exact: true }).fill('历史选择验证');
    await page.getByLabel('RSS 地址', { exact: true }).fill(`http://127.0.0.1:${server.address().port}/feed`);
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByRole('heading', { name: '历史选择验证', exact: true }).waitFor();
    assert.deepEqual(keys(), []);
    await page.getByRole('button', { name: '选择历史文章', exact: true }).click();
    const panel = page.getByRole('region', { name: '选择历史文章', exact: true });
    await panel.getByLabel('历史文章一').waitFor();
    await expect(panel.getByRole('checkbox', { checked: true })).toHaveCount(0);
    await panel.getByLabel('历史文章一').check();
    await panel.getByRole('button', { name: '导入所选（1）', exact: true }).click();
    await panel.getByText('已导入 1 篇文章', { exact: true }).waitFor();
    await expect(panel.getByLabel('历史文章一')).toHaveCount(0);
    assert.deepEqual(keys(), ['old-one']);
    includeNew = true;
    await page.getByRole('button', { name: '获取更新', exact: true }).click();
    await page.getByText('更新完成，仅获取添加来源后发布的文章', { exact: true }).waitFor();
    assert.deepEqual(keys(), ['new', 'old-one']);
    await panel.getByRole('button', { name: '收起列表', exact: true }).click();
    await page.getByRole('button', { name: '选择历史文章', exact: true }).click();
    await panel.getByLabel('无日期文章').waitFor();
    await panel.getByLabel('无日期文章').check();
    await panel.getByRole('button', { name: '导入所选（1）', exact: true }).click();
    await expect(panel.getByLabel('无日期文章')).toHaveCount(0);
    assert.deepEqual(keys(), ['new', 'old-one', 'unknown']);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(root, 'artifacts/source-history-desktop.png'), await page.screenshot());
    fs.writeFileSync(path.join(root, 'artifacts/source-history-desktop-result.json'), JSON.stringify({ defaultUnselected: true, selectedOnlyImported: true, updatesRespectSubscriptionDate: true, unknownDateManualOnly: true, pageErrors: errors }, null, 2));
    console.log('Source history selection and date-filtered updates: passed.');
  } finally {
    if (app) await app.close();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

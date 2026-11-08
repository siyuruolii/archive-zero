const { _electron: electron, expect } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '..', '..');
  const directory = fs.mkdtempSync(path.join(root, 'artifacts', 'workspace-updates-desktop-'));
  execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', path.join(root, 'tests/desktop_fixture.py'), directory], { cwd: root });
  let calls = 0, topic, tag, materialId, deepClosed = false, releaseFinal;
  const completion = new Promise(resolve => { releaseFinal = resolve; });
  const body = '本机原文：程序校验工具权限，再执行读取。';
  const server = http.createServer(async (request, response) => {
    if (request.url === '/feed') {
      response.writeHead(200, { 'Content-Type': 'application/rss+xml' });
      response.end('<rss><channel>' + [1, 2].map(i => `<item><guid>live-${i}</guid><title>实时入库文章${i}</title><link>http://127.0.0.1:${server.address().port}/article-${i}</link><description>人工测试文章</description></item>`).join('') + '</channel></rss>');
      return;
    }
    if (request.url.startsWith('/article-')) {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(`<main>${body}</main>`); return;
    }
    for await (const _chunk of request) {}
    calls++;
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const frame = (text, finish = null) => 'data: ' + JSON.stringify({ choices: [{ index: 0, delta: { content: text }, finish_reason: finish }] }) + '\n\n';
    if (calls === 2) {
      response.on('close', () => { deepClosed = true; });
      response.write(frame('{"status":"succeeded","reason":"等待中的精读","deep_summary":"尚未完成'));
      return; // Keep the actual model HTTP response open until cancellation.
    }
    if (calls === 4) {
      response.end('data: ' + JSON.stringify({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'read-original', type: 'function', function: { name: 'material_read', arguments: JSON.stringify({ material_id: materialId, start: 0, length: 1000 }) } }] }, finish_reason: 'tool_calls' }] }) + '\n\ndata: [DONE]\n\n');
      return;
    }
    if (calls === 5) {
      response.write(frame('{"status":"succeeded","reason":"已读原文","deep_summary":"精读第一段'));
      await completion;
      if (!response.destroyed) response.end(frame('，精读第二段","coverage":' + JSON.stringify([{ material_id: materialId, start: 0, end: body.length }]) + ',"limitations":""}', 'stop') + 'data: [DONE]\n\n');
      return;
    }
    const final = { status: 'succeeded', reason: '本机测试', routes: [{ topic_id: topic, tag_ids: [tag], short_summary: '人工验证材料进入右侧列表', match_reason: '测试', is_recommended: true, recommendation_reason: '测试', needs_deep_read: calls === 1, deep_read_guideline: calls === 1 ? '读取原文' : '' }] };
    response.end(frame(JSON.stringify(final), 'stop') + 'data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const bootstrap = path.join(directory, 'launch.cjs');
  fs.writeFileSync(bootstrap, `require('electron').app.setPath('userData', ${JSON.stringify(path.join(directory, 'profile'))}); require(${JSON.stringify(path.join(root, 'desktop/electron/main.cjs'))});`);
  let app;
  try {
    app = await electron.launch({ args: [bootstrap], env: { ...process.env, AGENT_API_KEY: '', AGENT_DATA_DIR: directory, AGENT_TEST_MODE: '1' }, timeout: 40000 });
    const page = await app.firstWindow(); await page.waitForLoadState('networkidle');
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.locator('.material-row').first().waitFor();
    const state = await page.evaluate(() => window.reader.request('/api/state'));
    topic = state.topics[0].id; tag = state.tags[0].id;
    const job = await page.evaluate(async ({ port, topic }) => {
      await window.reader.request('/api/settings/models', 'PUT', { version: 1, default: 'local', profiles: { local: { model: 'local-test', base_url: `http://127.0.0.1:${port}`, api_key: 'isolated-test-key', strict_tools: false } }, stages: {} });
      const added = await window.reader.request('/api/sources', 'POST', { name: '异步测试', url: `http://127.0.0.1:${port}/feed`, topic_ids: [topic] });
      let source;
      for (let i = 0; i < 100; i++) {
        const result = await window.reader.request('/api/jobs/' + added.job_id);
        if (result.status === 'completed') { source = result.result; break; }
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      if (!source) throw new Error('source creation did not complete');
      return window.reader.request(`/api/sources/${source.id}/history`, 'POST', { selected_keys: ['live-1', 'live-2'] });
    }, { port: server.address().port, topic });
    // No filter, tag, refresh, or settings action occurs after the job starts.
    const first = page.locator('.material-row').filter({ hasText: '实时入库文章1' });
    await first.waitFor();
    await expect.poll(() => calls).toBe(2);
    assert.equal(deepClosed, false);
    assert.equal((await page.evaluate(id => window.reader.request('/api/jobs/' + id), job.job_id)).status, 'running');
    await first.click();
    await page.getByRole('tab', { name: '阅读', exact: true }).click();
    await page.getByText('正在精读', { exact: true }).waitFor();
    await page.locator('.deep-stream-text').filter({ hasText: '尚未完成' }).waitFor();
    assert.equal(deepClosed, false, '精读正文已显示时模型响应仍未完成');
    fs.writeFileSync(path.join(root, 'artifacts/workspace-live-before-cancel.png'), await page.screenshot());
    await page.getByRole('button', { name: '取消精读', exact: true }).click();
    await page.getByText('已取消，可手动精读', { exact: true }).waitFor();
    await expect.poll(() => deepClosed).toBe(true);
    await page.locator('.material-row').filter({ hasText: '实时入库文章2' }).waitFor();
    await expect.poll(async () => (await page.evaluate(id => window.reader.request('/api/jobs/' + id), job.job_id)).status).toBe('completed');
    const rows = await page.evaluate(id => window.reader.request(`/api/topics/${id}/inbox`), topic);
    const cancelled = rows.find(row => row.title === '实时入库文章1');
    assert.equal(cancelled.deep_summary_status, 'cancelled');
    assert.equal(cancelled.deep_summary, null);
    assert.equal(calls, 3);
    fs.writeFileSync(path.join(root, 'artifacts/workspace-live-after-cancel.png'), await page.screenshot());
    materialId = cancelled.material_id;
    await page.getByRole('button', { name: '精读 / 重试', exact: true }).click();
    await page.locator('.deep-stream-text').filter({ hasText: '精读第一段' }).waitFor();
    assert.equal(calls, 5);
    assert.ok(!(await page.locator('.deep-stream-text').textContent()).includes('精读第二段'));
    await page.getByRole('tab', { name: '讨论', exact: true }).click();
    await page.getByRole('tab', { name: '阅读', exact: true }).click();
    await page.locator('.deep-stream-text').filter({ hasText: '精读第一段' }).waitFor();
    assert.equal(calls, 5, '切换视图只重连事件流，不重新调用模型');
    fs.writeFileSync(path.join(root, 'artifacts/deep-stream-before-completion.png'), await page.screenshot());
    releaseFinal();
    await page.getByText('精读第一段，精读第二段', { exact: true }).waitFor();
    await expect(page.locator('.deep-stream-text')).toHaveCount(0);
    const savedRows = await page.evaluate(id => window.reader.request(`/api/topics/${id}/inbox`), topic);
    assert.equal(savedRows.find(row => row.id === cancelled.id).deep_summary, '精读第一段，精读第二段');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(root, 'artifacts/deep-stream-completed.png'), await page.screenshot());
    fs.writeFileSync(path.join(root, 'artifacts/workspace-updates-desktop-result.json'), JSON.stringify({ rowVisibleWhileJobRunning: true, noManualRefreshOrFilter: true, deepPreviewVisibleBeforeResponseEnd: true, modelHttpClosedOnCancel: true, cancelledSummaryNotSaved: true, remainingArticlesContinue: true, manualRetryStreaming: true, viewSwitchRestoresSnapshotWithoutNewModelCall: true, validFinalSavedAfterStream: true, pageErrors: errors }, null, 2));
    console.log('Workspace push before job completion and deep-read HTTP cancellation: passed.');
  } finally {
    releaseFinal();
    if (app) await app.close();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

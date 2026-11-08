const { _electron: electron } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '..', '..');
  const directory = fs.mkdtempSync(path.join(root, 'artifacts', 'streaming-desktop-'));
  execFileSync(path.join(root, '.venv', 'Scripts', 'python.exe'), ['-X', 'utf8', path.join(root, 'tests', 'desktop_fixture.py'), directory], { cwd: root });
  let calls = 0, release;
  const gate = new Promise(resolve => { release = resolve; });
  const server = http.createServer(async (request, response) => {
    for await (const _chunk of request) {} // 只消费，不保存包含资料的请求。
    calls++;
    const frame = (text, finish = null) => 'data: ' + JSON.stringify({ choices: [{ index: 0, delta: { content: text }, finish_reason: finish }] }) + '\n\n';
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    if (calls === 1) {
      response.write(frame('{"status":"succeeded","reason":"完成","answer":"这是实时第一段'));
      await gate;
      if (!response.destroyed) response.write(frame('，这是实时第二段。"}', 'stop'));
    } else response.write(frame('{"profiles":[]}', 'stop'));
    response.end('data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let app;
  try {
    app = await electron.launch({ args: [path.join(root, 'desktop')], env: { ...process.env, AGENT_API_KEY: '', AGENT_DATA_DIR: directory, AGENT_TEST_MODE: '1' }, timeout: 40000 });
    const page = await app.firstWindow();
    await page.waitForLoadState('networkidle');
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.evaluate(async port => {
      await window.reader.request('/api/settings/models', 'PUT', { version: 1, default: 'local', profiles: { local: { model: 'stream-test', base_url: `http://127.0.0.1:${port}`, api_key: 'isolated-test-key', strict_tools: false } }, stages: {} });
    }, server.address().port);
    await page.locator('.material-row').first().click();
    await page.getByLabel('讨论问题', { exact: true }).fill('验证真实分片先到达界面，不等待最终结果。');
    await page.getByRole('button', { name: '发送问题', exact: true }).click();
    await page.locator('.stream-answer').filter({ hasText: '这是实时第一段' }).waitFor();
    assert.equal(calls, 1, '首段显示时模型响应仍未结束，记忆提取尚未发生');
    assert.ok(!(await page.locator('.stream-answer').textContent()).includes('"answer"'));
    const runningBranch = await page.getByLabel('讨论分支', { exact: true }).inputValue();
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.getByLabel('模型名称', { exact: true }).waitFor();
    await page.getByRole('button', { name: '阅读来源', exact: true }).click();
    await page.getByRole('button', { name: '关闭设置', exact: true }).click();
    assert.equal(await page.getByLabel('讨论分支', { exact: true }).inputValue(), runningBranch);
    await page.locator('.stream-answer').filter({ hasText: '这是实时第一段' }).waitFor();
    assert.equal(calls, 1, '切换设置不应重启或取消正在生成的模型请求');
    fs.writeFileSync(path.join(root, 'artifacts', 'streaming-before-completion.png'), await page.screenshot());
    release();
    await page.locator('.message.assistant').filter({ hasText: '这是实时第一段，这是实时第二段。' }).waitFor();
    await page.getByRole('button', { name: '发送问题', exact: true }).waitFor();
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(root, 'artifacts', 'streaming-desktop-result.json'), JSON.stringify({ firstFragmentVisibleBeforeResponseEnd: true, settingsPreserveRunningStream: true, finalAnswerSaved: true, pageErrors: errors }, null, 2));
    console.log('Desktop genuine SSE first fragment before completion and saved answer: passed.');
  } finally {
    release();
    if (app) await app.close();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

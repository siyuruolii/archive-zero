// Run against an extracted release, with Python/Node and source paths absent from PATH.
const { _electron: electron, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const os = require('node:os');

(async () => {
  const exe = path.resolve(process.argv[2]);
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'archive-zero-release-'));
  const env = { ...process.env, PATH: path.join(process.env.SystemRoot, 'System32'),
    AGENT_DATA_DIR: data, AGENT_TEST_MODE: '1', AGENT_API_KEY: '', AGENT_PYTHON: 'does-not-exist' };
  delete env.PYTHONPATH;
  const app = await electron.launch({ executablePath: exe, args: [], env, timeout: 60000 });
  try {
    const page = await app.firstWindow();
    await expect(page.locator('.rail-brand')).toContainText('Archive Zero');
    assert.equal(await app.evaluate(({ app }) => app.isPackaged), true);
    await page.getByRole('button', { name: '新建 Topic', exact: true }).count();
    await page.getByTitle('添加主题', { exact: true }).click();
    await page.getByLabel('名称', { exact: true }).fill('发布包验证');
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByRole('heading', { name: '发布包验证', exact: true }).waitFor();
    const hidden = await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0]; window.close();
      return !window.isDestroyed() && !window.isVisible();
    });
    assert.equal(hidden, true);
    assert.ok(fs.existsSync(path.join(data, 'reading.db')));
    console.log('Packaged executable, empty profile, frontend, CRUD and tray lifecycle: passed.');
  } finally { await app.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

const { _electron: electron, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const root = path.resolve(__dirname, '../..');
  const data = fs.mkdtempSync(path.join(root, 'artifacts/settings-ui-'));
  const app = await electron.launch({args:[path.join(root, 'desktop')], env:{...process.env, AGENT_DATA_DIR:data, AGENT_PROFILE_DIR:path.join(data, 'profile'), AGENT_API_KEY:'', AGENT_TEST_MODE:'1'}, timeout:40000});
  try {
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.getByRole('button', {name:'创建第一个 Topic', exact:true}).waitFor();
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isMenuBarVisible()), false);
    await expect(page.locator('.home-footnote')).toHaveCount(0);
    await page.getByRole('button', {name:'更新器', exact:true}).click();
    const dialog = page.getByRole('dialog', {name:'设置', exact:true});
    await expect(dialog.getByRole('heading', {name:'更新器', exact:true})).toBeVisible();
    // UI states use a local IPC substitute; real Git safety is tested in source-update.test.cjs.
    const result = async value => app.evaluate(({ipcMain}, reply) => {
      ipcMain.removeHandler('source-update');
      globalThis.updateActions = [];
      ipcMain.handle('source-update', (_event, action) => { globalThis.updateActions.push(action); if (reply.error) throw new Error(reply.error); return reply; });
    }, value);
    await result({error:'网络暂时不可用，请重试。'});
    await dialog.getByRole('button', {name:'检查更新', exact:true}).click();
    await expect(dialog.getByRole('alert')).toContainText('网络暂时不可用');
    await expect(dialog.getByRole('button', {name:'更新并重启', exact:true})).toHaveCount(0);
    await result({available:true, current:'12345678', latest:'abcdef12'});
    await dialog.getByRole('button', {name:'检查更新', exact:true}).click();
    await expect(dialog.getByRole('button', {name:'更新并重启', exact:true})).toBeVisible();
    await expect(dialog.locator('.update-versions')).toContainText('12345678');
    page.once('dialog', modal => modal.dismiss());
    await dialog.getByRole('button', {name:'更新并重启', exact:true}).click();
    assert.deepEqual(await app.evaluate(() => globalThis.updateActions), ['check']);
    await page.screenshot({path:path.join(root, 'artifacts/settings-updater.png')});
    page.once('dialog', modal => modal.accept());
    await dialog.getByRole('button', {name:'更新并重启', exact:true}).click();
    await expect.poll(() => app.evaluate(() => globalThis.updateActions.length)).toBe(2);
    await result({available:false, current:'abcdef12', latest:'abcdef12'});
    await dialog.getByRole('button', {name:'检查更新', exact:true}).click();
    await expect(dialog.getByRole('status')).toContainText('已是最新版本');
    await expect(dialog.getByRole('button', {name:'更新并重启', exact:true})).toHaveCount(0);
    for (const [name, file] of [['模型设置','models'], ['阅读来源','sources'], ['资料库','library'], ['关于我的记忆','memory'], ['通用','general']]) {
      await dialog.getByRole('button', {name, exact:true}).click();
      await expect(dialog.getByRole('heading', {name, exact:true})).toBeVisible();
      await expect(dialog.locator('nav button[aria-current="page"]')).toHaveCSS('background-color', 'rgb(227, 237, 245)');
      if (file === 'library') {
        await expect(dialog.getByLabel('导入主题', {exact:true})).toBeDisabled();
        await expect(dialog.getByRole('heading', {name:'资料库还没有材料', exact:true})).toBeVisible();
      }
      if (file === 'models') {
        await dialog.getByRole('button', {name:'添加模型', exact:true}).click();
        await dialog.getByLabel('模型名称', {exact:true}).fill('deepseek-chat');
        await dialog.getByLabel('模型接口地址', {exact:true}).fill('https://api.example.com/v1');
        await dialog.getByLabel('模型API密钥', {exact:true}).fill('isolated-settings-test-key');
        await dialog.getByRole('button', {name:'保存模型设置', exact:true}).click();
        await expect(dialog.getByRole('status')).toContainText('已保存');
      }
      await page.screenshot({path:path.join(root, `artifacts/settings-${file}.png`)});
    }
    await page.setViewportSize({width:980, height:680});
    await dialog.getByRole('button', {name:'模型设置', exact:true}).click();
    await dialog.getByLabel('模型名称', {exact:true}).waitFor();
    assert.ok(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth));
    assert.ok(await page.locator('.settings-content').evaluate(element => element.scrollWidth <= element.clientWidth));
    await page.screenshot({path:path.join(root, 'artifacts/settings-compact.png')});
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    assert.deepEqual(errors, []);
    console.log('Settings sections, hidden menu, empty library, updater entry and check/apply/error/cancel states: passed.');
  } finally { await app.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });

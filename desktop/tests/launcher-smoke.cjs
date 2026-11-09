// Run after the EXE --prepare-only test against an isolated copy of the checkout.
const { _electron: electron, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async()=>{
  const root=path.resolve(process.argv[2]);
  const specs=JSON.parse(fs.readFileSync(path.join(root,'scripts/launcher-runtimes.json'),'utf8'));
  const data=fs.mkdtempSync(path.join(root,'isolated-user-data-')); 
  const app=await electron.launch({ executablePath:path.join(root,'desktop/node_modules/electron/dist/electron.exe'), args:[path.join(root,'desktop')],
    env:{...process.env,PATH:path.join(process.env.SystemRoot,'System32'),AGENT_PYTHON:path.join(root,'.runtime','venv-'+specs.python.version,'Scripts/python.exe'),AGENT_DATA_DIR:data,AGENT_TEST_MODE:'1',AGENT_API_KEY:''},timeout:60000 });
  try {
    const page=await app.firstWindow(), errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.getByRole('button',{name:'创建第一个 Topic',exact:true}).click();
    await page.getByLabel('名称',{exact:true}).fill('启动器验收');await page.getByRole('button',{name:'保存',exact:true}).click();
    await page.getByRole('heading',{name:'启动器验收',exact:true}).waitFor();
    assert.ok(await page.locator('.brand-mascot').evaluate(im=>im.complete&&im.naturalWidth>0));
    await page.getByRole('button',{name:'设置',exact:true}).click();await page.getByRole('button',{name:'通用',exact:true}).click();
    await expect(page.getByRole('button',{name:'检查更新',exact:true})).toBeVisible();
    await page.getByRole('button',{name:'检查更新',exact:true}).click();
    await expect(page.getByRole('alert')).toContainText('Git 操作失败');
    await expect(page.getByRole('button',{name:'检查更新',exact:true})).toBeEnabled();
    assert.ok(fs.existsSync(path.join(data,'reading.db')));
    assert.ok(fs.existsSync(path.join(root,'.runtime',specs.node.directory,specs.node.executable)));
    assert.deepEqual(errors,[]);
    console.log('Managed runtimes with no system Python/Node PATH, frontend, CRUD, icons and update controls: passed.');
  } finally {await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

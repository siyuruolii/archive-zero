const { _electron: electron, expect } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async()=>{
  const root=path.resolve(__dirname,'../..');
  const temporary=fs.mkdtempSync(path.join(root,'artifacts/instance-isolation-'));
  const launch=mode=>electron.launch({args:[path.join(root,'desktop')],env:{...process.env,AGENT_DATA_DIR:path.join(temporary,mode,'data'),AGENT_PROFILE_DIR:path.join(temporary,mode,'profile'),AGENT_RUN_MODE:mode,AGENT_API_KEY:'',AGENT_TEST_MODE:'1'},timeout:40000});
  const development=await launch('development');let daily;
  try {
    daily=await launch('daily');
    const devPage=await development.firstWindow(), dailyPage=await daily.firstWindow();
    const errors=[];devPage.on('pageerror',e=>errors.push(e.message));dailyPage.on('pageerror',e=>errors.push(e.message));
    await devPage.getByTitle('添加主题',{exact:true}).click();await devPage.getByLabel('名称',{exact:true}).fill('仅开发目录');await devPage.getByRole('button',{name:'保存',exact:true}).click();
    await devPage.getByRole('heading',{name:'仅开发目录',exact:true}).waitFor();
    await expect(dailyPage.getByRole('button',{name:'创建第一个 Topic',exact:true})).toBeVisible();
    const dailyTopics=await dailyPage.evaluate(()=>window.reader.request('/api/state'));
    assert.equal(dailyTopics.topics.length,0);
    await dailyPage.getByTitle('添加主题',{exact:true}).click();await dailyPage.getByLabel('名称',{exact:true}).fill('仅日常目录');await dailyPage.getByRole('button',{name:'保存',exact:true}).click();
    await dailyPage.getByRole('heading',{name:'仅日常目录',exact:true}).waitFor();
    const devTopics=await devPage.evaluate(()=>window.reader.request('/api/state'));
    assert.deepEqual(devTopics.topics.map(t=>t.name),['仅开发目录']);
    assert.notEqual(await development.evaluate(({app})=>app.getPath('userData')),await daily.evaluate(({app})=>app.getPath('userData')));
    assert.deepEqual(errors,[]);
    console.log('Concurrent development/daily processes, profiles and business data stay isolated: passed.');
  } finally {if(daily)await daily.close();await development.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

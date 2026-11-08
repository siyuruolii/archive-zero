const { _electron: electron, expect } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

(async () => {
  const root=path.resolve(__dirname,'..','..');
  const directory=fs.mkdtempSync(path.join(root,'artifacts','recovery-desktop-'));
  const env={...process.env,AGENT_API_KEY:'',AGENT_DATA_DIR:directory,AGENT_TEST_MODE:'1',AGENT_TOOL_PERMISSIONS:'{}'};
  const crashed=spawnSync(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8',path.join(root,'tests/recovery_crash_fixture.py'),directory],{cwd:root,env,encoding:'utf8',windowsHide:true});
  assert.equal(crashed.status,73,crashed.stderr);
  const info=JSON.parse(fs.readFileSync(path.join(directory,'recovery-info.json'),'utf8'));
  const scope=fs.readdirSync(path.join(directory,'agent-workspaces'))[0];
  const file=path.join(directory,'agent-workspaces',scope,'note.txt');
  const before=fs.statSync(file).mtimeMs;
  let calls=0;
  const server=http.createServer(async(request,response)=>{
    let text='';for await(const chunk of request)text+=chunk;
    const input=JSON.parse(text);calls++;
    response.writeHead(200,{'Content-Type':'text/event-stream'});
    let delta,finish='stop';
    if(calls===1){
      const messages=input.messages;
      for(let i=0;i<messages.length;i++){
        if(messages[i].role==='assistant'&&messages[i].tool_calls){
          const next=[];for(let j=i+1;j<messages.length&&messages[j].role==='tool';j++)next.push(messages[j].tool_call_id);
          assert.deepEqual(new Set(next),new Set(messages[i].tool_calls.map(call=>call.id)));
        }
      }
      assert.ok(messages.some(message=>message.role==='tool'&&message.tool_call_id==='write'));
      delta={tool_calls:[{index:0,id:'repeat-write',type:'function',function:{name:'write_file',arguments:JSON.stringify({expected_sha256:null,content:'hello',path:'note.txt'})}}]};finish='tool_calls';
    }else delta={content:JSON.stringify(calls===2?{status:'succeeded',reason:'已核查',answer:'恢复后已核对笔记，未重复写入。'}:{profiles:[]})};
    response.end('data: '+JSON.stringify({choices:[{index:0,delta,finish_reason:finish}]})+'\n\ndata: [DONE]\n\n');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const bootstrap=path.join(directory,'launch.cjs');
  fs.writeFileSync(bootstrap,`require('electron').app.setPath('userData',${JSON.stringify(path.join(directory,'profile'))});require(${JSON.stringify(path.join(root,'desktop/electron/main.cjs'))});`);
  let app;
  try{
    app=await electron.launch({args:[bootstrap],env,timeout:40000});
    const page=await app.firstWindow();await page.waitForLoadState('networkidle');
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.evaluate(port=>window.reader.request('/api/settings/models','PUT',{version:1,default:'local',profiles:{local:{model:'recovery-test',base_url:`http://127.0.0.1:${port}`,api_key:'isolated-test-key',strict_tools:false}},stages:{}}),server.address().port);
    await page.locator('.discussion-link').first().click();
    const notice=page.getByRole('region',{name:'中断任务恢复'});
    await page.getByRole('button',{name:'恢复续跑',exact:true}).waitFor();
    assert.equal(calls,0,'启动与显示中断状态不会自动续跑');
    fs.writeFileSync(path.join(root,'artifacts/recovery-before-resume.png'),await page.screenshot());
    await page.getByRole('button',{name:'恢复续跑',exact:true}).click();
    await page.locator('.message.assistant').filter({hasText:'恢复后已核对笔记，未重复写入。'}).waitFor();
    await expect(page.getByRole('button',{name:'恢复续跑',exact:true})).toHaveCount(0);
    assert.equal(fs.statSync(file).mtimeMs,before);
    assert.equal(fs.readFileSync(file,'utf8'),'hello');
    const branch=await page.evaluate(id=>window.reader.request('/api/branches/'+id),info.branch_id);
    await expect.poll(async()=> (await page.evaluate(id=>window.reader.request('/api/branches/'+id),info.branch_id)).last_run.phase).toBe('completed');
    const saved=await page.evaluate(id=>window.reader.request('/api/branches/'+id),info.branch_id);
    assert.equal(saved.last_run.metrics.model_calls,4,'旧2次调用加新2次，不重置预算');
    assert.equal(saved.entries.filter(entry=>entry.type==='message'&&entry.data.role==='user').length,1);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(root,'artifacts/recovery-after-resume.png'),await page.screenshot());
    fs.writeFileSync(path.join(root,'artifacts/recovery-desktop-result.json'),JSON.stringify({actualProcessExit:73,manualResumeOnly:true,pairedMessages:true,fileNotRewritten:true,questionNotDuplicated:true,cumulativeModelCalls:4,pageErrors:errors},null,2));
    console.log('Hard process exit, manual checkpoint resume and no duplicate write: passed.');
  }finally{if(app)await app.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});

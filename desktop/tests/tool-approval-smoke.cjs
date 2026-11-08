const { _electron: electron, expect } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '..', '..');
  const directory = fs.mkdtempSync(path.join(root, 'artifacts', 'tool-approval-desktop-'));
  execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-X','utf8',path.join(root,'tests/desktop_fixture.py'),directory], { cwd: root });
  let calls = 0;
  const command = 'Set-Content -LiteralPath proof.txt -Value approved';
  const server = http.createServer(async (request,response) => {
    let body=''; for await (const chunk of request) body+=chunk;
    const payload=JSON.parse(body); calls++;
    const phase=(calls-1)%4;
    response.writeHead(200,{'Content-Type':'text/event-stream'});
    let delta, finish;
    if (phase===0 || phase===1) {
      const name=phase===0?'search_tool':'bash';
      const args=phase===0?{query:'bash',limit:1}:{command,timeout_seconds:10};
      const names=payload.tools.map(item=>item.function.name);
      if(phase===0) assert.ok(!names.includes('bash') && names.includes('search_tool'));
      else assert.ok(names.includes('bash'));
      delta={tool_calls:[{index:0,id:'call-'+calls,type:'function',function:{name,arguments:JSON.stringify(args)}}]};
      finish='tool_calls';
    } else {
      delta={content:JSON.stringify(phase===3?{profiles:[]}:{status:'succeeded',reason:'测试完成',answer:calls===3?'命令已拒绝，未执行。':'命令已确认执行。'})}; finish='stop';
    }
    response.end('data: '+JSON.stringify({choices:[{index:0,delta,finish_reason:finish}]})+'\n\ndata: [DONE]\n\n');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const bootstrap=path.join(directory,'launch.cjs');
  fs.writeFileSync(bootstrap,`require('electron').app.setPath('userData',${JSON.stringify(path.join(directory,'profile'))});require(${JSON.stringify(path.join(root,'desktop/electron/main.cjs'))});`);
  const proofs=()=>fs.existsSync(path.join(directory,'agent-workspaces'))?fs.readdirSync(path.join(directory,'agent-workspaces')).flatMap(name=>{const f=path.join(directory,'agent-workspaces',name,'proof.txt');return fs.existsSync(f)?[f]:[]}):[];
  let app;
  try {
    app=await electron.launch({args:[bootstrap],env:{...process.env,AGENT_API_KEY:'',AGENT_DATA_DIR:directory,AGENT_TEST_MODE:'1',AGENT_TOOL_PERMISSIONS:JSON.stringify({allow_container:false,allow_srt:false})},timeout:40000});
    const page=await app.firstWindow(); await page.waitForLoadState('networkidle');
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.evaluate(port=>window.reader.request('/api/settings/models','PUT',{version:1,default:'local',profiles:{local:{model:'local-tool-test',base_url:`http://127.0.0.1:${port}`,api_key:'isolated-test-key',strict_tools:false}},stages:{}}),server.address().port);
    await page.locator('.material-row').first().click();
    await page.getByLabel('讨论问题',{exact:true}).fill('测试命令确认：先请求，等待拒绝。');
    await page.getByRole('button',{name:'发送问题',exact:true}).click();
    const approval=page.getByRole('complementary',{name:'待确认的命令',exact:true});
    await approval.getByText(command,{exact:true}).waitFor();
    await expect(approval).toContainText('本机 Shell：无 OS 隔离');
    assert.deepEqual(proofs(),[]); assert.equal(calls,2);
    await approval.getByRole('button',{name:'拒绝执行',exact:true}).click();
    await page.locator('.message.assistant').filter({hasText:'命令已拒绝，未执行。'}).waitFor();
    await expect.poll(()=>calls).toBe(4);
    assert.deepEqual(proofs(),[]);
    await page.getByLabel('讨论问题',{exact:true}).fill('再次请求相同命令，等待确认。');
    await page.getByRole('button',{name:'发送问题',exact:true}).click();
    await approval.getByText(command,{exact:true}).waitFor();
    assert.equal(calls,6);assert.deepEqual(proofs(),[]);
    fs.writeFileSync(path.join(root,'artifacts/tool-command-approval.png'),await page.screenshot());
    await approval.getByRole('button',{name:'同意执行本次命令',exact:true}).click();
    await page.locator('.message.assistant').filter({hasText:'命令已确认执行。'}).waitFor();
    assert.equal(proofs().length,1);assert.ok(fs.readFileSync(proofs()[0],'utf8').includes('approved'));
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(root,'artifacts/tool-approval-desktop-result.json'),JSON.stringify({progressiveSchema:true,noExecutionBeforeConsent:true,denialDoesNotExecute:true,identicalCommandAsksAgain:true,confirmedHostCommandExecuted:true,pageErrors:errors},null,2));
    console.log('Progressive tools and per-call host command consent: passed.');
  } finally {if(app)await app.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});

// Exercise the actual EXE -> PowerShell -> Electron path in a prepared disposable checkout.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { setTimeout: pause } = require('node:timers/promises');

(async () => {
  const artifacts = path.resolve(__dirname, '../../artifacts');
  const root = fs.realpathSync(path.resolve(process.argv[2]));
  assert.ok(root.startsWith(artifacts + path.sep), 'Use a disposable prepared checkout under artifacts.');
  assert.ok(!fs.existsSync(path.join(root, '.development.json')), 'Use the daily launcher path.');
  const data = fs.mkdtempSync(path.join(artifacts, 'exe-window-'));
  const result = path.join(data, 'result.json');
  const main = path.join(root, 'desktop/electron/main.cjs');
  const original = fs.readFileSync(main, 'utf8');
  const probe = `
      await window.loadURL(origin);
      setTimeout(async () => {
        try {
          const state = await window.webContents.executeJavaScript(
            "({visible:document.visibilityState, title:document.title, bridge:typeof window.reader, rendered:!!document.querySelector('.sky-room'), images:[...document.images].every(i=>i.complete&&i.naturalWidth>0)})"
          );
          require('node:fs').writeFileSync(${JSON.stringify(result)}, JSON.stringify({windowVisible:window.isVisible(), ...state}));
          require('node:fs').writeFileSync(${JSON.stringify(path.join(data, 'window.png'))}, (await window.webContents.capturePage()).toPNG());
        } finally { app.quit(); }
      }, 1500);
`;
  // Only the disposable fixture is instrumented; the application has no debugging port.
  fs.writeFileSync(main, original.replace('      await window.loadURL(origin);', probe));
  const env = {...process.env, AGENT_DATA_DIR:data, AGENT_API_KEY:''};
  delete env.AGENT_TEST_MODE;
  const launcher = spawn(path.join(root, 'Archive Zero.exe'), [], {cwd:root, env, stdio:'ignore'});
  try {
    const deadline = Date.now()+60000;
    while (!fs.existsSync(result)) {
      if (Date.now()>deadline) throw new Error('Actual EXE startup did not render within 60 seconds.');
      await pause(200);
    }
    const state = JSON.parse(fs.readFileSync(result, 'utf8'));
    console.log(JSON.stringify(state));
    assert.equal(state.windowVisible, true);
    assert.equal(state.visible, 'visible');
    assert.equal(state.bridge, 'object');
    assert.equal(state.rendered, true);
    assert.equal(state.images, true);
    console.log('Actual EXE window visibility, renderer, bridge and images: passed.');
  } finally {
    fs.writeFileSync(main, original);
    if (launcher.exitCode === null) launcher.kill();
  }
})().catch(error => { console.error(error); process.exitCode=1; });

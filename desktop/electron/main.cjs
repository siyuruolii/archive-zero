const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, dialog } = require('electron');
const { spawn, execFile } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const path = require('node:path');
const { shouldHideOnClose, isApiRequest, restoreWindow, bindTrayRestore } = require('./lifecycle.cjs');
const { backendRuntime } = require('./runtime.cjs');
const { readAppearance, writeAppearance } = require('./appearance.cjs');
let backend, window, tray, origin, quitting = false;
const subscriptions = new Map();
const token = randomBytes(32).toString('hex');
// 保持已有桌面配置目录和单实例身份，品牌更名不迁移用户数据。
const profile = process.env.AGENT_PROFILE_DIR || (process.env.AGENT_RUN_MODE === 'development' ? path.join(__dirname, '../../.dev-data/desktop-profile') : path.join(app.getPath('appData'), 'reading-agent-desktop'));
require('node:fs').mkdirSync(profile, { recursive: true });
app.setPath('userData', profile);
app.setName('Archive Zero');
// Keep editing and developer shortcuts without a permanent menu strip.
Menu.setApplicationMenu(Menu.buildFromTemplate([
  { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
  { label: '视图', submenu: [{ role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
  { label: '窗口', submenu: [{ role: 'minimize' }, { role: 'close' }, { role: 'quit', label: '退出应用' }] }
]));
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => restoreWindow(window));
  app.whenReady().then(async () => {
    try {
      const root = path.resolve(__dirname, '..', '..');
      const runtime = backendRuntime(app, process.resourcesPath, process.env, root);
      backend = spawn(runtime.executable, runtime.args, {
        cwd: runtime.cwd, windowsHide: true,
        env: { ...runtime.env, AGENT_APP_TOKEN: token }, stdio: ['ignore', 'pipe', 'pipe']
      });
      let errorText = '';
      backend.stderr.on('data', chunk => { errorText = (errorText + chunk.toString()).slice(-3000); });
      const port = await new Promise((resolve, reject) => {
        const deadline = setTimeout(() => reject(new Error('后端启动超时，请检查 Python 环境和模型配置。')), 30000);
        let buffer = '';
        backend.stdout.on('data', chunk => {
          buffer += chunk.toString();
          if (buffer.includes('\n')) {
            try { const data = JSON.parse(buffer.split('\n')[0]); clearTimeout(deadline); resolve(data.port); }
            catch { clearTimeout(deadline); reject(new Error('后端返回了无效启动信息。')); }
          }
        });
        backend.once('error', () => { clearTimeout(deadline); reject(new Error('无法启动 Python，请先安装项目依赖。')); });
        backend.once('exit', () => { clearTimeout(deadline); reject(new Error('后端退出，请检查模型配置或是否已有后端运行。')); });
      });
      origin = `http://127.0.0.1:${port}`;
      let ready = false;
      for (let i = 0; i < 80; i++) {
        try { const response = await fetch(origin + '/api/state', { headers: { Authorization: 'Bearer ' + token } }); if (response.ok) { ready = true; break; } } catch {}
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (!ready) throw new Error('后端尚未就绪，请检查本地模型配置。');
      window = new BrowserWindow({ width: 1380, height: 920, minWidth: 980, minHeight: 680,
        show: process.env.AGENT_TEST_MODE !== '1', autoHideMenuBar: true,
        icon: path.join(__dirname, '../assets/brand/archive-zero-icon.png'),
        backgroundColor: '#f0f7fb', title: 'Archive Zero · 零号档案',
        webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true }
      });
      window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      window.webContents.on('will-navigate', (event, url) => { if (new URL(url).origin !== origin) event.preventDefault(); });
      window.on('close', event => { if (shouldHideOnClose(quitting)) { event.preventDefault(); window.hide(); } });
      const icon = nativeImage.createFromPath(path.join(__dirname, '../assets/brand/archive-zero-icon-32.png'));
      tray = new Tray(icon);
      tray.setToolTip('Archive Zero · 零号档案');
      tray.setContextMenu(Menu.buildFromTemplate([
        { label: '打开 Archive Zero', click: () => restoreWindow(window) },
        { type: 'separator' }, { label: '退出应用', click: () => app.quit() }
      ]));
      bindTrayRestore(tray, () => window);
      const appearanceDirectory = runtime.env.AGENT_DATA_DIR || path.join(root, '.local');
      ipcMain.handle('appearance', (event, value) => {
        if (event.sender !== window.webContents) throw new Error('请求不被允许');
        return value === undefined ? readAppearance(appearanceDirectory) : writeAppearance(appearanceDirectory, value);
      });
      let updateBusy = false;
      ipcMain.handle('source-update', async (event, action) => {
        if (event.sender !== window.webContents || !['check', 'apply'].includes(action)) throw new Error('请求不被允许');
        if (process.env.AGENT_RUN_MODE === 'development') throw new Error('开发目录请手动管理 Git；日常使用目录可在应用内更新。');
        if (app.isPackaged) throw new Error('此更新器用于完整克隆的源码工作区。');
        if (updateBusy) throw new Error('已有更新检查正在进行。');
        updateBusy = true;
        try {
          const fs = require('node:fs');
          const specs = JSON.parse(fs.readFileSync(path.join(root, 'scripts/launcher-runtimes.json'), 'utf8'));
          const localNode = path.join(root, '.runtime', specs.node.directory, specs.node.executable);
          const node = fs.existsSync(localNode) ? localNode : 'node.exe';
          const status = await new Promise((resolve, reject) => execFile(node, [path.join(root, 'scripts/update-repository.cjs'), '--check'], {
            cwd: root, windowsHide: true, timeout: 60000,
            env: { ...process.env, AGENT_UPDATE_ROOT: root, GIT_TERMINAL_PROMPT: '0', GCM_INTERACTIVE: 'never' }
          }, (error, stdout, stderr) => {
            if (error) return reject(new Error(stderr.trim() || '更新检查失败。'));
            try { resolve(JSON.parse(stdout)); } catch { reject(new Error('更新检查返回了无效结果。')); }
          }));
          if (action === 'apply' && status.available) {
            const launcher = path.join(root, 'Archive Zero.exe');
            if (!fs.existsSync(launcher)) throw new Error('缺少启动器，请先构建或恢复 Archive Zero.exe。');
            const updater = spawn(launcher, ['--update', '--wait-for-pid', String(process.pid)], { cwd: root, detached: true, stdio: 'ignore', windowsHide: true });
            await new Promise((resolve, reject) => { updater.once('spawn', resolve); updater.once('error', reject); });
            updater.unref();
            setImmediate(() => app.quit());
          }
          return status;
        } finally { updateBusy = false; }
      });
      ipcMain.handle('api', async (event, { path: requestPath, method, body }) => {
        if (event.sender !== window.webContents || !isApiRequest(requestPath, method)) throw new Error('请求不被允许');
        const response = await fetch(origin + requestPath, { method,
          headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
          ...(body === undefined ? {} : { body: JSON.stringify(body) })
        });
        const value = await response.json();
        if (!response.ok) throw new Error(value.error || value.detail || '操作失败');
        return value;
      });
      ipcMain.on('quit-app', event => { if (event.sender === window.webContents) app.quit(); });
      ipcMain.handle('agent-subscribe', (event, { id, branchId }) => {
        const deepId = typeof branchId === 'string' && branchId.startsWith('deep:') ? branchId.slice(5) : null;
        if (event.sender !== window.webContents || !/^[a-zA-Z0-9_-]{1,128}$/.test(deepId ?? branchId) || !/^[0-9-]{1,80}$/.test(id) || subscriptions.has(id) || subscriptions.size >= 4) throw new Error('订阅不被允许');
        const controller = new AbortController(); subscriptions.set(id, controller);
        const send = value => { if (!controller.signal.aborted && !window.isDestroyed()) window.webContents.send('agent-event', { id, event: value }); };
        (async () => {
          try {
            const { readSSE } = await import('../shared/sse.mjs');
            const endpoint = deepId ? `/api/topic-materials/${deepId}/deep-read/events` : branchId === 'workspace' ? '/api/workspace/events' : `/api/branches/${branchId}/events`;
            const response = await fetch(origin + endpoint, { headers: { Authorization: 'Bearer ' + token }, signal: controller.signal });
            if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('stream_unavailable');
            send({ type: 'transport_open' });
            for await (const item of readSSE(response.body)) send(item);
            send({ type: 'transport_end' });
          } catch { send({ type: 'transport_error', error: 'stream_disconnected' }); }
          finally { subscriptions.delete(id); }
        })();
        return { started: true };
      });
      ipcMain.on('agent-unsubscribe', (event, { id }) => {
        if (event.sender === window.webContents) { subscriptions.get(id)?.abort(); subscriptions.delete(id); }
      });
      await window.loadURL(origin);
      backend.on('exit', () => { if (!quitting) { dialog.showErrorBox('后端已退出', '请退出应用后重新启动。已保存内容仍在本地。'); } });
    } catch (error) {
      dialog.showErrorBox('启动失败', error.message);
      app.quit();
    }
  });
  app.on('before-quit', event => {
    if (quitting) return;
    event.preventDefault(); quitting = true;
    for (const controller of subscriptions.values()) controller.abort();
    subscriptions.clear();
    (async () => {
      try { if (origin) await fetch(origin + '/api/shutdown', { method: 'POST', headers: { Authorization: 'Bearer ' + token } }); } catch {}
      if (backend && backend.exitCode === null) {
        await Promise.race([new Promise(resolve => backend.once('exit', resolve)), new Promise(resolve => setTimeout(resolve, 4000))]);
        if (backend.exitCode === null) backend.kill();
      }
      app.quit();
    })();
  });
  app.on('window-all-closed', () => {});
}

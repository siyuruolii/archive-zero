const { contextBridge, ipcRenderer } = require('electron');
let sequence = 0;
contextBridge.exposeInMainWorld('reader', {
  request: (path, method = 'GET', body) => ipcRenderer.invoke('api', { path, method, body }),
  quit: () => ipcRenderer.send('quit-app'),
  appearance: value => ipcRenderer.invoke('appearance', value),
  subscribe: (branchId, listener) => {
    const id = Date.now() + '-' + (++sequence);
    let active = true;
    const receive = (_event, message) => { if (active && message.id === id) listener(message.event); };
    ipcRenderer.on('agent-event', receive);
    ipcRenderer.invoke('agent-subscribe', { id, branchId }).then(() => {
      if (!active) ipcRenderer.send('agent-unsubscribe', { id });
    }).catch(() => { if (active) listener({ type: 'transport_error', error: 'subscription_failed' }); });
    return () => { active = false; ipcRenderer.removeListener('agent-event', receive); ipcRenderer.send('agent-unsubscribe', { id }); };
  }
});

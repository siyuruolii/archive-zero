const test = require('node:test');
const assert = require('node:assert/strict');
const { shouldHideOnClose, isApiRequest, restoreWindow, bindTrayRestore } = require('../electron/lifecycle.cjs');
const { EventEmitter } = require('node:events');
test('closing hides until explicit quit', () => {
  assert.equal(shouldHideOnClose(false), true);
  assert.equal(shouldHideOnClose(true), false);
});

test('single and double tray clicks restore a minimized window before showing and focusing', () => {
  const calls = [];
  const window = { isDestroyed:()=>false, isMinimized:()=>true, restore:()=>calls.push('restore'), show:()=>calls.push('show'), focus:()=>calls.push('focus') };
  const tray = new EventEmitter();
  bindTrayRestore(tray, () => window);
  for (const event of ['click', 'double-click']) {
    calls.length = 0; tray.emit(event);
    assert.deepEqual(calls, ['restore', 'show', 'focus']);
  }
  calls.length = 0;
  window.isDestroyed = () => true;
  restoreWindow(window); restoreWindow(null);
  assert.deepEqual(calls, []);
});
test('renderer bridge restricts target and method', () => {
  assert.equal(isApiRequest('/api/state', 'GET'), true);
  for (const value of ['https://other.test/api/', '/api/../secret', '/api/\\secret', '/secret']) assert.equal(isApiRequest(value, 'GET'), false);
  assert.equal(isApiRequest('/api/state', 'CONNECT'), false);
});

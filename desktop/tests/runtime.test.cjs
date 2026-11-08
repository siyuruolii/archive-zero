const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { backendRuntime } = require('../electron/runtime.cjs');

test('packaged backend uses bundled executable and writable per-user data', () => {
  const app = { isPackaged: true, getPath: () => 'user-profile' };
  const result = backendRuntime(app, 'resources', { AGENT_PYTHON: 'untrusted' }, 'checkout');
  assert.equal(result.executable, path.join('resources', 'backend', 'archive-zero-backend.exe'));
  assert.deepEqual(result.args, []);
  assert.equal(result.env.AGENT_DATA_DIR, path.join('user-profile', 'data'));
  assert.equal(result.env.AGENT_FRONTEND_DIR, path.join('resources', 'app', 'dist'));
  assert.equal(backendRuntime(app, 'r', { AGENT_DATA_DIR: 'custom' }, 'c').env.AGENT_DATA_DIR, 'custom');
});

test('development backend retains Python module and project directory', () => {
  const result = backendRuntime({ isPackaged: false }, 'resources', {}, 'checkout');
  assert.deepEqual(result.args, ['-X', 'utf8', '-m', 'reading_agent.server']);
  assert.equal(result.cwd, 'checkout');
  assert.equal(result.env.AGENT_DATA_DIR, undefined);
});

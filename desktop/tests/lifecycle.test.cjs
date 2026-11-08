const test = require('node:test');
const assert = require('node:assert/strict');
const { shouldHideOnClose, isApiRequest } = require('../electron/lifecycle.cjs');
test('closing hides until explicit quit', () => {
  assert.equal(shouldHideOnClose(false), true);
  assert.equal(shouldHideOnClose(true), false);
});
test('renderer bridge restricts target and method', () => {
  assert.equal(isApiRequest('/api/state', 'GET'), true);
  for (const value of ['https://other.test/api/', '/api/../secret', '/api/\\secret', '/secret']) assert.equal(isApiRequest(value, 'GET'), false);
  assert.equal(isApiRequest('/api/state', 'CONNECT'), false);
});

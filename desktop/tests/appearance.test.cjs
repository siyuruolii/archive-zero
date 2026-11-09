const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { readAppearance, writeAppearance } = require('../electron/appearance.cjs');

test('appearance persists independently of backend origin and rejects arbitrary IPC fields', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'archive-zero-appearance-'));
  assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  assert.deepEqual(readAppearance(directory), { character: true, motion: true });
  writeAppearance(directory, { character: false, motion: true });
  assert.deepEqual(readAppearance(directory), { character: false, motion: true });
  for (const value of [null, [], { character: 'false', motion: true }, { character: true, motion: true, constructor: 'x' }, { character: true, motion: true, path: '../other' }]) {
    assert.throws(() => writeAppearance(directory, value));
  }
  assert.deepEqual(readAppearance(directory), { character: false, motion: true });
  fs.writeFileSync(path.join(directory, 'appearance.json'), '{broken');
  assert.deepEqual(readAppearance(directory), { character: true, motion: true });
});

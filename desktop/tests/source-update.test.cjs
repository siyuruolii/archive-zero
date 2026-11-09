const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const cp = require('node:child_process');

test('source updater fast-forwards real Git history and refuses dirty or diverged worktrees', { skip: process.platform !== 'win32' }, t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'archive-zero-update-'));
  assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const realExec = cp.execFileSync;
  const run = (cwd, args) => realExec('git.exe', ['-c','user.name=Test','-c','user.email=test@example.invalid','-C',cwd,...args], { encoding:'utf8', stdio:['ignore','pipe','pipe'] }).trim();
  const seed = path.join(directory, 'seed'), remote = path.join(directory, 'remote.git'), checkout = path.join(directory, 'checkout');
  fs.mkdirSync(seed); run(seed, ['init','-b','main']); fs.writeFileSync(path.join(seed,'file.txt'),'first');
  run(seed,['add','.']); run(seed,['commit','-m','first']); run(directory,['clone','--bare',seed,remote]);
  run(directory,['clone',remote,checkout]); run(seed,['remote','add','origin',remote]);
  let allowedOrigin = true;
  cp.execFileSync = (program, args, options) => {
    assert.equal(options.env.GIT_TERMINAL_PROMPT,'0'); assert.equal(options.env.GCM_INTERACTIVE,'never');
    // Only replace identity lookup; fetch, ancestry checks and merge execute against a real local bare repository.
    if (args.slice(-3).join(' ') === 'remote get-url origin') return allowedOrigin ? 'https://github.com/siyuruolii/archive-zero.git' : 'https://example.invalid/other.git';
    return realExec(program,args,options);
  };
  delete require.cache[require.resolve('../electron/source-update.cjs')];
  const updater = require('../electron/source-update.cjs');
  cp.execFileSync = realExec;
  assert.equal(updater.checkUpdate(checkout).available,false);
  fs.writeFileSync(path.join(seed,'file.txt'),'second'); run(seed,['commit','-am','second']); run(seed,['push','origin','main']);
  assert.equal(updater.checkUpdate(checkout).available,true);
  updater.applyUpdate(checkout); assert.equal(fs.readFileSync(path.join(checkout,'file.txt'),'utf8'),'second');
  fs.writeFileSync(path.join(checkout,'note.txt'),'keep me');
  assert.throws(()=>updater.applyUpdate(checkout),/本地修改/); assert.equal(fs.readFileSync(path.join(checkout,'note.txt'),'utf8'),'keep me');
  run(checkout,['add','.']);run(checkout,['commit','-m','local']);
  fs.writeFileSync(path.join(seed,'file.txt'),'third');run(seed,['commit','-am','third']);run(seed,['push','origin','main']);
  assert.throws(()=>updater.applyUpdate(checkout),/不能自动快进/);
  run(checkout,['switch','-c','work']);assert.throws(()=>updater.checkUpdate(checkout),/main 分支/);
  run(checkout,['switch','main']);allowedOrigin=false;assert.throws(()=>updater.checkUpdate(checkout),/不是本项目/);
});

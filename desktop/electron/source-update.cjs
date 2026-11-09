const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

function git(root, args) {
  try {
    return execFileSync('git.exe', ['-c', 'credential.interactive=never', '-C', root, ...args], {
      encoding: 'utf8', windowsHide: true, timeout: 45000, maxBuffer: 1024 * 1024,
      env: { ...process.env, GIT_TERMINAL_PROMPT: '0', GCM_INTERACTIVE: 'never' },
      stdio: ['ignore', 'pipe', 'pipe']
    }).trim();
  } catch { throw new Error('Git 操作失败。请确认已安装 Git、网络可用，并已为当前仓库配置访问权限；不会弹出登录窗口。'); }
}
function guard(root) {
  const actual = git(root, ['rev-parse', '--show-toplevel']);
  if (fs.realpathSync(actual).toLowerCase() !== fs.realpathSync(root).toLowerCase()) throw new Error('更新只能在完整克隆的项目根目录执行。');
  if (!['https://github.com/siyuruolii/archive-zero.git', 'https://github.com/siyuruolii/archive-zero', 'git@github.com:siyuruolii/archive-zero.git'].includes(git(root, ['remote', 'get-url', 'origin']))) throw new Error('origin 不是本项目的 GitHub 仓库，已停止更新。');
  if (git(root, ['branch', '--show-current']) !== 'main') throw new Error('请切回 main 分支后更新。');
  if (git(root, ['status', '--porcelain'])) throw new Error('仓库有本地修改或未跟踪文件，请先保存、提交或移走这些文件；更新不会覆盖它们。');
}
function checkUpdate(root) {
  guard(root);
  git(root, ['fetch', '--no-tags', 'origin', 'main']);
  const current = git(root, ['rev-parse', 'HEAD']);
  const latest = git(root, ['rev-parse', 'FETCH_HEAD']);
  const common = git(root, ['merge-base', current, latest]);
  if (common !== current) throw new Error('本地提交与远程不一致，不能自动快进更新，请手动处理 Git 分支。');
  return { available: current !== latest, current: current.slice(0, 8), latest: latest.slice(0, 8) };
}
function applyUpdate(root) {
  const status = checkUpdate(root);
  // Recheck immediately before checkout; a background editor may have changed files during fetch.
  guard(root);
  if (status.available) git(root, ['merge', '--ff-only', 'FETCH_HEAD']);
  return status;
}
module.exports = { checkUpdate, applyUpdate, guard };

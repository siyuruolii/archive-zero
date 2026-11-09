import React, { useState } from 'react';

export function SourceUpdate({ busy, hasDraft }: { busy: boolean; hasDraft: boolean }) {
  const [checking, setChecking] = useState(false);
  const [status, setStatus] = useState<{ available: boolean; current: string; latest: string } | null>(null);
  const [error, setError] = useState('');
  if (!window.reader) return null;
  async function run(action: 'check' | 'apply') {
    if (action === 'apply' && !confirm('更新会关闭应用、拉取最新代码并重新启动。请先结束其他精读任务。' + (hasDraft ? '当前有未发送的问题，请先复制保存。' : '') + '是否继续？')) return;
    setChecking(true); setError('');
    try { setStatus(await window.reader!.sourceUpdate(action)); }
    catch (e) { setError((e as Error).message); }
    finally { setChecking(false); }
  }
  return <section className="appearance-settings" aria-label="应用更新"><h3>应用更新</h3><p className="muted">从本项目 GitHub 仓库检查 main 分支更新。本地修改不会被覆盖；首次需要网络及 Git 仓库访问权限。</p><div className="toolbar"><button disabled={checking} onClick={() => { void run('check'); }}>{checking ? '正在处理…' : '检查更新'}</button>{status?.available && <button className="primary" disabled={checking || busy} onClick={() => { void run('apply'); }}>更新并重启</button>}</div>{status && <p className="muted" role="status">{status.available ? `有新版本：${status.current} → ${status.latest}` : `已是最新版本（${status.current}）`}</p>}{error && <p role="alert" className="error-text">{error}</p>}{busy && <p className="muted">当前任务完成后才能更新并重启。</p>}</section>;
}

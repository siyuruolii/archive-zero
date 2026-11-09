import React, { useState } from 'react';
import { Icon } from './ui';

export function SourceUpdate({ busy, hasDraft, compact = false }: { busy: boolean; hasDraft: boolean; compact?: boolean }) {
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const [status, setStatus] = useState<{ available: boolean; current: string; latest: string } | null>(null);
  const [error, setError] = useState('');
  const [checkedAt, setCheckedAt] = useState('');
  if (!window.reader) return <p className="muted">请在桌面应用中检查更新。</p>;
  async function run(action: 'check' | 'apply') {
    if (action === 'apply' && !confirm('更新会关闭应用、拉取最新代码并重新启动。请先结束其他精读任务。' + (hasDraft ? '当前有未发送的问题，请先复制保存。' : '') + '是否继续？')) return;
    setChecking(true); setApplying(action === 'apply'); setError('');
    try { setStatus(await window.reader!.sourceUpdate(action)); setCheckedAt(new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})); }
    catch (e) { setError((e as Error).message); }
    finally { setChecking(false); setApplying(false); }
  }
  return <section className={'source-updater' + (compact ? ' updater-compact' : '')} aria-label="应用更新">
    <div className="updater-heading"><div className="updater-identity"><span className="updater-icon"><Icon name="download" size={24}/></span><div><h3>{compact ? '应用更新' : 'Archive Zero'}</h3><span className="muted">{compact ? '从 GitHub 获取新版本' : 'siyuruolii / archive-zero'}</span></div></div><button disabled={checking} onClick={() => { void run('check'); }}><Icon name="refresh" size={15}/>{checking ? (applying ? '正在更新…' : '正在检查…') : '检查更新'}</button></div>
    {!compact && <div className="update-versions"><div><span>当前版本</span><strong>{status?.current || '待检查'}</strong><small>本地工作区</small></div><div><span>上游版本</span><strong>{status?.latest || '待检查'}</strong><small>GitHub · main</small></div></div>}
    <div className={'update-result' + (status?.available && !error ? ' update-available' : '')}><div><span className="update-state">{error ? '检查未完成' : checking ? (applying ? '正在准备更新' : '正在连接 GitHub') : status ? (status.available ? '发现新版本' : '已是最新版本') : '尚未检查更新'}</span>{checkedAt && <small>上次检查 {checkedAt}</small>}</div>{status?.available && !error && <button className="primary" disabled={checking || busy} onClick={() => { void run('apply'); }}>更新并重启</button>}</div>
    {status && !error && <p className="sr-only" role="status">{status.available ? `有新版本：${status.current} → ${status.latest}` : `已是最新版本（${status.current}）`}</p>}
    {error && <p role="alert" className="error-text update-error">{error}</p>}{busy && <p className="muted">当前任务完成后才能更新并重启。</p>}
    {!compact && <details className="settings-help"><summary>更新说明</summary><p>更新后会自动重启。未发送的问题请先复制保存；本地资料会保留。工作区有修改时停止更新，不会覆盖文件。</p></details>}
  </section>;
}

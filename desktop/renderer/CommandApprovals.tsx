import React, { useEffect, useRef, useState } from 'react';
import { api, type Row } from './api';
import { trapFocus } from './ui';

export function CommandApprovals({ items, onRefresh }: { items: Row[]; onRefresh: () => Promise<void> }) {
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const reject = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    if (items.length) reject.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, [items[0]?.id]);
  async function decide(id: string, approved: boolean) {
    setBusy(id); setError('');
    try { await api(`/api/approvals/${id}`, 'POST', { approved }); await onRefresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(''); }
  }
  if (!items.length) return null;
  return <aside className="command-confirmations" aria-label="待确认的命令">{items.slice(0, 1).map(item => <section key={item.id} role="alertdialog" aria-modal="true" aria-label="命令执行确认" onKeyDown={trapFocus}>
    <h3>是否执行这条 Bash 命令？</h3>
    <p>{item.environment} · 最长 {item.timeout_seconds} 秒</p>
    <small>任务工作区：{item.scope}</small><pre>{item.command}</pre>
    <p>仅批准本次命令；拒绝或超过 60 秒未确认均不执行。</p>
    {error && <p role="alert" className="error-text">{error}</p>}
    {items.length > 1 && <p>另有 {items.length - 1} 条命令等待确认。</p>}
    <div className="toolbar"><button ref={reject} disabled={!!busy} onClick={() => decide(item.id, false)}>拒绝执行</button><button className="primary" disabled={!!busy} onClick={() => decide(item.id, true)}>同意执行本次命令</button></div>
  </section>)}</aside>;
}

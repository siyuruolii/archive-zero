import React, { useEffect, useState } from 'react';
import { api, type Row } from './api';

export function SourceHistory({ sourceId, waitJob, onImported, onClose }: {
  sourceId: string; waitJob: (job: Row) => Promise<Row>;
  onImported: () => Promise<void>; onClose: () => void;
}) {
  const [items, setItems] = useState<Row[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    api(`/api/sources/${sourceId}/history`).then(rows => { if (active) setItems(rows); })
      .catch(e => { if (active) setError(e.message); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [sourceId]);
  async function importSelected() {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await waitJob(await api(`/api/sources/${sourceId}/history`, 'POST', { selected_keys: selected }));
      setNotice(`已导入 ${result.saved.length} 篇文章`);
      setSelected([]);
      setItems(await api(`/api/sources/${sourceId}/history`));
      await onImported();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="source-history" aria-label="选择历史文章" aria-busy={busy}>
    <h3>选择历史文章</h3>
    <p className="muted">仅列出当前 RSS 中尚未导入的文章，不代表网站全部历史。默认不选；日期未知的文章也可手动导入。</p>
    {error && <p role="alert" className="error-text">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {busy && <p role="status">正在获取或导入，请稍候…</p>}
    {!busy && !error && items.length === 0 && <p>没有可选文章。</p>}
    <div className="toolbar"><button disabled={busy || !items.length} onClick={() => setSelected(items.map(item => item.item_key))}>全选</button><button disabled={busy || !selected.length} onClick={() => setSelected([])}>清空选择</button></div>
    <div className="preview">{items.map(item => <label key={item.item_key}>
      <input type="checkbox" disabled={busy} checked={selected.includes(item.item_key)} onChange={e => setSelected(previous => e.target.checked ? [...previous, item.item_key] : previous.filter(key => key !== item.item_key))}/>
      <span>{item.title}<br/><small className="muted">{item.published_at || '发布时间未知'}</small></span>
    </label>)}</div>
    <div className="toolbar"><button className="primary" disabled={busy || !selected.length} onClick={importSelected}>导入所选（{selected.length}）</button><button disabled={busy} onClick={onClose}>收起列表</button></div>
  </section>;
}

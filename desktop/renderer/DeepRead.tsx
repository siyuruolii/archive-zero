import React, { useEffect, useState } from 'react';
import type { Row } from './api';
import { applyRunEvent, subscribeDeepRead } from './streaming';
import { RunStats } from './RunMetrics';

export function DeepRead({ item }: { item: Row }) {
  const [run, setRun] = useState<Row | null>(null);
  const [connection, setConnection] = useState('');
  useEffect(() => {
    setRun(null); setConnection('');
    if (!['running', 'pending'].includes(item.deep_summary_status)) return;
    let active = true;
    let terminal = false;
    let stop = () => {};
    let timer: ReturnType<typeof setTimeout> | undefined;
    function connect() {
      stop();
      stop = subscribeDeepRead(item.id, event => {
        if (!active) return;
        if (event.type === 'transport_open') setConnection('');
        else if (event.type === 'transport_error' || (event.type === 'transport_end' && !terminal)) {
          setConnection('精读连接中断，正在重连…');
          if (timer === undefined) timer = setTimeout(() => { timer = undefined; connect(); }, 1000);
        } else if (event.type !== 'transport_end') {
          if (event.type === 'snapshot' && 'data' in event) terminal = !event.data || event.data.phase !== 'running';
          if (event.type === 'status' && event.phase) terminal = event.phase !== 'running';
          setRun(previous => applyRunEvent(previous, event));
        }
      });
    }
    connect();
    return () => { active = false; stop(); clearTimeout(timer); };
  }, [item.id, item.deep_summary_status]);
  const statuses: Row = { not_scheduled: '尚未精读', pending: '等待精读', running: '正在精读', failed: '精读失败，可重试', cancelled: '已取消，可手动精读' };
  const tools: Row = { material_read: '读取原文', material_search: '查找材料', history_search: '检索历史', memory_search: '检索记忆', session_search: '检索会话' };
  return <section aria-label="精读内容">
    {connection && <p role="status" className="muted">{connection}</p>}
    <p className="prose">{item.deep_summary || statuses[item.deep_summary_status]}</p>
    {!item.deep_summary && run?.answer_draft && <div className="deep-stream"><small className="muted">正在生成 · 尚未校验保存</small><p className="prose deep-stream-text">{run.answer_draft}</p></div>}
    {!!run?.activities?.length && <details open><summary>精读过程</summary><ul className="tool-activity">{run.activities.map((activity: Row) => <li key={activity.call_id}>{tools[activity.name] || '处理材料'} · {activity.status === 'running' ? '执行中' : activity.status === 'error' ? '失败' : '已完成'}</li>)}</ul></details>}
    {run?.metrics?.version && <RunStats metrics={run.metrics} phase={run.phase}/>}
    {item.deep_summary_error && <p className="error-text">{item.deep_summary_error}</p>}
  </section>;
}

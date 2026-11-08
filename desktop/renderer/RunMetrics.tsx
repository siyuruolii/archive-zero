import React, { useState } from 'react';

type Row = Record<string, any>;
const number = (n: any) => typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '未报告';
const compact = (n: number) => n >= 1000000 ? (n / 1000000).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
const percent = (n: number) => (n * 100).toFixed(1) + '%';
const seconds = (n: number) => n >= 60 ? `${Math.floor(n / 60)}分${Math.floor(n % 60)}秒` : `${n.toFixed(1)}秒`;
const phases: Row = { running: '进行中', succeeded: '已完成', completed: '已完成', failed: '失败', stopped: '已停止', interrupted: '已中断', cancelled: '已取消' };
const purposes: Row = { agent: '主任务', compaction: '压缩', session_recall: '会话回忆' };
const labels: Row = { system: '系统提示', conversation: '资料与对话消息', tool_messages: '工具请求与结果', tool_definitions: '工具定义', output_schema: '输出契约', framing: '消息结构' };
const colors: Row = { system: '#517ca0', conversation: '#8075bc', tool_messages: '#4e9292', tool_definitions: '#ba9051', output_schema: '#7399ad', framing: '#b4bdc6' };

export function RunStats({ metrics, phase }: { metrics?: Row; phase?: string }) {
  if (!metrics?.version) return null;
  const cache = metrics.cache || {};
  const total = metrics.tokens?.total_tokens;
  return <details className="run-stats"><summary aria-label="运行详情">
    <span>{phases[phase || metrics.phase] || '运行记录'}</span>
    <span>{seconds(metrics.elapsed_seconds || 0)}</span><span>{metrics.tool_calls || 0} 次工具</span>
    {typeof total === 'number' && <span title="各次请求累计，并非上下文占用">{metrics.usage_calls?.total_tokens < metrics.model_calls ? '已报告 ' : '累计 '}{compact(total)} token</span>}
    <span>缓存{typeof cache.hit_rate === 'number' ? '命中率 ' + percent(cache.hit_rate) : cache.reported_calls ? '无可计算输入' : '未报告'}</span>
  </summary><div className="run-detail">
    <div className="metric-heading"><strong>{metrics.model || '模型未报告'}</strong><span>{metrics.model_calls || 0} 次模型请求</span></div>
    <dl className="metric-grid"><dt>输入 token（累计）</dt><dd>{number(metrics.tokens?.prompt_tokens)}</dd><dt>输出 token（累计）</dt><dd>{number(metrics.tokens?.completion_tokens)}</dd><dt>缓存命中 / 未命中</dt><dd>{number(cache.hit_tokens)} / {number(cache.miss_tokens)}</dd><dt>已执行压缩</dt><dd>{metrics.compactions || 0} 次</dd></dl>
    <p className="metric-note">缓存按已报告的输入 token 加权；可计算 {cache.reported_calls || 0}/{cache.attempted_calls || 0} 次请求。未报告不计为零命中。统计范围为主循环，包含压缩和会话回忆；记忆提取与后台沉淀另计。</p>
    {!!metrics.calls?.length && <div className="call-table"><table><caption>模型请求记录</caption><thead><tr><th>任务 / 模型</th><th>输入 / 输出</th><th>缓存命中</th><th>状态</th></tr></thead><tbody>{metrics.calls.map((call: Row, i: number) => <tr key={i}><td>{purposes[call.purpose] || call.purpose}<small>{call.model || '未知模型'}</small></td><td>{number(call.input_tokens)} / {number(call.output_tokens)}</td><td>{number(call.cache_hit_tokens)}</td><td title={call.status}>{call.status === 'received' ? '已返回' : call.status === 'interrupted' ? (metrics.phase === 'running' ? '请求中' : '未完成') : call.status}</td></tr>)}</tbody></table></div>}
  </div></details>;
}

export function ContextMeter({ metrics, onConfigure }: { metrics?: Row; onConfigure: () => void }) {
  const [open, setOpen] = useState(false);
  const context = metrics?.context;
  const windowKnown = context && typeof context.window_tokens === 'number' && typeof context.input_tokens === 'number';
  const ratio = context ? (windowKnown ? context.input_tokens / context.window_tokens : context.budget_used / context.budget_bytes) : null;
  return <details className="context-meter" open={open} onKeyDown={e => { if (e.key === 'Escape') setOpen(false); }}><summary aria-label="上下文窗口详情" aria-expanded={open} onClick={e => { e.preventDefault(); setOpen(!open); }}>上下文 {ratio === null ? '—' : (windowKnown ? '' : '约 ') + percent(ratio)}</summary><div className="context-popover">
    <div className="metric-heading"><strong>上下文窗口</strong><small>{context?.model || metrics?.model || '尚未请求模型'}</small></div>
    {!context ? <p className="metric-note">发送问题后显示输入用量、预算与组成。</p> : <>
      <div className="window-amount">{number(context.input_tokens)} <span>/ {typeof context.window_tokens === 'number' ? compact(context.window_tokens) + ' token' : '模型窗口未配置'}</span></div>
      <p className="metric-note">输入 token 来自最近一次主模型请求的接口报告，不代表当前编辑框的实时占用，也不是本轮累计 token。</p>
      {windowKnown && <progress aria-label="最近请求输入占模型窗口" value={context.input_tokens} max={context.window_tokens}/>}
      <div className="budget-heading"><strong>本地保守预算</strong><span>{number(context.budget_used)} / {number(context.budget_bytes)}</span></div>
      <progress aria-label="本地保守预算占用" value={context.budget_used} max={context.budget_bytes}/>
      <p className="metric-note">输入按 UTF-8 字节计数，输出预留 {number(context.output_reserve_tokens)} token 按保守代理值加入预算。下方分项是字节，不是精确 token 拆分。</p>
      <div className="context-stack" aria-hidden="true">{Object.entries(context.breakdown_bytes || {}).map(([key, value]) => <span key={key} style={{ width: `${Number(value) / context.input_bytes * 100}%`, background: colors[key] }}/>)}</div>
      <dl className="context-rows">{Object.entries(context.breakdown_bytes || {}).map(([key, value]) => <React.Fragment key={key}><dt><i style={{ background: colors[key] }}/>{labels[key] || key}</dt><dd>{number(value)} B</dd></React.Fragment>)}</dl>
      <div className="budget-remaining">预算剩余 <strong>{number(Math.max(0, context.budget_bytes - context.budget_used))}</strong></div>
    </>}
    <button type="button" className="configure-window" onClick={onConfigure}>配置模型窗口与预算</button>
  </div></details>;
}

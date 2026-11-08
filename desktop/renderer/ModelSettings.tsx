import React, { useEffect, useState } from 'react';

type Row = Record<string, any>;
type Props = { request: (path: string, method?: string, body?: any) => Promise<any>; onSaved: () => Promise<void> };
const labels: Row = { route: '材料路由', deep_read: '精读', discuss: '讨论', compact: '上下文压缩', extract: '记忆提取', consolidate: '记忆沉淀', session_recall: '会话回忆归纳', history_select: '来源历史选择' };
const emptyProfile = () => ({ model: '', base_url: '', api_key: '', adapter: 'chat_completions', json_mode: 'json_object', supports_tools: true, strict_tools: false, cache_mode: 'unknown', context_byte_budget: 131072, context_window_tokens: null, output_token_budget: 4096, token_field: 'max_tokens', request_timeout: 30, reasoning_effort: null });

export function ModelSettings({ request, onSaved }: Props) {
  const [config, setConfig] = useState<Row | null>(null);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [usage, setUsage] = useState<Row>({});
  useEffect(() => { request('/api/settings/model-usage').then(r => setUsage(r.profiles)).catch(() => {}); }, [request]);
  useEffect(() => { request('/api/settings/models').then(c => { setConfig(c); setSelected(c.default); }).catch(() => setError('无法读取模型设置，请检查后端是否已启动。')); }, [request]);
  if (!config) return <section className="page">{error || '正在读取设置…'}</section>;
  const names = Object.keys(config.profiles);
  const profile = config.profiles[selected];
  function update(field: string, value: any) { setConfig(c => ({ ...c, profiles: { ...c!.profiles, [selected]: { ...c!.profiles[selected], [field]: value } } })); setNotice(''); }
  function add() { let i = 1; while (config!.profiles['model_' + i]) i++; const name = 'model_' + i; setConfig(c => ({ ...c, profiles: { ...c!.profiles, [name]: emptyProfile() }, default: c!.default || name })); setSelected(name); setNotice(''); }
  function remove() {
    if (names.length < 2) { setError('请至少保留一个模型配置。'); return; }
    const profiles = { ...config!.profiles }; delete profiles[selected];
    const fallback = Object.keys(profiles)[0];
    setConfig({ ...config, profiles, default: config!.default === selected ? fallback : config!.default, stages: Object.fromEntries(Object.entries(config!.stages).filter(([, p]) => p !== selected)) }); setSelected(fallback); setNotice('');
  }
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError(''); setNotice('');
    try { const saved = await request('/api/settings/models', 'PUT', config); setConfig(saved); setUsage({}); setNotice('已保存，新任务使用新设置；正在运行的模型循环继续使用原配置。'); await onSaved(); }
    catch { setError('保存未确认：请检查模型名称、接口地址、密钥、阶段选择和长度限制，并重新打开设置核对结果。'); }
    finally { setBusy(false); }
  }
  return <section className="page model-settings"><form onSubmit={save}>
    <p className="muted">支持兼容 Chat Completions 的接口。先添加模型，再为阶段选择模型；未单独指定的阶段使用默认模型。</p>
    {config.environment_override && <p className="muted">当前存在 AGENT_* 环境配置；重启时它会覆盖默认模型。要以页面设置为准，请先移除环境覆盖，单独指定的阶段分配仍保留。</p>}
    <button type="button" onClick={() => request('/api/settings/model-usage').then(r => setUsage(r.profiles)).catch(() => setError('无法读取调用统计。'))}>刷新调用统计</button>
    {error && <p role="alert" className="error-text">{error}</p>}{notice && <p role="status">{notice}</p>}
    <fieldset disabled={busy}><legend>模型配置</legend><div className="toolbar"><label>编辑配置<select aria-label="编辑模型配置" value={selected} onChange={e => setSelected(e.target.value)}>{names.map(n => <option key={n} value={n}>{n} · {config.profiles[n].model || '新模型'}</option>)}</select></label><button type="button" onClick={add}>添加模型</button>{profile && <button type="button" onClick={remove}>移除此配置</button>}</div>
      {profile && <><label>模型名称<input aria-label="模型名称" required value={profile.model} onChange={e => update('model', e.target.value)}/></label>
        {usage[selected] && <p className="muted">本次启动以来：完成 {usage[selected].completed} 次；缓存命中 token：{usage[selected].cache_hit_tokens ?? '接口未报告'}，未命中：{usage[selected].cache_miss_tokens ?? '接口未报告'}。更换设置后重新统计。</p>}
        <label>接口地址<input aria-label="模型接口地址" type="url" required placeholder="https://example.com/v1" value={profile.base_url} onChange={e => update('base_url', e.target.value)}/></label>
        <label>API 密钥<input aria-label="模型API密钥" type="password" autoComplete="new-password" required={!profile.has_api_key} value={profile.api_key || ''} placeholder={profile.has_api_key ? '已保存；留空保留原密钥' : '输入密钥'} onChange={e => update('api_key', e.target.value)}/></label>
        <details><summary>接口能力与预算</summary>
          <label>输出格式<select aria-label="模型输出格式" value={profile.json_mode} onChange={e => update('json_mode', e.target.value)}><option value="text">文本（程序校验 JSON）</option><option value="json_object">JSON 模式</option><option value="json_schema">JSON Schema 约束</option></select></label>
          <label><input type="checkbox" checked={profile.supports_tools} onChange={e => update('supports_tools', e.target.checked)}/>支持工具调用（路由、精读、讨论、沉淀需要）</label>
          <label><input type="checkbox" checked={profile.strict_tools} onChange={e => update('strict_tools', e.target.checked)}/>接口支持严格工具参数</label>
          <label>供应商前缀缓存<select value={profile.cache_mode} onChange={e => update('cache_mode', e.target.value)}><option value="unknown">能力未知</option><option value="automatic">供应商自动缓存</option><option value="unsupported">不支持</option></select></label><p className="muted">此项记录接口能力，不是远程缓存开关。命中情况以接口实际返回为准。</p>
          <label>上下文保守预算（UTF-8 字节）<input type="number" min="1000" max="200000" required value={profile.context_byte_budget} onChange={e => update('context_byte_budget', Number(e.target.value))}/></label>
          <label>模型上下文窗口（token，可留空）<input aria-label="模型上下文窗口" type="number" min="1" max="10000000" placeholder="以供应商说明为准" value={profile.context_window_tokens ?? ''} onChange={e => update('context_window_tokens', e.target.value ? Number(e.target.value) : null)}/></label>
          <label>最大输出 token<input type="number" min="32" max="16000" required value={profile.output_token_budget} onChange={e => update('output_token_budget', Number(e.target.value))}/></label>
          <label>接口超时（秒）<input type="number" min="1" max="300" required value={profile.request_timeout} onChange={e => update('request_timeout', Number(e.target.value))}/></label>
          <label>输出长度参数<select value={profile.token_field} onChange={e => update('token_field', e.target.value)}><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></select></label>
          <label>推理强度<select value={profile.reasoning_effort || ''} onChange={e => update('reasoning_effort', e.target.value || null)}><option value="">不传，使用接口默认</option>{['none', 'low', 'medium', 'high', 'max'].map(v => <option key={v}>{v}</option>)}</select></label>
        </details></>}
    </fieldset>
    <fieldset disabled={busy || !names.length}><legend>阶段分配</legend><label>默认模型<select aria-label="默认模型" value={config.default} onChange={e => setConfig({ ...config, default: e.target.value })}>{names.map(n => <option key={n} value={n}>{n}</option>)}</select></label>
      {(config.available_stages || Object.keys(labels)).map((stage: string) => <label key={stage}>{labels[stage] || stage}<select aria-label={labels[stage] || stage} value={config.stages[stage] || ''} onChange={e => { const stages = { ...config.stages }; if (e.target.value) stages[stage] = e.target.value; else delete stages[stage]; setConfig({ ...config, stages }); }}>{<option value="">继承默认模型</option>}{names.map(n => <option key={n} value={n}>{n}</option>)}</select></label>)}
    </fieldset><button className="primary" type="submit" disabled={busy || !names.length}>{busy ? '正在保存…' : '保存模型设置'}</button>
  </form></section>;
}

import React, { useEffect, useRef, useState } from 'react';
import { api, type Row } from './api';
import { ModelSettings } from './ModelSettings';
import { ContextMeter, RunStats } from './RunMetrics';
import { applyRunEvent, mergeBranch, subscribeBranch, subscribeWorkspace } from './streaming';
import { Sidebar } from './Sidebar';
import { SettingsShell } from './SettingsShell';
import { MaterialPanel } from './MaterialPanel';
import { SourceHistory } from './SourceHistory';
import { DeepRead } from './DeepRead';
import { CommandApprovals } from './CommandApprovals';
import { SourceUpdate } from './SourceUpdate';
import { ReadingHome } from './ReadingHome';
import { AppearanceSettings, useAppearance } from './AppearanceSettings';
import { Icon, Notice, trapFocus } from './ui';

export default function App() {
  const [state, setState] = useState<Row>({ topics: [], tags: [], sources: [], links: [] });
  const [topic, setTopic] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState('models');
  const [panelOpen, setPanelOpen] = useState(true);
  const [homeOpen, setHomeOpen] = useState(false);
  const [focusReading, setFocusReading] = useState(false);
  const appearanceSettings = useAppearance();
  const [displayMode, setDisplayMode] = useState('discuss');
  const [discussions, setDiscussions] = useState<Row[]>([]);
  const [rawItems, setRawItems] = useState<Row[]>([]);
  const [importTopic, setImportTopic] = useState('');
  const [memoryTopic, setMemoryTopic] = useState('');
  const [sourceTopic, setSourceTopic] = useState('');
  const topicRef = useRef(''); topicRef.current = topic;
  const chosenMaterial = useRef('');
  const [items, setItems] = useState<Row[]>([]);
  const [selected, setSelected] = useState<Row | null>(null);
  const [mode, setMode] = useState('all');
  const [tags, setTags] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [working, setWorking] = useState(false);
  const [approvals, setApprovals] = useState<Row[]>([]);
  async function loadApprovals() { setApprovals(await api('/api/approvals')); }
  const [cancellingDeep, setCancellingDeep] = useState('');
  const [startingDeep, setStartingDeep] = useState('');
  const itemsRequest = useRef(0);
  const itemQuery = useRef(''); itemQuery.current = `${topic}|${mode}|${tags.join(',')}`;
  const workspaceRefresh = useRef<() => Promise<void>>(async () => {});
  const [form, setFormState] = useState('');
  const formFocus = useRef<HTMLElement | null>(null);
  const formActive = useRef(false);
  function setForm(next: string) {
    if (next) { formFocus.current = document.activeElement as HTMLElement | null; formActive.current = true; setError(''); }
    else { formActive.current = false; setTimeout(() => { if (!formActive.current) formFocus.current?.focus(); }, 0); }
    setFormState(next);
  }
  const [branches, setBranches] = useState<Row[]>([]);
  const [branch, setBranch] = useState('');
  const branchRef = useRef(''); branchRef.current = branch;
  const [conversation, setConversation] = useState<Row>({ entries: [] });
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [streamStatus, setStreamStatus] = useState('');
  const messagesRef = useRef<HTMLDivElement>(null);
  const tailRef = useRef<HTMLDivElement>(null);
  const followStream = useRef(true);
  const justSent = useRef(false);
  const [question, setQuestion] = useState('');
  const [memories, setMemories] = useState<Row[]>([]);
  const [feedPreview, setFeedPreview] = useState<Row[]>([]);
  const [chosenKeys, setChosenKeys] = useState<string[]>([]);
  const [policy, setPolicy] = useState('none');
  const [historySource, setHistorySource] = useState('');
  const [editingMemory, setEditingMemory] = useState<Row | null>(null);
  const current = state.topics.find((t: Row) => t.id === topic);
  const topicTags = state.tags.filter((t: Row) => t.topic_id === topic);
  const running = conversation.run?.phase === 'running';
  const currentMetrics = conversation.run?.metrics?.version ? conversation.run.metrics : conversation.last_run?.metrics;
  const hasSavedMetrics = conversation.entries.some((e: Row) => e.data?.metrics?.version && e.data?.run_id === (conversation.run?.run_id || conversation.last_run?.run_id));
  useEffect(() => {
    if (followStream.current && (running || justSent.current)) {
      if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
      tailRef.current?.scrollIntoView({ block: 'nearest' });
    }
    if (!running && conversation.run?.phase) justSent.current = false;
  }, [conversation.run?.answer_draft, conversation.entries.length, running]);

  async function refresh() {
    const data = await api('/api/state'); setState(data);
    if (!data.topics.some((t: Row) => t.id === topic)) setTopic(data.topics[0]?.id || '');
  }
  async function loadItems() {
    const request = ++itemsRequest.current;
    const query = `${topic}|${mode}|${tags.join(',')}`;
    if (!topic) { setItems([]); return; }
    const data = await api(`/api/topics/${topic}/inbox?recommended=${mode === 'recommended'}&untagged=${mode === 'untagged'}&tags=${tags.join(',')}`);
    if (topicRef.current !== topic || itemQuery.current !== query || itemsRequest.current !== request) return;
    setItems(data);
    setSelected(previous => previous ? data.find((item: Row) => item.id === previous.id) || previous : null);
  }
  async function loadDiscussions() {
    if (!topic) { setDiscussions([]); return; }
    const data = await api(`/api/topics/${topic}/discussions`);
    if (topicRef.current === topic) setDiscussions(data);
  }
  async function loadSettings() {
    if (!settingsOpen) return;
    if (settingsTab === 'memory') setMemories(await api('/api/memories' + (memoryTopic ? '?topic_id=' + memoryTopic : '')));
    if (settingsTab === 'raw') setRawItems(await api('/api/materials'));
  }
  function openSettings(section = 'models') {
    setNotice('');
    setSettingsTab(section); setImportTopic(topic); setMemoryTopic(topic); setSourceTopic(topic); setSettingsOpen(true);
  }
  async function action(work: () => Promise<any>, text = '已保存') {
    setError(''); setNotice(''); setWorking(true);
    try { await work(); if (text) setNotice(text); await refresh(); await loadItems(); await loadDiscussions(); await loadSettings(); }
    catch (e) { setError((e as Error).message); }
    finally { setWorking(false); }
  }
  async function waitJob(result: Row) {
    for (;;) {
      const job = await api('/api/jobs/' + result.job_id);
      if (job.status === 'failed') throw new Error('任务失败：' + job.error);
      if (job.status === 'completed') { if (job.result?.status === 'failed') throw new Error(job.result.reason); if (job.result?.routing_failures) throw new Error('材料已保存，但部分路由失败，请在原始材料中查看原因并重试。'); return job.result; }
      await new Promise(resolve => setTimeout(resolve, 700));
    }
  }
  workspaceRefresh.current = async () => { await refresh(); await loadItems(); await loadSettings(); await loadApprovals(); };
  useEffect(() => {
    let active = true, busy = false, dirty = false;
    let stop = () => {};
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    async function flush() {
      refreshTimer = undefined;
      if (!active || busy) return;
      busy = true; dirty = false;
      try { await workspaceRefresh.current(); }
      catch (e) { if (active) setError((e as Error).message); }
      finally { busy = false; if (active && dirty) schedule(); }
    }
    function schedule() {
      dirty = true;
      if (!busy && refreshTimer === undefined) refreshTimer = setTimeout(flush, 100);
    }
    function connect() {
      if (!active) return;
      stop();
      stop = subscribeWorkspace(event => {
        if (!active) return;
        if (event.type === 'workspace_changed') schedule();
        if ((event.type === 'transport_error' || event.type === 'transport_end') && reconnectTimer === undefined) {
          reconnectTimer = setTimeout(() => { reconnectTimer = undefined; connect(); }, 1000);
        }
      });
    }
    connect();
    return () => { active = false; stop(); clearTimeout(refreshTimer); clearTimeout(reconnectTimer); };
  }, []);
  async function startDeep(id: string) {
    setStartingDeep(id);
    try {
      await action(async () => {
        const result = await waitJob(await api(`/api/topic-materials/${id}/deep-read`, 'POST', {}));
        setNotice(result.status === 'cancelled' ? '精读已取消' : '精读已完成');
      }, '');
    } finally { setStartingDeep(''); }
  }
  async function cancelDeep(id: string) {
    setCancellingDeep(id); setError('');
    try {
      const result = await api(`/api/topic-materials/${id}/deep-read/cancel`, 'POST');
      setNotice(result.status === 'succeeded' ? '精读已经完成' : '精读已取消');
      await loadItems();
    } catch (e) { setError((e as Error).message); }
    finally { setCancellingDeep(''); }
  }
  async function resumeDiscussion() {
    if (!branch || !conversation.recovery?.can_resume) return;
    const requested = branch;
    await action(async () => {
      await api(`/api/branches/${branch}/resume`, 'POST', { expected_run_id: conversation.recovery.run_id });
      const data = await api(`/api/branches/${requested}`);
      if (branchRef.current === requested) { setConversation(data); setStreamEpoch(value => value + 1); }
    }, '已从检查点恢复，继续处理');
  }
  useEffect(() => { refresh().catch(e => setError(e.message)); }, []);
  useEffect(() => { chosenMaterial.current = ''; setSelected(null); setItems([]); setBranch(''); setBranches([]); setTags([]); setDiscussions([]); setQuestion(''); setPanelOpen(true); }, [topic]);
  useEffect(() => { loadItems().catch(e => setError(e.message)); }, [topic, mode, tags.join(',')]);
  useEffect(() => { loadDiscussions().catch(e => setError(e.message)); }, [topic]);
  useEffect(() => { loadSettings().catch(e => setError(e.message)); }, [settingsOpen, settingsTab, memoryTopic]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { if (form) setForm(''); else setSettingsOpen(false); } };
    document.addEventListener('keydown', escape); return () => document.removeEventListener('keydown', escape);
  }, [form]);
  useEffect(() => {
    if (!branch) { setConversation({ entries: [] }); return; }
    let active = true, streaming = false, retries = 0;
    let stop = () => {}, timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => api('/api/branches/' + branch).then(data => { if (active) setConversation(previous => mergeBranch(previous, data)); }).catch(e => { if (active) setError(e.message); });
    const connect = () => {
      stop();
      stop = subscribeBranch(branch, event => {
        if (!active) return;
        if (event.type === 'transport_open') { setStreamStatus(''); return; }
        if (event.type === 'transport_error' || (event.type === 'transport_end' && streaming)) {
          setStreamStatus('连接中断，正在恢复…'); load();
          if (timer === undefined) timer = setTimeout(() => { timer = undefined; connect(); }, Math.min(5000, 500 * 2 ** Math.min(retries++, 4)));
          return;
        }
        if (event.type === 'snapshot' && 'data' in event) streaming = event.data?.phase === 'running';
        if (event.type === 'status' && event.phase) streaming = event.phase === 'running';
        if (event.type === 'message_saved') loadDiscussions();
        if (event.type === 'message_saved' || (event.type === 'status' && event.phase && event.phase !== 'running') || (event.type === 'snapshot' && event.data?.phase && event.data.phase !== 'running')) load();
        setConversation(previous => ({ ...previous, run: applyRunEvent(previous.run, event) }));
      });
    };
    setStreamStatus(''); load(); connect();
    return () => { active = false; stop(); if (timer !== undefined) clearTimeout(timer); };
  }, [branch, streamEpoch]);
  async function choose(item: Row, requestedBranch?: string, view = 'discuss') {
    if (item.topic_id !== topicRef.current) return;
    chosenMaterial.current = item.id;
    setHomeOpen(false);
    if (selected?.id !== item.id || (requestedBranch && requestedBranch !== branch)) setQuestion('');
    setSelected(item); setBranch(''); setBranches([]); setDisplayMode(view);
    const data = await api(`/api/topic-materials/${item.id}/sessions`);
    if (item.topic_id !== topicRef.current || chosenMaterial.current !== item.id) return;
    setBranches(data);
    if (requestedBranch && data.some((entry: Row) => entry.id === requestedBranch)) setBranch(requestedBranch);
    else if (data.length) setBranch(data[0].id);
  }
  async function openDiscussion(row: Row) {
    try {
      const all = await api(`/api/topics/${row.topic_id}/inbox`);
      const item = all.find((entry: Row) => entry.id === row.topic_material_id);
      if (!item) throw new Error('这份材料已不可访问，请刷新 Topic。');
      await choose(item, row.id);
    } catch (error) { setError((error as Error).message); }
  }
  async function send(stopFirst = false) {
    if (!question.trim() || !selected) return;
    followStream.current = true; justSent.current = true;
    await action(async () => {
      let bid = branch;
      if (!bid) { const created = await api(`/api/topic-materials/${selected.id}/sessions`, 'POST'); bid = created.branch_id; setBranch(bid); setBranches(await api(`/api/topic-materials/${selected.id}/sessions`)); }
      if (stopFirst) await api(`/api/branches/${bid}/stop`, 'POST');
      const latest = await api('/api/branches/' + bid);
      await api(`/api/branches/${bid}/messages`, 'POST', { question, expected_leaf: latest.leaf_id });
      setStreamEpoch(value => value + 1);
      setQuestion('');
    }, '已发送');
  }
  async function submitForm(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget));
    await action(async () => {
      if (form === 'topic') { const result = await api('/api/topics', 'POST', data); setTopic(result.id); setSettingsOpen(false); }
      if (form === 'topic-edit') await api('/api/topics/' + topic, 'PATCH', data);
      if (form === 'tag') await api(`/api/topics/${topic}/tags`, 'POST', data);
      if (form === 'source') await waitJob(await api('/api/sources', 'POST', { ...data, policy, selected_keys: chosenKeys, topic_ids: sourceTopic ? [sourceTopic] : [] }));
      if (form === 'memory') { if (editingMemory) await api('/api/memories/' + editingMemory.id, 'PATCH', { content: data.content }); else await api('/api/memories', 'POST', { content: data.content, topic_id: data.scope === 'global' ? null : memoryTopic }); }
      setForm(''); setFeedPreview([]); setChosenKeys([]);
    });
  }
  function remove(table: string, id: string) {
    if (confirm('确认删除？已有记忆会保留，删除的内容不会自动恢复。')) action(async () => {
      await api(`/api/objects/${table}/${id}`, 'DELETE');
      if (table === 'topic_materials' && selected?.id === id) { setSelected(null); setBranch(''); }
    }, '已删除');
  }
  const clearNotice = () => { setError(''); setNotice(''); };

  return <div className={"app sky-room" + (focusReading ? " focus-reading" : "")} data-motion={appearanceSettings.appearance.motion}>
    <Sidebar topics={state.topics} topic={topic} discussions={discussions} branch={branch} model={state.model}
      onTopic={setTopic} onDiscussion={row => { void openDiscussion(row); }} onCreate={() => setForm('topic')} onSettings={() => openSettings()}/>
    <main className="workspace">
      <header className="workspace-header">
        <div className="workspace-breadcrumb"><Icon name="folder"/><h1>{current?.name || 'Archive Zero'}</h1><span className="header-description" title={current?.description}>{current?.description}</span></div>
        <div className="header-actions">{selected && <><button className="home-toggle" onClick={() => setHomeOpen(!homeOpen)}>{homeOpen ? '返回当前材料' : '主题首页'}</button>{!homeOpen && <button className={'focus-toggle' + (focusReading ? ' selected' : '')} aria-pressed={focusReading} onClick={() => setFocusReading(!focusReading)}>{focusReading ? '退出专注' : '专注阅读'}</button>}</>}<button className={'panel-toggle' + (panelOpen && !focusReading ? ' selected' : '')} aria-pressed={panelOpen && !focusReading} aria-label="切换材料列表" onClick={() => { if (focusReading) { setFocusReading(false); setPanelOpen(true); } else setPanelOpen(!panelOpen); }}><Icon name="panel" size={16}/><span>材料</span></button>
          <button className="icon-button" title="刷新" aria-label="刷新" onClick={() => action(async () => {}, '已刷新')}><Icon name="refresh" size={16}/></button>
          {current && <details className="action-menu"><summary aria-label="Topic 操作"><Icon name="more"/></summary><div><button onClick={() => setForm('topic-edit')}>编辑主题</button><button className="danger" onClick={() => remove('topics', topic)}>删除主题</button></div></details>}
        </div>
      </header>
      {!settingsOpen && !form && <Notice error={error} notice={notice} onClose={clearNotice}/>}
      {working && !settingsOpen && <div className="progress" role="status">正在处理，请稍候…</div>}
      <div className="workspace-body">
        <section className="workspace-canvas" aria-label="阅读与讨论">
          {!selected || homeOpen ? <ReadingHome topic={current} items={items} character={appearanceSettings.appearance.character} onChoose={item => { void choose(item, undefined, 'read').catch(e => setError(e.message)); }} onCreate={() => setForm('topic')} onSources={() => openSettings('sources')} onBrowse={() => { setFocusReading(false); setPanelOpen(true); }}/> : <>
            <div className="material-heading"><div className="material-heading-text"><small>当前材料</small><h2>{selected.title}</h2></div><div className="material-actions">
              <button onClick={() => action(() => api(`/api/topic-materials/${selected.id}`, 'PATCH', { is_read: !selected.is_read }))}>{selected.is_read ? '标为未读' : '标为已读'}</button>
              <button disabled={working || startingDeep === selected.id || selected.deep_summary_status === 'running' || !!selected.deep_summary} onClick={() => startDeep(selected.id)}>精读 / 重试</button>
              {['pending', 'running'].includes(selected.deep_summary_status) && <button disabled={cancellingDeep === selected.id} onClick={() => cancelDeep(selected.id)}>{cancellingDeep === selected.id ? '正在取消…' : '取消精读'}</button>}
              <details className="action-menu"><summary aria-label="材料操作"><Icon name="more" size={17}/></summary><div><button onClick={() => action(() => api(`/api/topic-materials/${selected.id}`, 'PATCH', { interest: selected.interest === 'uninterested' ? 'none' : 'uninterested' }))}>{selected.interest === 'uninterested' ? '撤销不感兴趣' : '不感兴趣'}</button><button className="danger" onClick={() => remove('topic_materials', selected.id)}>删除材料</button></div></details>
            </div></div>
            <div className="view-tabs" role="tablist" aria-label="材料视图"><button role="tab" aria-selected={displayMode === 'discuss'} onClick={() => setDisplayMode('discuss')}><Icon name="chat" size={15}/>讨论</button><button role="tab" aria-selected={displayMode === 'read'} onClick={() => setDisplayMode('read')}><Icon name="book" size={15}/>阅读</button></div>
            {displayMode === 'read' ? <article className="article-view"><small className="muted source-url">{selected.url}</small><h3>摘要</h3><p className="prose">{selected.short_summary}</p><h3>精读</h3><DeepRead key={selected.id} item={selected}/>
              <details><summary>为什么推荐</summary><p className="prose">{selected.recommendation_reason || '暂无推荐说明。'}</p></details><details className="article-tags"><summary>材料标签</summary><div className="tag-list">{topicTags.map((tag: Row) => <label key={tag.id}><input type="checkbox" checked={selected.tag_ids?.includes(tag.id)} onChange={e => action(() => api(`/api/topic-materials/${selected.id}`, 'PATCH', { tag_ids: e.target.checked ? [...selected.tag_ids, tag.id] : selected.tag_ids.filter((id: string) => id !== tag.id) }))}/>{tag.name}</label>)}</div></details>
            </article> : <section className="discussion">
              <div className="discussion-top"><span>围绕材料讨论</span><select aria-label="讨论分支" value={branch} onChange={e => { setBranch(e.target.value); setQuestion(''); }}><option value="">新讨论</option>{branches.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select>{branch && <details className="action-menu"><summary aria-label="讨论操作"><Icon name="more" size={16}/></summary><div><button onClick={() => action(() => api(`/api/branches/${branch}/compact`, 'POST'), '已压缩较早历史')}>压缩旧对话</button><button className="danger" onClick={() => action(async () => { await api(`/api/branches/${branch}`, 'DELETE'); setBranches(await api(`/api/topic-materials/${selected.id}/sessions`)); setBranch(''); })}>删除分支</button></div></details>}</div>
              <div className="messages" ref={messagesRef} onScroll={e => { const node = e.currentTarget; followStream.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80; }}>
                {!conversation.entries.some((entry: Row) => entry.type === 'message') && !running && <div className="discussion-empty"><Icon name="chat" size={24}/><h3>你想读懂哪一点？</h3><p>可以追问一个概念、核对原文，或与以前读过的内容做比较。</p><details className="material-preview"><summary>先看材料摘要</summary><p>{selected.short_summary}</p></details></div>}
                {conversation.entries.filter((entry: Row) => entry.type === 'message' && entry.data.kind !== 'tool_pair').map((entry: Row) => <div className={'message ' + entry.data.role} key={entry.id}><strong>{entry.data.role === 'user' ? '你' : 'Archive Zero'}{entry.data.incomplete && '（未完成）'}</strong><p>{entry.data.content}</p>{entry.data.role === 'assistant' && <RunStats metrics={entry.data.metrics}/>}<button className="fork-action" onClick={() => action(async () => { const item = await api(`/api/branches/${branch}/fork`, 'POST', { entry_id: entry.id }); setBranches(await api(`/api/topic-materials/${selected.id}/sessions`)); setBranch(item.id); }, '已从此处分叉')}>从此处分叉</button></div>)}
                {running && <details open className="stream-block"><summary>正在生成</summary><div className="stream-answer" role="status"><small>生成中，尚未校验</small><p>{conversation.run?.answer_draft || conversation.run?.status_message || '正在读取与思考，请稍候…'}</p>{conversation.run?.activities?.map((item: Row) => <div className="tool-activity" key={item.call_id}>{item.name}：{item.status === 'running' ? '执行中' : item.status === 'success' || item.status === 'completed' ? '已完成' : '失败'}</div>)}</div></details>}
                {!running && conversation.last_run?.phase === 'interrupted' && <section className="recovery-notice" aria-label="中断任务恢复"><strong>上次任务意外中断</strong><p>{conversation.recovery?.can_resume ? '可以从检查点续跑，沿用已使用预算，不自动重放未知副作用。' : '当前无法续跑：' + (({ branch_changed_or_result_already_saved: '分支已变化或结果已保存，不能重放这轮任务', checkpoint_missing: '缺少检查点', question_changed: '原始问题已变化' } as Row)[conversation.recovery?.reason] || '检查点不可用')}</p>{!!conversation.recovery?.unknown_operations?.length && <><p className="error-text">有操作的执行结果尚未确认。续跑会先核查文件状态，仍无法确认时仅允许读取与分析。</p><details><summary>查看待核查操作</summary><pre>{JSON.stringify(conversation.recovery.unknown_operations, null, 2)}</pre></details></>}<button className="primary" disabled={working || !conversation.recovery?.can_resume} onClick={resumeDiscussion}>恢复续跑</button></section>}
                {conversation.run?.error && <p role="alert" className="error-text">{conversation.run.error}</p>}<div ref={tailRef}/>
              </div>
              <div className="composer-wrap"><div className="composer"><textarea aria-label="讨论问题" placeholder="提出一个问题，继续理解这份材料…" value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !running && !working && question.trim()) { e.preventDefault(); void send(); } }}/>
                {streamStatus && <p className="muted" role="status">{streamStatus}</p>}<div className="composer-metrics">{(running || !hasSavedMetrics) && <RunStats metrics={currentMetrics} phase={conversation.run?.phase || conversation.last_run?.phase}/>}</div>
                <div className="composer-bottom"><span className="composer-model" title={currentMetrics?.model}>◌ {currentMetrics?.model || '按阶段设置模型'}</span><ContextMeter metrics={currentMetrics} onConfigure={() => openSettings('models')}/><div className="send">{running && <><button onClick={() => action(() => api(`/api/branches/${branch}/stop`, 'POST'), '已停止')}>停止</button><button disabled={!question.trim()} onClick={() => send(true)}>停止并发送</button></>}<button className="primary send-button" aria-label="发送问题" title="发送问题（Enter）" disabled={running || working || !question.trim()} onClick={() => send()}><Icon name="arrow" size={19}/></button></div></div>
              </div><div className="composer-caption"><span>已读或讨论不等于掌握</span><span>Shift + Enter 换行</span></div></div>
            </section>}
          </>}
        </section>
        {panelOpen && !focusReading && <MaterialPanel character={appearanceSettings.appearance.character && !!selected && !homeOpen} items={items} selected={selected} tags={topicTags} filterTags={tags} mode={mode} onMode={setMode} onTag={id => setTags(tags.includes(id) ? tags.filter(item => item !== id) : [...tags, id])} onChoose={item => { void choose(item).catch(e => setError(e.message)); }} onAddTag={() => setForm('tag')} onRemoveTag={id => remove('tags', id)} onSources={() => openSettings('sources')} onClose={() => setPanelOpen(false)}/>}
      </div>
    </main>
    {settingsOpen && <SettingsShell section={settingsTab} onSection={setSettingsTab} onClose={() => setSettingsOpen(false)}>
      {!form && <Notice error={error} notice={notice} onClose={clearNotice}/>} {working && <div className="progress" role="status">正在处理，请稍候…</div>}
      {settingsTab === 'models' && <ModelSettings request={api} onSaved={refresh}/>}
      {settingsTab === 'sources' && <section className="page"><button className="primary" onClick={() => setForm('source')}>添加 RSS 来源</button><p className="muted">获取更新仅导入添加来源之后发布的文章。历史文章可手动勾选；日期未知的文章需手动导入。关联主题不会回填历史。</p>{state.sources.map((s: Row) => <div className="source" key={s.id}><h2>{s.name}</h2><p>{s.entry_url}</p><div className="toolbar"><button disabled={working || !s.is_enabled} onClick={() => action(() => api(`/api/sources/${s.id}/fetch`, 'POST').then(waitJob), '更新完成，仅获取添加来源后发布的文章')}>获取更新</button><button disabled={working || !s.is_enabled} onClick={() => setHistorySource(historySource === s.id ? '' : s.id)}>选择历史文章</button><button onClick={() => action(() => api(`/api/sources/${s.id}`, 'PATCH', { enabled: !s.is_enabled }))}>{s.is_enabled ? '停用' : '启用'}</button><button onClick={() => action(() => api(`/api/sources/${s.id}`, 'PATCH', { removed: true }), '来源已移除，历史材料保留')}>移除来源</button></div><div className="tag-list">{state.topics.map((t: Row) => <label key={t.id}><input type="checkbox" checked={state.links.some((l: Row) => l.topic_id === t.id && l.source_id === s.id && l.is_linked)} onChange={e => action(() => api(`/api/sources/${s.id}/topics/${t.id}`, 'PUT', { linked: e.target.checked }))}/>{t.name}</label>)}</div>{historySource === s.id && <SourceHistory key={s.id} sourceId={s.id} waitJob={waitJob} onImported={async () => { await refresh(); await loadItems(); await loadSettings(); }} onClose={() => setHistorySource('')}/>}</div>)}</section>}
      {settingsTab === 'raw' && <section className="page"><p>导入目标：<select aria-label="导入主题" value={importTopic} onChange={e => setImportTopic(e.target.value)}>{state.topics.map((t: Row) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></p>{rawItems.map(item => <div className="source" key={item.id}><h2>{item.title}</h2><p>{item.source_summary}</p><small>被 {item.reference_count} 个主题引用 · 路由：{item.routing_status}</small>{item.routing_error && <p className="error-text">{item.routing_error}</p>}<div className="toolbar"><button disabled={!importTopic || working} onClick={() => action(() => api(`/api/materials/${item.id}/import`, 'POST', { topic_id: importTopic }).then(waitJob), '导入完成')}>导入所选主题</button><button disabled={working} onClick={() => action(() => api(`/api/materials/${item.id}/route`, 'POST').then(waitJob), '路由已重试')}>重试路由</button><button disabled={item.reference_count > 0} onClick={() => remove('materials', item.id)}>删除原始材料</button></div></div>)}</section>}
      {settingsTab === 'memory' && <section className="page"><label className="scope-select">查看范围<select aria-label="记忆查看范围" value={memoryTopic} onChange={e => setMemoryTopic(e.target.value)}><option value="">仅全局记忆</option>{state.topics.map((item: Row) => <option key={item.id} value={item.id}>{item.name} + 全局</option>)}</select></label><button className="primary" onClick={() => { setEditingMemory(null); setForm('memory'); }}>添加明确背景或偏好</button><p className="muted">显示当前主题与明确全局记忆。删除依据不会删除既存记忆；更正后旧画像退出召回。</p>{state.memory_error && <p className="error-text">最近一次记忆提炼失败：{state.memory_error}，可手动补记。</p>}{memories.map(m => <div className="source" key={m.id}><small>{m.scope === 'global' ? '全局' : '当前主题'} · {m.kind === 'profile' ? '背景与偏好' : '可确认经历'}</small><p>{m.content}</p><div className="toolbar"><button onClick={() => { setEditingMemory(m); setForm('memory'); }}>更正</button><button onClick={() => action(() => api('/api/memories/' + m.id, 'DELETE'))}>删除记忆</button><button onClick={() => action(async () => { const refs = await api(`/api/memories/${m.id}/evidence`); setNotice(refs.length ? refs.map((r: Row) => r.message).join('；') : '用户主动提供，无外部依据引用'); }, '')}>查看依据状态</button></div></div>)}</section>}

      {settingsTab === 'general' && <section className="page general-settings"><h3>Archive Zero · 零号档案</h3><p>以 Topic 整理关注的内容，在材料之间积累理解。</p><AppearanceSettings {...appearanceSettings}/><SourceUpdate busy={running || working} hasDraft={!!question.trim()}/><div className="setting-row"><div><strong>关闭窗口</strong><p className="muted">关闭后留在托盘，已经开始的任务继续执行。</p></div><span>保持运行</span></div><div className="setting-row"><div><strong>数据与记忆</strong><p className="muted">阅读记录与记忆保存在本机。更换模型不会清空已有讨论。</p></div><span>本地保存</span></div>{window.reader && <button onClick={() => window.reader?.quit()}>退出应用</button>}</section>}
    </SettingsShell>}
    <CommandApprovals items={approvals} onRefresh={loadApprovals}/>
    {form && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setForm(''); }}><form className="modal" role="dialog" aria-modal="true" onKeyDown={trapFocus} aria-label={({ topic: '添加主题', 'topic-edit': '编辑主题', tag: '添加标签', source: '添加阅读来源', memory: '添加记忆' } as Row)[form]} onSubmit={submitForm}>
      <div className="modal-heading"><h2>{({ topic: '添加主题', 'topic-edit': '编辑主题', tag: '添加标签', source: '添加阅读来源', memory: '添加记忆' } as Row)[form]}</h2><button type="button" className="icon-button" aria-label="关闭表单" onClick={() => setForm('')}><Icon name="close"/></button></div>
      {error && <p className="error-text" role="alert">{error}</p>}
      {form !== 'memory' && <label>名称<input autoFocus name="name" required maxLength={200} defaultValue={form === 'topic-edit' ? current?.name : ''}/></label>}
      {(form === 'topic' || form === 'topic-edit' || form === 'tag') && <label>描述<textarea name="description" defaultValue={form === 'topic-edit' ? current?.description : ''} placeholder="说明你关注什么，或标签的适用边界。"/></label>}
      {form === 'source' && <><label>RSS 地址<input name="url" type="url" required/></label><label>初始关联 Topic<select aria-label="初始关联 Topic" value={sourceTopic} onChange={e => setSourceTopic(e.target.value)}><option value="">暂不关联</option>{state.topics.map((item: Row) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>本次历史获取<select value={policy} onChange={e => setPolicy(e.target.value)}><option value="none">暂不获取历史</option><option value="selected">我勾选历史条目</option><option value="model">让模型选择（会调用模型）</option></select></label>
        {policy === 'selected' && <><button type="button" onClick={e => { const element = e.currentTarget.form!; const url = new FormData(element).get('url'); action(async () => setFeedPreview(await api('/api/sources/preview', 'POST', { url }))); }}>预览可获取条目</button><div className="preview">{feedPreview.map(item => <label key={item.item_key}><input type="checkbox" checked={chosenKeys.includes(item.item_key)} onChange={e => setChosenKeys(e.target.checked ? [...chosenKeys, item.item_key] : chosenKeys.filter(key => key !== item.item_key))}/>{item.title}</label>)}</div></>}
        <p className="muted">之后可以在阅读来源中调整关联；关联不会自动导入旧材料。</p></>}
      {form === 'memory' && <><label>明确背景或偏好<textarea autoFocus name="content" defaultValue={editingMemory?.content || ''} required maxLength={2000}/></label><label>适用范围<select name="scope" defaultValue={editingMemory?.scope || (memoryTopic ? 'topic' : 'global')} disabled={!!editingMemory}><option value="topic" disabled={!memoryTopic}>仅当前查看的 Topic</option><option value="global">明确适用于所有主题</option></select></label></>}
      <div className="toolbar modal-actions"><button type="button" onClick={() => setForm('')}>取消</button><button className="primary" disabled={working}>保存</button></div>
    </form></div>}
  </div>;
}

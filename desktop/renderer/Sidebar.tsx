import React, { useState } from 'react';
import { Icon } from './ui';
import mascot from '../assets/brand/archive-zero-icon.png';

type Row = Record<string, any>;
type Props = { topics: Row[]; topic: string; discussions: Row[]; branch: string; model?: string; onTopic: (id: string) => void; onDiscussion: (row: Row) => void; onCreate: () => void; onSettings: () => void };

export function Sidebar({ topics, topic, discussions, branch, model, onTopic, onDiscussion, onCreate, onSettings }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [query, setQuery] = useState('');
  const visible = topics.filter(t => t.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <aside className={'rail' + (collapsed ? ' rail-collapsed' : '')} aria-label="工作区侧边栏">
    <div className="rail-brand"><img className="brand-mascot" src={mascot} alt="零号档案 Q 版管理员"/><div className="brand-wordmark"><strong title="零号档案">Archive Zero</strong><small>零号档案 · 晴空阅览室</small></div><button className="icon-button rail-collapse" aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'} onClick={() => setCollapsed(!collapsed)}><Icon name="sidebar"/></button></div>
    <button className="new-topic" title="添加主题" aria-label="添加主题" onClick={onCreate}><Icon name="plus"/><span>新建 Topic</span></button>
    {!collapsed && <label className="topic-search"><Icon name="search" size={16}/><input aria-label="查找 Topic" placeholder="查找 Topic" value={query} onChange={e => setQuery(e.target.value)}/></label>}
    <div className="rail-section-label"><span>我的 Topic</span><span>{topics.length}</span></div>
    <nav className="topic-tree" aria-label="Topic 列表">{visible.map(t => <div className="topic-group" key={t.id}>
      <button className={'topic-button' + (topic === t.id ? ' active' : '')} aria-label={t.name} aria-current={topic === t.id ? 'page' : undefined} aria-expanded={topic === t.id && !collapsed} title={t.name} onClick={() => onTopic(t.id)}><Icon name="folder"/><span>{t.name}</span><span className={'topic-chevron' + (topic === t.id ? ' expanded' : '')}><Icon name="chevron" size={12}/></span></button>
      {topic === t.id && !collapsed && <div className="topic-children">{discussions.length ? discussions.slice(0, 12).map(d => <button key={d.id} className={'discussion-link' + (branch === d.id ? ' selected' : '')} title={d.material_title} aria-label={'打开讨论：' + (d.branch_title === '主讨论' ? d.material_title : d.branch_title)} onClick={() => onDiscussion(d)}><Icon name="chat" size={14}/><span>{d.branch_title === '主讨论' ? d.material_title : d.branch_title}</span></button>) : <span className="topic-no-chats">阅读材料后，在这里继续讨论</span>}</div>}
    </div>)}</nav>
    {!visible.length && !collapsed && <p className="rail-empty">{topics.length ? '没有匹配的 Topic' : '创建一个 Topic，收集你关心的内容。'}</p>}
    <div className="rail-bottom"><button className="settings-entry" aria-label="设置" title="设置" onClick={onSettings}><span className="settings-avatar"><Icon name="settings"/></span><span><strong>设置</strong><small title={model}>{model || '模型、阅读源与偏好'}</small></span></button></div>
  </aside>;
}

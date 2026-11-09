import React, { useState } from 'react';
import { Icon } from './ui';
import { ReadingCompanion } from './ReadingHome';

type Row = Record<string, any>;
type Props = { character?: boolean; items: Row[]; selected: Row | null; tags: Row[]; filterTags: string[]; mode: string; onMode: (mode: string) => void; onTag: (id: string) => void; onChoose: (item: Row) => void; onAddTag: () => void; onRemoveTag: (id: string) => void; onSources: () => void; onClose: () => void };

export function MaterialPanel(props: Props) {
  const [query, setQuery] = useState('');
  const items = props.items.filter(item => (item.title + ' ' + item.short_summary).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <aside className="material-panel" aria-label="当前 Topic 材料">{props.character && <ReadingCompanion/>}<div className="material-panel-heading"><strong>材料</strong><span>{props.items.length}</span><button className="icon-button" aria-label="收起材料列表" onClick={props.onClose}><Icon name="panel" size={16}/></button></div>
    <label className="material-search"><Icon name="search" size={15}/><input aria-label="筛选当前材料" placeholder="筛选当前材料" value={query} onChange={e => setQuery(e.target.value)}/></label>
    <div className="filters">{[['all', '收件箱'], ['recommended', '推荐'], ['untagged', '无标签']].map(([mode, title]) => <button key={mode} className={props.mode === mode ? 'selected' : ''} onClick={() => props.onMode(mode)}>{title}</button>)}</div>
    <div className="tag-list">{props.tags.map(tag => <label key={tag.id}><input type="checkbox" checked={props.filterTags.includes(tag.id)} onChange={() => props.onTag(tag.id)}/>{tag.name}<button title="删除标签定义" aria-label={'删除标签：' + tag.name} onClick={() => props.onRemoveTag(tag.id)}>×</button></label>)}<button className="add-tag" onClick={props.onAddTag}>＋标签</button></div>
    <div className="material-scroll">{items.map(item => <button className={'material-row' + (props.selected?.id === item.id ? ' chosen' : '')} key={item.id} onClick={() => props.onChoose(item)}><small><span className={'read-dot' + (item.is_read ? ' read' : '')}/>{item.is_read ? '已读' : '未读'}{item.recommended ? ' · 推荐' : ''}<time>{item.published_at?.slice(0, 10) || '时间未知'}</time></small><h2>{item.title}</h2><p>{item.short_summary}</p></button>)}
      {!items.length && <div className="material-empty"><Icon name="book" size={24}/><h3>{query || props.mode !== 'all' || props.filterTags.length ? '没有匹配的材料' : '还没有材料'}</h3><p>{query ? '试试其他关键词。' : '关联阅读源，或从资料库导入已有内容。'}</p><button onClick={props.onSources}>管理阅读来源</button></div>}
    </div>
  </aside>;
}

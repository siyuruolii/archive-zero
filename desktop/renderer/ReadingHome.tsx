import React from 'react';
import type { Row } from './api';
import librarian from '../assets/brand/sky-archivist.webp';
import { Icon } from './ui';

type Props = { topic?: Row; items: Row[]; character: boolean; onChoose: (item: Row) => void; onCreate: () => void; onSources: () => void; onBrowse: () => void };

export function ReadingHome({ topic, items, character, onChoose, onCreate, onSources, onBrowse }: Props) {
  return <div className={'reading-home' + (character ? '' : ' without-character')}>
    <section className="reading-welcome" aria-label="晴空阅览室">
      <div className="welcome-window" aria-hidden="true"><span className="welcome-cloud"/></div>
      <div className="welcome-copy"><p className="welcome-label">晴空阅览室</p><h2>{topic ? <>今天，也有<br/>值得细读的故事。</> : <>从一份材料，<br/>开始理解。</>}</h2>
        <p className="welcome-description">{topic?.description || '读原文、问问题，把每次好奇，慢慢收进档案。'}</p>
        <div className="welcome-actions"><button className="primary" onClick={() => { if (!topic) onCreate(); else if (items.length) onChoose(items[0]); else onSources(); }}>{!topic ? '创建第一个 Topic' : items.length ? '开始阅读' : '添加阅读来源'}</button><button onClick={topic && items.length ? onSources : onBrowse}>{topic && items.length ? '添加阅读来源' : '浏览材料'}</button></div>
      </div>
      {character && <><img className="welcome-character" src={librarian} alt="蓝灰短发、手持档案书的阅览室管理员"/><span className="welcome-signature" aria-hidden="true">零号档案<br/>阅览室管理员</span></>}
    </section>
    <section className="home-materials" aria-label="主题材料预览"><div className="home-materials-heading"><h3>{topic ? '从这里继续阅读' : '你的阅读档案'}</h3><button onClick={onBrowse}>浏览材料 <Icon name="chevron" size={13}/></button></div>
      {items.length ? <div className="home-material-grid">{items.slice(0, 3).map(item => <button className="home-material-card" key={item.id} onClick={() => onChoose(item)}><span className="home-material-type">{item.recommended ? '推荐阅读' : item.is_read ? '已读材料' : '等待阅读'}</span><strong>{item.title}</strong><p>{item.short_summary}</p><small>{item.published_at?.slice(0, 10) || '发布时间未知'}</small></button>)}</div> : <div className="home-empty"><Icon name="book" size={23}/><div><strong>{topic ? '暂无材料' : '还没有阅读记录'}</strong>{topic && <p>添加阅读来源，或调整当前筛选。</p>}</div></div>}
    </section>
  </div>;
}

export function ReadingCompanion() {
  return <div className="reading-companion" aria-label="阅览室管理员"><div><img src={librarian} alt="阅览室管理员"/></div><p>把问题，留给下一页。</p></div>;
}

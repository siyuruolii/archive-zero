import React, { useEffect, useRef } from 'react';
import { Icon, trapFocus } from './ui';

const sections = [{ id: 'models', name: '模型设置', icon: 'model' }, { id: 'sources', name: '阅读来源', icon: 'rss' }, { id: 'raw', name: '资料库', icon: 'folder' }, { id: 'memory', name: '关于我的记忆', icon: 'memory' }, { id: 'general', name: '通用', icon: 'settings' }, { id: 'updates', name: '更新器', icon: 'download' }];

export function SettingsShell({ section, onSection, onClose, children }: { section: string; onSection: (id: string) => void; onClose: () => void; children: React.ReactNode }) {
  const close = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; close.current?.focus(); return () => previous?.focus(); }, []);
  useEffect(() => { if (content.current) content.current.scrollTop = 0; }, [section]);
  const current = sections.find(s => s.id === section)!;
  return <div className="settings-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><section className="settings-window" role="dialog" aria-modal="true" aria-label="设置" onKeyDown={trapFocus}>
    <aside className="settings-nav"><h2>设置</h2><nav aria-label="设置分类">{sections.map(item => <button key={item.id} aria-current={section === item.id ? 'page' : undefined} className={(section === item.id ? 'selected' : '') + (item.id === 'general' ? ' settings-nav-divider' : '')} onClick={() => onSection(item.id)}><Icon name={item.icon}/>{item.name}</button>)}</nav><span className="settings-keyhint"><kbd>Esc</kbd> 关闭设置</span></aside>
    <div className="settings-main"><div className="settings-heading"><h2>{current.name}</h2><button ref={close} className="icon-button" aria-label="关闭设置" onClick={onClose}><Icon name="close"/></button></div><div ref={content} className="settings-content">{children}</div></div>
  </section></div>;
}

export function SettingsEmpty({ icon, title, children }: { icon: string; title: string; children?: React.ReactNode }) {
  return <div className="settings-empty"><span className="settings-empty-icon"><Icon name={icon} size={26}/></span><h3>{title}</h3>{children && <p>{children}</p>}</div>;
}

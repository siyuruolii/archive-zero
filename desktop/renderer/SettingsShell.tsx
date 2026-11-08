import React, { useEffect, useRef } from 'react';
import { Icon, trapFocus } from './ui';

const sections = [{ id: 'models', name: '模型设置', icon: 'model', description: '选择模型与各阶段的运行配置' }, { id: 'sources', name: '阅读来源', icon: 'rss', description: '订阅 RSS，管理来源与 Topic 的关联' }, { id: 'raw', name: '资料库', icon: 'folder', description: '查看原始材料，按需导入 Topic' }, { id: 'memory', name: '关于我的记忆', icon: 'memory', description: '查看、更正明确的背景与偏好' }, { id: 'general', name: '通用', icon: 'info', description: '本地工作区与应用行为' }];

export function SettingsShell({ section, onSection, onClose, children }: { section: string; onSection: (id: string) => void; onClose: () => void; children: React.ReactNode }) {
  const close = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; close.current?.focus(); return () => previous?.focus(); }, []);
  useEffect(() => { if (content.current) content.current.scrollTop = 0; }, [section]);
  const current = sections.find(s => s.id === section)!;
  return <div className="settings-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><section className="settings-window" role="dialog" aria-modal="true" aria-label="设置" onKeyDown={trapFocus}>
    <aside className="settings-nav"><h2>设置</h2><nav aria-label="设置分类">{sections.map(item => <button key={item.id} aria-current={section === item.id ? 'page' : undefined} className={section === item.id ? 'selected' : ''} onClick={() => onSection(item.id)}><Icon name={item.icon}/>{item.name}</button>)}</nav><span className="settings-local">Archive Zero · 零号档案 · 本地</span></aside>
    <div className="settings-main"><div className="settings-heading"><div><h2>{current.name}</h2><p>{current.description}</p></div><button ref={close} className="icon-button" aria-label="关闭设置" onClick={onClose}><Icon name="close"/></button></div><div ref={content} className="settings-content">{children}</div></div>
  </section></div>;
}

import React, { useEffect, useState } from 'react';

export type Appearance = { character: boolean; motion: boolean };
const defaults: Appearance = { character: true, motion: true };

export function useAppearance() {
  const [appearance, setAppearance] = useState<Appearance>(defaults);
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const value = window.reader ? await window.reader.appearance() : JSON.parse(localStorage.getItem('archive-zero.appearance') || 'null');
        if (active && value && typeof value.character === 'boolean' && typeof value.motion === 'boolean') setAppearance({ character: value.character, motion: value.motion });
      } catch { if (active) setError('无法读取外观设置，已使用默认外观。'); }
      finally { if (active) setReady(true); }
    }
    void load();
    return () => { active = false; };
  }, []);
  async function update(next: Appearance) {
    const previous = appearance;
    setAppearance(next);
    setSaving(true); setError('');
    try {
      if (window.reader) await window.reader.appearance(next);
      else localStorage.setItem('archive-zero.appearance', JSON.stringify(next));
    } catch { setAppearance(previous); setError('外观设置未保存，请检查本地数据目录是否可写。'); }
    finally { setSaving(false); }
  }
  return { appearance, update, disabled: !ready || saving, error };
}

export function AppearanceSettings({ appearance, update, disabled, error }: ReturnType<typeof useAppearance>) {
  return <section className="appearance-settings" aria-label="阅读外观"><h3>外观</h3>
    <label className="setting-row appearance-toggle"><span><strong>角色陪伴</strong><span className="setting-description">在首页和阅读侧栏显示管理员</span></span><input type="checkbox" aria-label="角色陪伴" checked={appearance.character} disabled={disabled} onChange={e => { void update({ ...appearance, character: e.target.checked }); }}/></label>
    <label className="setting-row appearance-toggle"><span><strong>页面动效</strong><span className="setting-description">遵循系统的减少动态效果设置</span></span><input type="checkbox" aria-label="页面动效" checked={appearance.motion} disabled={disabled} onChange={e => { void update({ ...appearance, motion: e.target.checked }); }}/></label>
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>;
}

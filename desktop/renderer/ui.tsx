import React from 'react';

export function trapFocus(event: React.KeyboardEvent<HTMLElement>) {
  if (event.key !== 'Tab') return;
  const elements = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]')).filter(element => element.offsetParent !== null);
  const first = elements[0], last = elements.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    book: <><path d="M3 5.5c4-2 6-1 9 0 3-1 5-2 9 0v14c-4-2-6-1-9 0-3-1-5-2-9 0z"/><path d="M12 5.5v14"/></>,
    folder: <path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11H3z"/>,
    chat: <path d="M4 4h16v12H9l-5 4z"/>,
    plus: <path d="M12 5v14M5 12h14"/>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
    sidebar: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></>,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
    refresh: <><path d="M20 10a8 8 0 1 0 0 5M20 4v6h-6"/></>,
    settings: <><path d="m9 3-1 3-3 1-2 5 2 5 3 1 1 3h6l1-3 3-1 2-5-2-5-3-1-1-3z"/><circle cx="12" cy="12" r="3"/></>,
    arrow: <path d="M12 20V4m-6 6 6-6 6 6"/>,
    chevron: <path d="m9 5 7 7-7 7"/>,
    more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
    model: <><rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4M9 9h6v6H9z"/></>,
    rss: <><path d="M4 4a16 16 0 0 1 16 16M4 10a10 10 0 0 1 10 10"/><circle cx="5" cy="19" r="1"/></>,
    memory: <><path d="M8 3a4 4 0 0 0-4 4v2a4 4 0 0 0 0 6v2a4 4 0 0 0 8 0V7a4 4 0 0 0-4-4zm8 0a4 4 0 0 1 4 4v2a4 4 0 0 1 0 6v2a4 4 0 0 1-8 0V7a4 4 0 0 1 4-4z"/><path d="M4 10h4m8 4h4"/></>,
    info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.book}</svg>;
}

export function Notice({ error, notice, onClose }: { error: string; notice: string; onClose: () => void }) {
  return error || notice ? <div role={error ? 'alert' : 'status'} className={'banner' + (error ? ' error' : '')}><span>{error || notice}</span><button className="icon-button" aria-label="关闭提示" onClick={onClose}><Icon name="close" size={15}/></button></div> : null;
}

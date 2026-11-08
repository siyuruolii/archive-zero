export type Row = Record<string, any>;
declare global { interface Window { reader?: { request: (path: string, method?: string, body?: any) => Promise<any>; quit: () => void; subscribe: (branchId: string, receive: (event: Row) => void) => () => void } } }
export async function api(path: string, method = 'GET', body?: any) {
  if (window.reader) return window.reader.request(path, method, body);
  // 仅供显式本机浏览器验证，令牌从 URL hash 读取，不进入服务访问日志。
  const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  const response = await fetch(path, { method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const value = await response.json(); if (!response.ok) throw new Error(value.error || JSON.stringify(value.detail)); return value;
}


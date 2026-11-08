// 浏览器与Electron共用，只限制单帧；不限制整个长连接的累计传输量。
export async function* readSSE(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) { buffer += decoder.decode(); if (buffer.trim()) throw new Error('incomplete_event_stream'); return; }
      buffer += decoder.decode(value, { stream: true });
      for (;;) {
        const boundary = /\r?\n\r?\n/.exec(buffer);
        if (!boundary) break;
        if (boundary.index > 1024 * 1024) throw new Error('event_frame_too_large');
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const data = frame.split(/\r?\n/).filter(l => l.startsWith('data:')).map(l => l.slice(5).replace(/^ /, '')).join('\n');
        if (data) {
          const event = JSON.parse(data);
          if (!event || typeof event !== 'object' || typeof event.type !== 'string') throw new Error('invalid_event');
          yield event;
        }
      }
      if (buffer.length > 1024 * 1024) throw new Error('event_frame_too_large');
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

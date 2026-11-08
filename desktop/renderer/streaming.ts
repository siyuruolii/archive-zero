import { readSSE } from '../shared/sse.mjs';

type Row = Record<string, any>;

export function subscribeBranch(branch: string, receive: (event: Row) => void): () => void {
  return subscribeEvents(branch, `/api/branches/${encodeURIComponent(branch)}/events`, receive);
}

export function subscribeWorkspace(receive: (event: Row) => void): () => void {
  return subscribeEvents('workspace', '/api/workspace/events', receive);
}

export function subscribeDeepRead(id: string, receive: (event: Row) => void): () => void {
  return subscribeEvents('deep:' + id, `/api/topic-materials/${encodeURIComponent(id)}/deep-read/events`, receive);
}

function subscribeEvents(branch: string, path: string, receive: (event: Row) => void): () => void {
  if (window.reader?.subscribe) return window.reader.subscribe(branch, receive);
  const controller = new AbortController();
  const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  (async () => {
    try {
      const response = await fetch(path, { headers: { Authorization: 'Bearer ' + token }, signal: controller.signal });
      if (!response.ok || !response.body) throw new Error('stream_unavailable');
      if (!controller.signal.aborted) receive({ type: 'transport_open' });
      for await (const event of readSSE(response.body)) if (!controller.signal.aborted) receive(event);
      if (!controller.signal.aborted) receive({ type: 'transport_end' });
    } catch { if (!controller.signal.aborted) receive({ type: 'transport_error', error: 'stream_disconnected' }); }
  })();
  return () => controller.abort();
}

export function applyRunEvent(previous: Row | null, event: Row): Row | null {
  if (event.type === 'snapshot' && 'data' in event) {
    const next = event.data;
    if (!next) return previous;
    if (previous && previous.run_id === next.run_id && previous.sequence > next.sequence) return previous;
    return next;
  }
  if (!event.run_id || (previous && previous.run_id === event.run_id && previous.sequence >= event.sequence)) return previous;
  const next: Row = previous && previous.run_id === event.run_id ? { ...previous } : { run_id: event.run_id, draft: '', answer_draft: '', phase: 'running' };
  next.sequence = event.sequence;
  if (event.type === 'text_delta') next.draft = ((next.draft || '') + event.text).slice(-32000);
  if (event.type === 'answer_delta') next.answer_draft = (next.answer_draft || '') + event.text;
  if (event.type === 'snapshot') Object.assign(next, { draft: event.draft || '', answer_draft: event.answer_draft || '', phase: event.phase });
  if (event.type === 'telemetry') next.metrics = event;
  if (event.type === 'activities') next.activities = event.activities;
  if (event.type === 'status') {
    if (event.phase) next.phase = event.phase;
    if ('error' in event) next.error = event.error;
    if (event.message) next.status_message = event.message;
  }
  return next;
}

export function mergeBranch(previous: Row, incoming: Row): Row {
  const old = previous.run, next = incoming.run;
  return { ...incoming, run: old && next && old.run_id === next.run_id && old.sequence > next.sequence ? old : next };
}

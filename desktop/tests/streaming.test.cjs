const { test } = require('node:test');
const assert = require('node:assert/strict');

async function collect(text, size = 1) {
  const { readSSE } = await import('../shared/sse.mjs');
  const bytes = new TextEncoder().encode(text);
  const body = new ReadableStream({ start(controller) { for (let i = 0; i < bytes.length; i += size) controller.enqueue(bytes.slice(i, i + size)); controller.close(); } });
  const events = []; for await (const event of readSSE(body)) events.push(event); return events;
}

test('SSE preserves split UTF8, emoji, multiline data and heartbeats', async () => {
  const text = ': heartbeat\r\n\r\ndata: {"type":"answer_delta","text":"中文😀\u2028内容"}\r\n\r\ndata: {"type":"snapshot",\ndata: "data":null}\n\n';
  assert.deepEqual(await collect(text), [{ type: 'answer_delta', text: '中文😀\u2028内容' }, { type: 'snapshot', data: null }]);
});
test('SSE incomplete and oversized frames fail explicitly', async () => {
  await assert.rejects(collect('data: {"type":"x"}'), /incomplete/);
  await assert.rejects(collect('x'.repeat(1024 * 1024 + 1), 65536), /too_large/);
});
test('closing SSE iterator cancels its body reader', async () => {
  const { readSSE } = await import('../shared/sse.mjs');
  let closed = false;
  const body = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('data: {"type":"x"}\n\n')); }, cancel() { closed = true; } });
  for await (const event of readSSE(body)) { assert.equal(event.type, 'x'); break; }
  assert.equal(closed, true);
});
test('run reducer deduplicates deltas and restores a complete overflow snapshot', async () => {
  const { applyRunEvent, mergeBranch } = await import('../renderer/streaming.ts');
  let run = applyRunEvent(null, { type: 'snapshot', data: { run_id: 'one', sequence: 1, phase: 'running', answer_draft: '' } });
  const event = { type: 'answer_delta', run_id: 'one', sequence: 2, text: '第一段' };
  run = applyRunEvent(run, event); run = applyRunEvent(run, event);
  assert.equal(run.answer_draft, '第一段');
  run = applyRunEvent(run, { type: 'snapshot', data: { ...run, sequence: 10, answer_draft: '完整回答' } });
  assert.equal(applyRunEvent(run, event).answer_draft, '完整回答');
  const merged = mergeBranch({ run }, { entries: ['saved'], run: { ...run, sequence: 3, answer_draft: '旧快照' } });
  assert.equal(merged.run.answer_draft, '完整回答'); assert.deepEqual(merged.entries, ['saved']);
  assert.equal(applyRunEvent(run, { type: 'snapshot', run_id: 'one', sequence: 11, draft: '', answer_draft: '', phase: 'running' }).answer_draft, '');
});

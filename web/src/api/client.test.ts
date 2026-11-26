import { afterEach, describe, expect, it, vi } from 'vitest';
import type { StreamEvent } from '../types';
import { api, replayTurn, submitChat } from './client';

const encoder = new TextEncoder();

const frame = (event: StreamEvent) =>
  `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;

function join(...parts: Uint8Array[]) {
  const total = parts.reduce((size, part) => size + part.length, 0);
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    merged.set(part, offset);
    offset += part.length;
  }
  return merged;
}

function sseResponse(...chunks: (string | Uint8Array)[]) {
  let index = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (index >= chunks.length) return { done: true, value: undefined };
          const chunk = chunks[index++];
          return {
            done: false,
            value: typeof chunk === 'string' ? encoder.encode(chunk) : chunk,
          };
        },
        releaseLock: () => {},
      }),
    },
  } as unknown as Response;
}

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as unknown as Response;
}

afterEach(() => vi.unstubAllGlobals());

describe('chat client', () => {
  it('parses multiple frames and keeps UTF-8 characters split across chunks', async () => {
    const first = frame({ seq: 1, type: 'start', turn_id: 7 });
    const second = frame({ seq: 2, type: 'content_delta', text: '你好' });
    const third = frame({ seq: 3, type: 'done', text: '你好' });
    const bytes = encoder.encode(second);
    // 从最后一个多字节字符中间切分，验证 stream:true 能将其完整还原。
    const split = encoder.encode(second.slice(0, second.indexOf('好'))).length + 1;
    const fetch = vi.fn(async () => sseResponse(
      first,
      bytes.slice(0, split),
      join(bytes.slice(split), encoder.encode(third)),
    ));
    vi.stubGlobal('fetch', fetch);
    const { signal } = new AbortController();
    const events: StreamEvent[] = [];
    await replayTurn(7, 0, (event) => events.push(event), signal);
    expect(fetch).toHaveBeenCalledWith('/api/chat/turns/7?after=0', expect.objectContaining({ signal }));
    expect(events.map((item) => item.type)).toEqual(['start', 'content_delta', 'done']);
    expect(events[1].text).toBe('你好');
  });

  it('ignores keep-alive comments and malformed frames', async () => {
    vi.stubGlobal('fetch', async () => sseResponse(
      ': keep-alive\n\n' + frame({ seq: 1, type: 'start', turn_id: 1 }),
      'data: {not-json}\n\n' + frame({ seq: 2, type: 'done', text: 'ok' }),
    ));
    const events: StreamEvent[] = [];
    await replayTurn(1, 0, (event) => events.push(event));
    expect(events.map((item) => item.seq)).toEqual([1, 2]);
  });

  it('posts a message and returns the receipt for replay', async () => {
    const receipt = { session_id: 'chat', turn_id: 7, text: 'hi' };
    const fetch = vi.fn().mockResolvedValue(jsonResponse(receipt));
    vi.stubGlobal('fetch', fetch);
    const body = { message: 'hi', session_id: 'chat', submission_id: 'submission' };
    await expect(submitChat(body)).resolves.toEqual(receipt);
    expect(fetch).toHaveBeenCalledWith('/api/chat?receipt=1', expect.objectContaining({
      method: 'POST', body: JSON.stringify(body),
    }));
  });

  it('surfaces structured API errors', async () => {
    vi.stubGlobal('fetch', async () => jsonResponse(
      { error: { code: 'busy', message: 'A reply is still generating' } }, 409,
    ));
    await expect(submitChat({ message: 'hi', session_id: null }))
      .rejects.toMatchObject({ code: 'busy', status: 409, message: 'A reply is still generating' });
    await expect(replayTurn(7, 0, () => {}))
      .rejects.toMatchObject({ code: 'busy', status: 409, message: 'A reply is still generating' });
  });

  it('posts the turn id when cancelling', async () => {
    let body = '';
    vi.stubGlobal('fetch', async (_url: string, init?: RequestInit) => {
      body = String(init?.body || '');
      return jsonResponse({ cancelled: true, turn_id: 7, active_turn_id: 7 });
    });
    const result = await api.cancelTurn(7);
    expect(body).toContain('"turn_id":7');
    expect(result.cancelled).toBe(true);
  });
});

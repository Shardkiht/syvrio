import { afterEach, describe, expect, it, vi } from 'vitest';
import type { StreamEvent } from '../types';
import { api, streamChat } from './client';

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

describe('SSE client', () => {
  it('parses multiple frames and keeps UTF-8 characters split across chunks', async () => {
    const first = frame({ seq: 1, type: 'start', turn_id: 7 });
    const second = frame({ seq: 2, type: 'content_delta', text: '你好' });
    const third = frame({ seq: 3, type: 'done', text: '你好' });
    const bytes = encoder.encode(second);
    // Cut inside the last multi-byte character; stream:true must reassemble it.
    const split = bytes.length - 4;
    vi.stubGlobal('fetch', async () => sseResponse(
      first,
      bytes.slice(0, split),
      join(bytes.slice(split), encoder.encode(third)),
    ));
    const events: StreamEvent[] = [];
    await streamChat({ message: 'hi', session_id: null }, (event) => events.push(event));
    expect(events.map((item) => item.type)).toEqual(['start', 'content_delta', 'done']);
    expect(events[1].text).toBe('你好');
  });

  it('ignores keep-alive comments and malformed frames', async () => {
    vi.stubGlobal('fetch', async () => sseResponse(
      ': keep-alive\n\n' + frame({ seq: 1, type: 'start', turn_id: 1 }),
      'data: {not-json}\n\n' + frame({ seq: 2, type: 'done', text: 'ok' }),
    ));
    const events: StreamEvent[] = [];
    await streamChat({ message: 'hi', session_id: null }, (event) => events.push(event));
    expect(events.map((item) => item.seq)).toEqual([1, 2]);
  });

  it('surfaces structured API errors', async () => {
    vi.stubGlobal('fetch', async () => jsonResponse(
      { error: { code: 'busy', message: 'A reply is still generating' } }, 409,
    ));
    await expect(streamChat({ message: 'hi', session_id: null }, () => {}))
      .rejects.toThrow('A reply is still generating');
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

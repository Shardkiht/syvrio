import { describe, expect, it } from 'vitest';
import type { HistoryResponse, StreamEvent } from '../types';
import { addInfo, initialChatState, loadHistory, reduceChat, startTurn, toggleSegment } from './chat';

const event = (type: StreamEvent['type'], fields: Partial<StreamEvent> = {}): StreamEvent => ({
  seq: 1,
  type,
  ...fields,
});

const segmentsOf = (state: ReturnType<typeof reduceChat>) => {
  const message = [...state.messages].reverse().find((item) => item.role === 'assistant');
  return message && message.role === 'assistant' ? message.segments : [];
};

const textOf = (state: ReturnType<typeof reduceChat>) =>
  segmentsOf(state)
    .filter((segment) => segment.kind === 'text')
    .map((segment) => (segment.kind === 'text' ? segment.content : ''))
    .join('');

describe('chat reducer', () => {
  it('keeps one live turn when a command result is inserted mid-stream', () => {
    let state = startTurn(initialChatState, 'long answer', null);
    state = reduceChat(state, event('content_delta', { text: 'partial ' }));
    state = addInfo(state, '/status\nGenerating: yes');
    state = reduceChat(state, event('content_delta', { text: 'rest' }));
    state = reduceChat(state, event('done', { text: 'partial rest' }));
    expect(state.messages.filter((item) => item.role === 'assistant')).toHaveLength(1);
    expect(textOf(state)).toBe('partial rest');
    expect(state.busy).toBe(false);
    expect(state.messages.some((item) => item.role === 'info')).toBe(true);
  });

  it('starts a turn with a user bubble and a live assistant message', () => {
    const state = startTurn(initialChatState, 'hello', null);
    expect(state.busy).toBe(true);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[0]).toMatchObject({ role: 'user', content: 'hello' });
  });

  it('streams content deltas in order and folds thinking when the answer starts', () => {
    let state = startTurn(initialChatState, 'hi', 's-1');
    state = reduceChat(state, event('reasoning_delta', { text: 'line one\n', phase: 1 }));
    state = reduceChat(state, event('reasoning_delta', { text: 'line two\n', phase: 1 }));
    state = reduceChat(state, event('reasoning_end', { text: 'line one\nline two\n', phase: 1, seconds: 1.5 }));
    state = reduceChat(state, event('content_delta', { text: 'Hello ' }));
    state = reduceChat(state, event('content_delta', { text: 'world' }));
    state = reduceChat(state, event('done', { text: 'Hello world' }));
    expect(textOf(state)).toBe('Hello world');
    expect(state.busy).toBe(false);
    const thinking = segmentsOf(state).find((segment) => segment.kind === 'thinking');
    expect(thinking).toMatchObject({ collapsed: true, done: true, seconds: 1.5 });
  });

  it('keeps reasoning from different tool rounds in separate phases', () => {
    let state = startTurn(initialChatState, 'check', null);
    state = reduceChat(state, event('progress', { text: 'Waiting for model' }));
    state = reduceChat(state, event('reasoning_delta', { text: 'first thought', phase: 1 }));
    state = reduceChat(state, event('tool_start', { name: 'recall' }));
    state = reduceChat(state, event('tool_end', { name: 'recall', text: '1 fact' }));
    state = reduceChat(state, event('reasoning_delta', { text: 'second thought', phase: 2 }));
    state = reduceChat(state, event('content_delta', { text: 'answer' }));
    state = reduceChat(state, event('done', { text: 'answer' }));

    const segments = segmentsOf(state);
    expect(segments.map((segment) => segment.kind)).toEqual(['thinking', 'tool', 'thinking', 'text']);
    const [first, tool, second] = segments;
    expect(first).toMatchObject({ kind: 'thinking', phase: 1, text: 'first thought' });
    expect(tool).toMatchObject({ kind: 'tool', name: 'recall', done: true, display: '1 fact' });
    expect(second).toMatchObject({ kind: 'thinking', phase: 2, text: 'second thought' });
  });

  it('pairs parallel tool starts with results in call order', () => {
    let state = startTurn(initialChatState, 'search', null);
    state = reduceChat(state, event('tool_start', { name: 'search' }));
    state = reduceChat(state, event('tool_start', { name: 'search' }));
    state = reduceChat(state, event('tool_end', { name: 'search', text: 'first result' }));
    state = reduceChat(state, event('tool_end', { name: 'search', text: 'second result' }));
    const tools = segmentsOf(state).filter((segment) => segment.kind === 'tool');
    expect(tools).toHaveLength(2);
    expect(tools[0]).toMatchObject({ done: true, display: 'first result' });
    expect(tools[1]).toMatchObject({ done: true, display: 'second result' });
  });

  it('keeps live thinking folded while it streams', () => {
    let state = startTurn(initialChatState, 'hi', null);
    state = reduceChat(state, event('reasoning_delta', { text: 'thinking', phase: 1 }));
    expect(segmentsOf(state)[0]).toMatchObject({ collapsed: true, expanded: false });
    state = reduceChat(state, event('content_delta', { text: 'answer' }));
    expect(segmentsOf(state)[0]).toMatchObject({ collapsed: true });
  });

  it('toggles a thinking segment open and closed', () => {
    let state = startTurn(initialChatState, 'hi', null);
    state = reduceChat(state, event('reasoning_end', { text: 'full text', phase: 1 }));
    const id = segmentsOf(state)[0].id;
    state = toggleSegment(state, id);
    expect(segmentsOf(state)[0]).toMatchObject({ expanded: true });
    state = toggleSegment(state, id);
    expect(segmentsOf(state)[0]).toMatchObject({ expanded: false });
  });

  it('labels a failed tool call from the stream', () => {
    let state = startTurn(initialChatState, 'hi', null);
    state = reduceChat(state, event('tool_start', { name: 'fetch_web' }));
    state = reduceChat(state, event('tool_end', { name: 'fetch_web', text: 'timeout', ok: false }));
    const [tool] = segmentsOf(state);
    expect(tool).toMatchObject({
      kind: 'tool',
      done: true,
      ok: false,
      display: 'timeout',
      expanded: false,
    });
    // Tool rows share the same fold path as reasoning.
    state = toggleSegment(state, tool.id);
    expect(segmentsOf(state)[0]).toMatchObject({ expanded: true });
  });

  it('marks cancelled and failed turns without losing partial output', () => {
    let cancelled = startTurn(initialChatState, 'hi', null);
    cancelled = reduceChat(cancelled, event('content_delta', { text: 'half' }));
    cancelled = reduceChat(cancelled, event('cancelled'));
    expect(cancelled.busy).toBe(false);
    expect(textOf(cancelled)).toBe('half');
    expect(cancelled.messages[cancelled.messages.length - 1]).toMatchObject({ interrupted: true });

    let failed = startTurn(initialChatState, 'hi', null);
    failed = reduceChat(failed, event('content_delta', { text: 'partial' }));
    failed = reduceChat(failed, event('failed', { message: 'provider error' }));
    expect(failed.busy).toBe(false);
    expect(failed.error).toBe('provider error');
    expect(textOf(failed)).toBe('partial');
  });

  it('restores history with reasoning collapsed by default', () => {
    const history: HistoryResponse = {
      session_id: 's-1',
      title: 'Earlier',
      has_earlier: false,
      summary: null,
      current: true,
      items: [
        { id: 'u1', role: 'user', content: 'question', at: '', reasoning: null,
          tool_calls: [], tool_name: null, tool_ok: null, interrupted: false },
        { id: 'a1', role: 'assistant', content: 'answer', at: '',
          reasoning: { text: 'old thought', seconds: 2 }, tool_calls: [{ id: 'c1', name: 'recall' }],
          tool_name: null, tool_ok: null, interrupted: false },
      ],
    };
    const state = loadHistory(initialChatState, history);
    expect(state.sessionId).toBe('s-1');
    expect(state.messages).toHaveLength(2);
    const assistant = state.messages[1];
    expect(assistant.role).toBe('assistant');
    if (assistant.role === 'assistant') {
      expect(assistant.segments[0]).toMatchObject({ kind: 'thinking', collapsed: true, done: true });
      expect(assistant.segments[1]).toMatchObject({ kind: 'tool', name: 'recall', done: true });
      expect(assistant.segments[2]).toMatchObject({ kind: 'text', content: 'answer' });
    }
  });

  it('attaches a tool result to its call instead of a duplicate row', () => {
    const state = loadHistory(initialChatState, {
      session_id: 's-3',
      title: 'Tools',
      has_earlier: false,
      summary: null,
      current: true,
      items: [
        { id: 'a1', role: 'assistant', content: 'answer', at: '', reasoning: null,
          tool_calls: [{ id: 'c1', name: 'fetch_web' }], tool_name: null, tool_ok: null,
          interrupted: false },
        { id: 't1', role: 'tool', content: 'page text', at: '', reasoning: null,
          tool_calls: [], tool_name: 'fetch_web', tool_ok: true, interrupted: false },
      ],
    });
    expect(state.messages).toHaveLength(1);
    const assistant = state.messages[0];
    expect(assistant.role).toBe('assistant');
    if (assistant.role === 'assistant') {
      expect(assistant.segments[0]).toMatchObject({
        kind: 'tool',
        name: 'fetch_web',
        display: 'page text',
        ok: true,
      });
      expect(assistant.segments[1]).toMatchObject({ kind: 'text', content: 'answer' });
    }
  });

  it('matches tool results by name when the journal order differs', () => {
    const state = loadHistory(initialChatState, {
      session_id: 's-4',
      title: 'Tools',
      has_earlier: false,
      summary: null,
      current: true,
      items: [
        { id: 'a1', role: 'assistant', content: 'answer', at: '', reasoning: null,
          tool_calls: [{ id: 'c1', name: 'search' }, { id: 'c2', name: 'fetch_web' }],
          tool_name: null, tool_ok: null, interrupted: false },
        { id: 't1', role: 'tool', content: 'page text', at: '', reasoning: null,
          tool_calls: [], tool_name: 'fetch_web', tool_ok: true, interrupted: false },
        { id: 't2', role: 'tool', content: 'hits', at: '', reasoning: null,
          tool_calls: [], tool_name: 'search', tool_ok: false, interrupted: false },
      ],
    });
    const assistant = state.messages[0];
    if (assistant.role !== 'assistant') throw new Error('expected an assistant turn');
    expect(assistant.segments.filter((segment) => segment.kind === 'tool')).toMatchObject([
      { name: 'search', display: 'hits', ok: false },
      { name: 'fetch_web', display: 'page text', ok: true },
    ]);
  });

  it('marks a compacted summary as folded so it stays collapsed', () => {
    const state = loadHistory(initialChatState, {
      session_id: 's-2',
      title: 'Long chat',
      has_earlier: true,
      summary: 'user asked about tea; assistant explained oxidation',
      current: true,
      items: [],
    });
    const folded = state.messages.filter(
      (message) => message.role === 'info' && message.folded,
    );
    expect(folded).toHaveLength(1);
    expect(folded[0]).toMatchObject({
      role: 'info',
      folded: true,
      title: 'Summarized context',
      content: 'user asked about tea; assistant explained oxidation',
    });
  });

  it('never mutates segment objects shared with a previous state', () => {
    const first = reduceChat(startTurn(initialChatState, 'hi', null), event('content_delta', { text: 'a' }));
    const snapshot = segmentsOf(first).map((segment) => ({ ...segment }));
    const second = reduceChat(first, event('content_delta', { text: 'b' }));
    expect(textOf(second)).toBe('ab');
    expect(textOf(first)).toBe('a');
    expect(segmentsOf(first).map((segment) => ({ ...segment }))).toEqual(snapshot);
  });

  it('marks a transport-interrupted failed turn as interrupted', () => {
    let state = startTurn(initialChatState, 'hi', null);
    state = reduceChat(state, event('content_delta', { text: 'half' }));
    state = reduceChat(state, event('failed', { message: '连接中断', interrupted: true }));
    expect(state.busy).toBe(false);
    expect(textOf(state)).toBe('half');
    expect(state.messages[state.messages.length - 1]).toMatchObject({ interrupted: true });

    let provider = startTurn(initialChatState, 'hi', null);
    provider = reduceChat(provider, event('failed', { message: 'provider error' }));
    expect(provider.messages[provider.messages.length - 1]).toMatchObject({ interrupted: false });
  });
});

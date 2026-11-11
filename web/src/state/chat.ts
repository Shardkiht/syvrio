import type { HistoryResponse, StreamEvent } from '../types';

/** Ordered segments keep Thinking / Tool / Result / Thinking / Answer intact. */
export type Segment =
  | { kind: 'text'; id: string; content: string }
  | {
      kind: 'thinking';
      id: string;
      phase: number;
      text: string;
      done: boolean;
      collapsed: boolean;
      expanded: boolean;
      seconds: number | null;
    }
  | {
      kind: 'tool';
      id: string;
      name: string;
      done: boolean;
      display: string;
      /** null while the call is still running or when the backend sent no state. */
      ok: boolean | null;
      expanded: boolean;
    }
  | { kind: 'event'; id: string; text: string };

export type ChatMessage =
  | { id: string; role: 'user'; content: string; at?: string }
  | {
      id: string;
      role: 'assistant';
      segments: Segment[];
      streaming: boolean;
      interrupted: boolean;
      at?: string;
    }
  | { id: string; role: 'info'; content: string; at?: string; folded?: boolean; title?: string };

export interface ChatState {
  messages: ChatMessage[];
  busy: boolean;
  turnId: number | null;
  sessionId: string | null;
  error: string | null;
  progress: string;
}

let counter = 0;
const nextId = (prefix: string) => `${prefix}-${++counter}`;

export const initialChatState: ChatState = {
  messages: [],
  busy: false,
  turnId: null,
  sessionId: null,
  error: null,
  progress: '',
};

export function startTurn(state: ChatState, text: string, sessionId: string | null): ChatState {
  return {
    ...state,
    messages: [
      ...state.messages,
      { id: nextId('u'), role: 'user', content: text },
      { id: nextId('a'), role: 'assistant', segments: [], streaming: true, interrupted: false },
    ],
    busy: true,
    turnId: null,
    sessionId,
    error: null,
    progress: 'Connecting',
  };
}

/** The live assistant turn may no longer be last: a command result (info)
 *  can be inserted while the reply streams. Search backwards instead of
 *  assuming the tail, otherwise deltas split into a second assistant bubble. */
function liveIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === 'assistant' && message.streaming) return index;
  }
  return -1;
}

/** Locate (or create) the live assistant message for the current turn. */
function withLive(state: ChatState): { messages: ChatMessage[]; index: number } {
  const messages = [...state.messages];
  const index = liveIndex(messages);
  if (index >= 0) return { messages, index };
  messages.push({
    id: nextId('a'),
    role: 'assistant',
    segments: [],
    streaming: true,
    interrupted: false,
  });
  return { messages, index: messages.length - 1 };
}

function finishLive(
  state: ChatState,
  patch: (live: Extract<ChatMessage, { role: 'assistant' }>) => Extract<ChatMessage, { role: 'assistant' }>,
): ChatState {
  const messages = [...state.messages];
  const index = liveIndex(messages);
  if (index >= 0) {
    const live = messages[index];
    if (live.role === 'assistant') messages[index] = patch(live);
  }
  return { ...state, messages, busy: false, turnId: null, progress: '' };
}

function patchLive(
  state: ChatState,
  update: (segments: Segment[]) => Segment[],
): ChatState {
  const { messages, index } = withLive(state);
  const live = messages[index];
  if (live.role !== 'assistant') return state;
  // Copy each segment so the reducer never mutates objects shared with prior state.
  messages[index] = { ...live, segments: update(live.segments.map((segment) => ({ ...segment }))) };
  return { ...state, messages, sessionId: state.sessionId };
}

function activeThinking(segments: Segment[], phase: number): Segment | undefined {
  return [...segments].reverse().find(
    (segment) => segment.kind === 'thinking' && segment.phase === phase && !segment.done,
  ) as Segment | undefined;
}

export function reduceChat(state: ChatState, event: StreamEvent): ChatState {
  const sessionId = event.session_id ?? state.sessionId;
  const base: ChatState = { ...state, sessionId };

  switch (event.type) {
    case 'start':
      return { ...base, turnId: event.turn_id ?? null, progress: 'Thinking' };

    case 'progress':
      return { ...base, progress: event.text || '' };

    case 'reasoning_delta': {
      const phase = event.phase ?? 0;
      return patchLive(base, (segments) => {
        let found = activeThinking(segments, phase);
        if (!found) {
          const created: Segment = {
            kind: 'thinking',
            id: nextId('t'),
            phase,
            text: '',
            done: false,
            // Reasoning never streams into view; the summary row is the only
            // signal until the user chooses to expand it.
            collapsed: true,
            expanded: false,
            seconds: null,
          };
          segments.push(created);
          found = created;
        }
        const target = found as Extract<Segment, { kind: 'thinking' }>;
        target.text += event.text || '';
        return segments;
      });
    }

    case 'reasoning_end': {
      const phase = event.phase ?? 0;
      return patchLive(base, (segments) => {
        const existing = activeThinking(segments, phase);
        if (existing) {
          const target = existing as Extract<Segment, { kind: 'thinking' }>;
          target.text = event.text || target.text;
          target.done = true;
          target.collapsed = true;
          target.seconds = event.seconds ?? null;
        } else {
          segments.push({
            kind: 'thinking',
            id: nextId('t'),
            phase,
            text: event.text || '',
            done: true,
            collapsed: true,
            expanded: false,
            seconds: event.seconds ?? null,
          });
        }
        return segments;
      });
    }

    case 'content_delta': {
      return patchLive(base, (segments) => {
        for (const segment of segments) {
          if (segment.kind === 'thinking' && !segment.collapsed) segment.collapsed = true;
        }
        const last = segments[segments.length - 1];
        if (last && last.kind === 'text') last.content += event.text || '';
        else segments.push({ kind: 'text', id: nextId('c'), content: event.text || '' });
        return segments;
      });
    }

    case 'tool_start':
      return patchLive(base, (segments) => [
        ...segments,
        {
          kind: 'tool',
          id: nextId('tool'),
          name: event.name || 'tool',
          done: false,
          display: '',
          ok: null,
          expanded: false,
        },
      ]);

    case 'tool_end':
      return patchLive(base, (segments) => {
        const index = segments.findIndex(
          (segment) => segment.kind === 'tool' && !segment.done && segment.name === (event.name || ''),
        );
        const fallback = segments.findIndex((segment) => segment.kind === 'tool' && !segment.done);
        const target = index >= 0 ? index : fallback;
        if (target >= 0) {
          segments[target] = {
            ...(segments[target] as Extract<Segment, { kind: 'tool' }>),
            done: true,
            display: event.text || '',
            // The stream carries the outcome so a failure reads as Failed right
            // away instead of waiting for a history reload.
            ok: event.ok ?? null,
          };
        }
        return segments;
      });

    case 'event':
      return patchLive(base, (segments) => [
        ...segments,
        { kind: 'event', id: nextId('e'), text: event.text || '' },
      ]);

    case 'done': {
      const next = patchLive(base, (segments) => {
        const lastText = [...segments].reverse().find((segment) => segment.kind === 'text');
        if (lastText && lastText.kind === 'text') lastText.content = event.text || lastText.content;
        else if (event.text) segments.push({ kind: 'text', id: nextId('c'), content: event.text });
        for (const segment of segments) {
          if (segment.kind === 'thinking') segment.collapsed = true;
        }
        return segments;
      });
      return finishLive(next, (live) => ({ ...live, streaming: false }));
    }

    case 'cancelled': {
      const next = patchLive(base, (segments) => segments);
      return finishLive(next, (live) => ({ ...live, streaming: false, interrupted: true }));
    }

    case 'failed': {
      const next = patchLive(base, (segments) => segments);
      return {
        ...finishLive(next, (live) => ({ ...live, streaming: false, interrupted: Boolean(event.interrupted) })),
        error: event.message || 'Request failed',
      };
    }

    default:
      return base;
  }
}

/** Fold state lives on the segment itself, so tool rows and reasoning share one path. */
export function toggleSegment(state: ChatState, segmentId: string): ChatState {
  return {
    ...state,
    messages: state.messages.map((message) => {
      if (message.role !== 'assistant') return message;
      return {
        ...message,
        segments: message.segments.map((segment) => {
          if (segment.id !== segmentId) return segment;
          if (segment.kind === 'thinking') return { ...segment, expanded: !segment.expanded };
          if (segment.kind === 'tool') return { ...segment, expanded: !segment.expanded };
          return segment;
        }),
      };
    }),
  };
}

/** Command results are informational messages, not assistant turns. */
export function addInfo(state: ChatState, content: string): ChatState {
  return {
    ...state,
    messages: [...state.messages, { id: nextId('i'), role: 'info', content }],
  };
}

export function loadHistory(state: ChatState, history: HistoryResponse): ChatState {
  const messages: ChatMessage[] = [];
  if (history.has_earlier) {
    messages.push({ id: nextId('i'), role: 'info', content: '更早的记录已省略' });
  }
  if (history.summary) {
    messages.push({
      id: nextId('i'),
      role: 'info',
      content: history.summary,
      folded: true,
      title: 'Summarized context',
    });
  }
  for (let index = 0; index < history.items.length; index += 1) {
    const item = history.items[index];
    if (item.role === 'user') {
      messages.push({ id: item.id || nextId('u'), role: 'user', content: item.content, at: item.at });
    } else if (item.role === 'assistant') {
      // Tool results are separate journal records that follow their call. Fold
      // them back into the call so the transcript shows one entry per call
      // instead of a pill plus a duplicate "name ok" line.
      const results: (typeof history.items)[number][] = [];
      let look = index + 1;
      while (look < history.items.length && history.items[look].role === 'tool') {
        results.push(history.items[look]);
        look += 1;
      }
      index = look - 1;
      const segments: Segment[] = [];
      if (item.reasoning?.text) {
        segments.push({
          kind: 'thinking',
          id: nextId('t'),
          phase: 0,
          text: item.reasoning.text,
          done: true,
          collapsed: true,
          expanded: false,
          seconds: item.reasoning.seconds,
        });
      }
      // Match by tool name first: a missing or extra record must not shift
      // every later result onto the wrong call. Order is only the fallback.
      const used = new Set<number>();
      const takeResult = (name: string) => {
        const named = results.findIndex(
          (row, at) => !used.has(at) && (row.tool_name || '') === name,
        );
        const chosen = named >= 0 ? named : results.findIndex((_, at) => !used.has(at));
        if (chosen < 0) return null;
        used.add(chosen);
        return results[chosen];
      };
      item.tool_calls.forEach((call) => {
        const result = takeResult(call.name);
        segments.push({
          kind: 'tool',
          id: nextId('tool'),
          name: call.name,
          done: true,
          display: result?.content || '',
          ok: result ? Boolean(result.tool_ok) : null,
          expanded: false,
        });
      });
      if (item.content) {
        segments.push({ kind: 'text', id: nextId('c'), content: item.content });
      }
      messages.push({
        id: item.id || nextId('a'),
        role: 'assistant',
        segments,
        streaming: false,
        interrupted: item.interrupted,
        at: item.at,
      });
    } else if (item.role === 'tool') {
      // A tool record with no preceding assistant turn: keep it, folded.
      messages.push({
        id: item.id || nextId('i'),
        role: 'info',
        content: item.content || `${item.tool_name || 'tool'} ${item.tool_ok ? 'ok' : 'failed'}`,
        folded: true,
        title: `${item.tool_name || 'tool'} ${item.tool_ok ? 'ok' : 'failed'}`,
        at: item.at,
      });
    } else {
      messages.push({
        id: item.id || nextId('i'),
        role: 'info',
        content: item.content,
        folded: true,
        title: 'Summarized context',
        at: item.at,
      });
    }
  }
  return {
    ...state,
    messages,
    busy: false,
    turnId: null,
    sessionId: history.session_id,
    error: null,
    progress: '',
  };
}

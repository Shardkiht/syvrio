import type { HistoryItem, HistoryResponse, StreamEvent } from '../../types';

/** 有序片段保留思考、工具、结果、再次思考及回答的顺序。 */
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
      detail?: { sessionId: string; recordId: string; length: number; archived: boolean };
      /** 调用仍在运行或后端未发送状态时为 null。 */
      ok: boolean | null;
      expanded: boolean;
    }
  | { kind: 'event'; id: string; text: string };

export type ChatMessage =
  | { id: string; savedId?: string; role: 'user'; content: string; at?: string; attachments?: HistoryItem['attachments'] }
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

export function startTurn(state: ChatState, text: string, sessionId: string | null, savedId?: string): ChatState {
  return {
    ...state,
    messages: [
      ...state.messages,
      { id: nextId('u'), role: 'user', content: text, savedId },
      { id: nextId('a'), role: 'assistant', segments: [], streaming: true, interrupted: false },
    ],
    busy: true,
    turnId: null,
    sessionId,
    error: null,
    progress: 'Connecting',
  };
}

/** 流式回复期间可能插入命令结果，当前助手回合未必在列表末尾。
 *  从末尾向前查找，避免将后续增量拆成第二个助手气泡。
 *   */
function liveIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === 'assistant' && message.streaming) return index;
  }
  return -1;
}

/** 定位当前回合正在生成的助手消息，若缺失则创建。 */
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
  // 复制各片段，避免状态更新函数修改与先前状态共用的对象。
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

    case 'message_saved': {
      if (!event.message_id) return base;
      const user = [...base.messages].reverse().find((message) => message.role === 'user');
      if (!user) return base;
      // 历史记录可能已含新消息，但回执尚未带回其持久化 ID。
      const saved = base.messages.findIndex(m => m !== user && m.role === 'user' && m.savedId === event.message_id);
      const messages = saved < 0 ? base.messages : [
        ...base.messages.slice(0, saved), ...base.messages.slice(base.messages.indexOf(user)),
      ];
      return { ...base, messages: messages.map((message) =>
        message === user ? { ...user, savedId: event.message_id, attachments: event.attachments ?? user.attachments } : message) };
    }

    case 'message_inserted': {
      const completed = finishLive(base, (live) => ({ ...live, streaming: false }));
      return { ...startTurn(completed, event.text || '', sessionId, event.message_id), turnId: base.turnId };
    }

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
            // 思考流默认只更新摘要行，用户主动展开后才会看到正文。
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
            // 事件流携带调用结果，客户端可立即显示失败，无需等待重载历史。
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

/** 将折叠状态保存在片段上，使工具行和思考区共用同一套逻辑。 */
export function toggleSegment(state: ChatState, segmentId: string): ChatState {
  return {
    ...state,
    messages: state.messages.map((message) => {
      if (message.role !== 'assistant' || !message.segments.some(segment => segment.id === segmentId)) return message;
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

/** 命令结果按提示消息展示，不创建助手回合。 */
export function addInfo(state: ChatState, content: string): ChatState {
  return {
    ...state,
    messages: [...state.messages, { id: nextId('i'), role: 'info', content }],
  };
}

export function loadHistory(state: ChatState, history: HistoryResponse, archived = false): ChatState {
  const toolSegment = (name: string, row?: HistoryItem): Extract<Segment, { kind: 'tool' }> => ({
    kind: 'tool', id: nextId('tool'), name, done: true,
    display: row?.content ?? '', ok: row ? Boolean(row.tool_ok) : null, expanded: false,
    detail: row?.tool_content_truncated && row.id && row.tool_content_length
      ? { sessionId: history.session_id, recordId: row.id, length: row.tool_content_length, archived }
      : undefined,
  });
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
      messages.push({ id: item.id || nextId('u'), savedId: item.id, role: 'user', content: item.content, at: item.at, attachments: item.attachments });
    } else if (item.role === 'assistant') {
      // 工具结果在日志中单独记录，位于对应调用之后。将它们合并回调用，
      // 确保每次调用只显示一个条目，避免在摘要之外重复显示结果行。
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
      const remaining = [...results];
      for (const call of item.tool_calls) {
        let match = remaining.findIndex(row => row.tool_call_id && row.tool_call_id === call.id);
        // 缺少调用 ID 的历史记录先按名称、再按记录顺序配对。
        if (match < 0) match = remaining.findIndex(row => !row.tool_call_id && row.tool_name === call.name);
        if (match < 0) match = remaining.findIndex(row => !row.tool_call_id);
        const result = match >= 0 ? remaining.splice(match, 1)[0] : undefined;
        segments.push(toolSegment(call.name, result));
      }
      for (const row of remaining) segments.push(toolSegment(row.tool_name || 'tool', row));
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
    } else if (item.role === 'error') {
      messages.push({ id: item.id, role: 'info', content: `Reply failed: ${item.content}`, at: item.at });
    } else if (item.role === 'tool') {
      messages.push({
        id: item.id || nextId('a'), role: 'assistant',
        segments: [toolSegment(item.tool_name || 'tool', item)],
        streaming: false, interrupted: false, at: item.at,
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

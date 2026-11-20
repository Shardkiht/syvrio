import type {
  CancelResponse,
  ChatRequest,
  CommandCatalogResponse,
  CommandRequest,
  CommandResponse,
  ContextUsage,
  ActionResponse,
  CreateSessionResponse,
  DeleteSessionResponse,
  HealthResponse,
  ProviderActionRequest,
  ProviderListResponse,
  HistoryResponse,
  MessageDraft,
  ModelSearchResponse,
  SessionDeletionPreview,
  SessionListResponse,
  StreamEvent,
  SwitchSessionResponse,
} from '../types';

export class ApiError extends Error {
  constructor(message: string, readonly code?: string) { super(message); }
}

async function readError(response: Response): Promise<ApiError> {
  const fallback = `Request failed (${response.status})`;
  try {
    const payload = await response.json();
    return new ApiError(payload?.error?.message || fallback, payload?.error?.code);
  } catch {
    return new ApiError(fallback);
  }
}

/** A transport failure (as opposed to an HTTP error response). */
export class NetworkError extends Error {}

// Each document, including a duplicated tab, owns its navigation and shares
// the conversation only when it selects the same session as another client.
const viewId = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte =>
  byte.toString(16).padStart(2, '0')).join('');

async function fetchChecked(path: string, init?: RequestInit): Promise<Response> {
  try {
    const headers = new Headers(init?.headers);
    headers.set('X-Syvrio-View', viewId);
    return await fetch(path, { ...init, headers });
  } catch (error) {
    throw new NetworkError(error instanceof Error ? error.message : 'Network unavailable');
  }
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchChecked(path, init);
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

const jsonHeaders = { 'Content-Type': 'application/json' };

function messagePath(sessionId: string, messageId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}`;
}

export function attachmentUrl(sessionId: string, messageId: string, fileId: string): string {
  return `${messagePath(sessionId, messageId)}/attachments/${encodeURIComponent(fileId)}`;
}

export const api = {
  searchModels: (query: string, refresh: boolean, signal: AbortSignal) => {
    const params = new URLSearchParams({ q: query });
    if (refresh) params.set('refresh', 'true');
    return jsonRequest<ModelSearchResponse>('/api/settings/models?' + params, { signal, cache: 'no-store' });
  },
  editDraft: (sessionId: string, messageId: string) =>
    jsonRequest<MessageDraft>(messagePath(sessionId, messageId) + '/draft'),
  async readUpload(sessionId: string, item: MessageDraft['attachments'][number]): Promise<File> {
    const response = await fetchChecked(attachmentUrl(sessionId, item.message_id, item.id));
    if (!response.ok) throw new Error(`${item.name}: ${(await readError(response)).message}`);
    return new File([await response.blob()], item.name, { type: item.media_type });
  },
  disconnect: () => fetchChecked('/api/view/disconnect', { method: 'POST', keepalive: true }),
  health: () => jsonRequest<HealthResponse>('/api/health'),

  contextUsage: (sessionId: string | null, signal: AbortSignal) =>
    jsonRequest<ContextUsage>(
      '/api/context-usage' + (sessionId === null ? '' : '?' + new URLSearchParams({ session_id: sessionId })),
      { signal, cache: 'no-store' },
    ),

  listSessions: () => jsonRequest<SessionListResponse>('/api/sessions?limit=100'),

  createSession: () =>
    jsonRequest<CreateSessionResponse>('/api/sessions', { method: 'POST', headers: jsonHeaders }),

  switchSession: (sessionId: string) =>
    jsonRequest<SwitchSessionResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/switch`, {
      method: 'POST',
      headers: jsonHeaders,
    }),

  deletionPreview: (sessionId: string) =>
    jsonRequest<SessionDeletionPreview>(`/api/sessions/${encodeURIComponent(sessionId)}/deletion-preview`, { cache: 'no-store' }),
  deleteSession: (sessionId: string, revision: string) =>
    jsonRequest<DeleteSessionResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}?confirmed=true`
        + (revision ? '&pending_revision=' + encodeURIComponent(revision) : ''),
      { method: 'DELETE', headers: jsonHeaders },
    ),

  history: (sessionId: string) =>
    jsonRequest<HistoryResponse>(`/api/sessions/${encodeURIComponent(sessionId)}`),

  insertMessage: async (turnId: number, sessionId: string | null, message: string) => {
    const response = await fetchChecked('/api/chat/insert', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({ turn_id: turnId, session_id: sessionId, message }),
    });
    if (!response.ok) throw await readError(response);
  },

  cancelTurn: (turnId: number) =>
    jsonRequest<CancelResponse>('/api/chat/cancel', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({ turn_id: turnId }),
    }),

  providers: () => jsonRequest<ProviderListResponse>('/api/settings/providers'),

  providerAction: (provider: string, body: ProviderActionRequest) =>
    jsonRequest<ActionResponse>(`/api/settings/providers/${encodeURIComponent(provider)}`, {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),

  commands: () => jsonRequest<CommandCatalogResponse>('/api/commands'),

  runCommand: (body: CommandRequest) =>
    jsonRequest<CommandResponse>('/api/commands', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),
};

/**
 * POST + SSE with the Fetch API. Frames are split on blank lines; TextDecoder
 * with stream:true keeps multi-byte UTF-8 characters intact across chunks.
 */
export interface TurnReceipt {
  turn_id: number;
  session_id: string;
  text: string;
  message_id?: string | null;
}
export interface QueueState {
  session_id: string | null;
  active: TurnReceipt | null;
  waiting: TurnReceipt[];
  paused: boolean;
  revision: number;
}

function postMessage(body: ChatRequest, files: readonly File[], receipt: boolean, signal?: AbortSignal) {
  const form = files.length ? new FormData() : null;
  if (form) {
    form.append('message', body.message);
    if (body.session_id) form.append('session_id', body.session_id);
    for (const file of files) form.append('files', file);
  }
  const path = form ? '/api/chat/attachments' : '/api/chat';
  return fetchChecked(path + (receipt ? '?receipt=1' : ''), {
    method: 'POST', headers: form ? undefined : jsonHeaders, body: form ?? JSON.stringify(body), signal,
  });
}

export async function submitChat(body: ChatRequest, files: readonly File[] = []): Promise<TurnReceipt> {
  const response = await postMessage(body, files, true);
  if (!response.ok) throw await readError(response);
  return response.json();
}

export const queueState = () => jsonRequest<QueueState>('/api/chat/queue', { cache: 'no-store' });
export async function removeQueuedTurn(id: number) {
  const response = await fetchChecked(`/api/chat/queue/${id}`, { method: 'DELETE' });
  if (!response.ok) throw await readError(response);
}

export async function replayTurn(turn: number, after: number, onEvent: (event: StreamEvent) => void, signal?: AbortSignal) {
  return consumeStream(await fetchChecked(`/api/chat/turns/${turn}?after=${after}`, { signal }), onEvent);
}

export async function streamChat(
  body: ChatRequest, onEvent: (event: StreamEvent) => void, signal?: AbortSignal, files: readonly File[] = [],
) {
  return consumeStream(await postMessage(body, files, false, signal), onEvent);
}

async function consumeStream(response: Response, onEvent: (event: StreamEvent) => void) {
  if (!response.ok) throw await readError(response);
  if (!response.body) throw new Error('Streaming is unavailable in this browser.');

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  const consume = (frame: string) => {
    const data = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n');
    if (!data) return; // comment / keep-alive frame
    try {
      onEvent(JSON.parse(data) as StreamEvent);
    } catch {
      // A malformed frame is ignored; the terminal event still closes the turn.
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        consume(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) consume(buffer);
  } finally {
    reader.releaseLock();
  }
}

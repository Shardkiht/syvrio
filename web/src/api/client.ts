import type {
  CancelResponse,
  ChatRequest,
  CommandCatalogResponse,
  CommandRequest,
  CommandResponse,
  ActionResponse,
  CreateSessionResponse,
  DeleteSessionResponse,
  HealthResponse,
  ProviderActionRequest,
  ProviderListResponse,
  HistoryResponse,
  SessionListResponse,
  StreamEvent,
  SwitchSessionResponse,
} from '../types';

async function readError(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    return payload?.error?.message || `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}

/** A transport failure (as opposed to an HTTP error response). */
export class NetworkError extends Error {}

async function fetchChecked(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(path, init);
  } catch (error) {
    throw new NetworkError(error instanceof Error ? error.message : 'Network unavailable');
  }
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchChecked(path, init);
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as T;
}

const jsonHeaders = { 'Content-Type': 'application/json' };

export const api = {
  health: () => jsonRequest<HealthResponse>('/api/health'),

  listSessions: () => jsonRequest<SessionListResponse>('/api/sessions?limit=100'),

  createSession: () =>
    jsonRequest<CreateSessionResponse>('/api/sessions', { method: 'POST', headers: jsonHeaders }),

  switchSession: (sessionId: string) =>
    jsonRequest<SwitchSessionResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/switch`, {
      method: 'POST',
      headers: jsonHeaders,
    }),

  deleteSession: (sessionId: string) =>
    jsonRequest<DeleteSessionResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}?confirmed=true`,
      { method: 'DELETE', headers: jsonHeaders },
    ),

  history: (sessionId: string) =>
    jsonRequest<HistoryResponse>(`/api/sessions/${encodeURIComponent(sessionId)}`),

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
export async function streamChat(
  body: ChatRequest,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetchChecked('/api/chat', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw new Error(await readError(response));
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

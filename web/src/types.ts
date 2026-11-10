/** Mirrors src/syvrio/app/web/models.py. Keep these in sync with the Schema. */

export type EventType =
  | 'start'
  | 'reasoning_delta'
  | 'reasoning_end'
  | 'content_delta'
  | 'tool_start'
  | 'tool_end'
  | 'event'
  | 'progress'
  | 'done'
  | 'cancelled'
  | 'failed';

export interface StreamEvent {
  seq: number;
  type: EventType;
  turn_id?: number | null;
  session_id?: string | null;
  text?: string;
  name?: string | null;
  seconds?: number | null;
  phase?: number | null;
  steps?: number | null;
  estimated_tokens?: number | null;
  message?: string | null;
  interrupted?: boolean;
}

export interface HealthResponse {
  status: string;
  version: string;
  provider: string;
  model: string;
  busy: boolean;
  active_turn_id: number | null;
  single_client: boolean;
  static_ready: boolean;
}

export interface SessionInfo {
  id: string;
  title: string;
  messages: number;
  started_at: string;
  updated_at: string;
  last_message_at?: string;
  current: boolean;
}

export interface SessionListResponse {
  items: SessionInfo[];
}

export interface DeleteSessionResponse {
  session_id: string;
  deleted: boolean;
}

export interface ProviderInfo {
  key: string;
  label: string;
  configured: boolean;
  current: boolean;
  endpoint: string;
}

export interface ProviderListResponse {
  items: ProviderInfo[];
}

export type ProviderAction = 'set_key' | 'update_key' | 'set_endpoint' | 'remove';

export interface ProviderActionRequest {
  action: ProviderAction;
  api_key?: string;
  endpoint?: string;
  confirmed?: boolean;
}

export interface ActionResponse {
  ok: boolean;
  message: string;
  refresh_health: boolean;
}

export interface CreateSessionResponse {
  session_id: string;
  title: string;
}

export interface SwitchSessionResponse {
  session_id: string;
  title: string;
}

export interface ToolCallInfo {
  id: string;
  name: string;
}

export interface ReasoningBlock {
  text: string;
  seconds: number | null;
}

export interface HistoryItem {
  id: string;
  role: 'user' | 'assistant' | 'tool' | 'summary';
  content: string;
  at: string;
  reasoning: ReasoningBlock | null;
  tool_calls: ToolCallInfo[];
  tool_name: string | null;
  tool_ok: boolean | null;
  interrupted: boolean;
}

export interface HistoryResponse {
  session_id: string;
  title: string;
  items: HistoryItem[];
  summary: string | null;
  has_earlier: boolean;
  current: boolean;
}

export interface ApiErrorResponse {
  error: { code: string; message: string };
}

export interface ChatRequest {
  message: string;
  session_id: string | null;
}

export interface CancelResponse {
  cancelled: boolean;
  turn_id: number;
  active_turn_id: number | null;
}

export interface CommandInfo {
  name: string;
  usage: string;
  description: string;
  support: 'server' | 'client' | 'unsupported' | string;
  intent: string;
  confirm: boolean;
  note: string;
  shortcut: string;
  subcommands: { name: string; description: string }[];
}

export interface CommandCatalogResponse {
  items: CommandInfo[];
}

export interface CommandRequest {
  line: string;
  session_id: string | null;
  confirmed: boolean;
}

export type CommandStatus =
  | 'ok'
  | 'client'
  | 'unsupported'
  | 'unknown'
  | 'busy'
  | 'confirm_required'
  | 'error';

export interface CommandResponse {
  status: CommandStatus;
  command: string;
  intent: string;
  message: string;
  data: Record<string, unknown> | null;
  session_id: string | null;
  refresh_sessions: boolean;
  refresh_history: boolean;
}

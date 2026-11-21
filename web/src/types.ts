/** Mirrors src/syvrio/backend/api/models.py. Keep these in sync with the Schema. */

export type EventType =
  | 'start'
  | 'message_saved'
  | 'message_inserted'
  | 'reasoning_delta'
  | 'reasoning_end'
  | 'content_delta'
  | 'tool_start'
  | 'tool_end'
  | 'event'
  | 'progress'
  | 'compaction'
  | 'done'
  | 'cancelled'
  | 'failed';

export interface StreamEvent {
  seq: number;
  type: EventType;
  turn_id?: number | null;
  session_id?: string | null;
  message_id?: string;
  text?: string;
  name?: string | null;
  seconds?: number | null;
  phase?: number | null;
  steps?: number | null;
  estimated_tokens?: number | null;
  message?: string | null;
  interrupted?: boolean;
  /** Tool outcome: false marks a failed call, null when unknown. */
  ok?: boolean | null;
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

export type CompactionPhase = 'running' | 'done' | 'failed';

export interface ContextUsage {
  session_id: string | null;
  used: number;
  limit: number;
  compact_threshold: number;
}

export interface SessionInfo {
  id: string;
  title: string;
  title_pending?: boolean;
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

export interface SessionDeletionPreview {
  pending: Array<{ id: string; text: string }>;
  revision: string;
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

export interface ModelSearchResponse {
  items: Array<{ provider: string; model: string; label: string; current: boolean }>;
  unavailable_providers: string[];
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
  // Null until the first message: a new chat only opens the empty view.
  session_id: string | null;
  title: string;
  draft?: boolean;
}

export interface SwitchSessionResponse {
  session_id: string;
  title: string;
  history_version?: string;
  history?: HistoryResponse | null;
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
  attachments?: { id: string; name: string; size: number; media_type: string }[];
  id: string;
  role: 'user' | 'assistant' | 'tool' | 'summary' | 'error';
  content: string;
  at: string;
  reasoning: ReasoningBlock | null;
  tool_calls: ToolCallInfo[];
  tool_name: string | null;
  tool_ok: boolean | null;
  interrupted: boolean;
}

export interface MessageDraft {
  text: string;
  attachments: { id: string; message_id: string; name: string; media_type: string }[];
}

export interface HistoryResponse {
  /** Version accompanying a full HTTP history read. */
  history_version?: string;
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
  submission_id?: string;
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
  expected_message_id?: string;
  pending_text?: string;
  inference_text?: string;
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

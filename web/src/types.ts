/** 与 src/syvrio/backend/api/models.py 的协议结构保持一致。 */

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
  attachments?: HistoryItem['attachments'] | null;
  text?: string;
  name?: string | null;
  seconds?: number | null;
  phase?: number | null;
  steps?: number | null;
  estimated_tokens?: number | null;
  message?: string | null;
  interrupted?: boolean;
  /** 工具调用结果为 false 表示失败，null 表示结果未知。 */
  ok?: boolean | null;
}

export interface AttachmentPolicy {
  extensions: string[];
  max_files: number;
  max_file_bytes: number;
  max_batch_bytes: number;
  max_name_length: number;
}

export interface HealthResponse {
  attachment_policy: AttachmentPolicy;
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
  compaction_trigger_tokens?: number;
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
  can_remove_key: boolean;
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

export type ProviderAction = 'set_key' | 'update_key' | 'set_endpoint' | 'remove_key';

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
  // 新聊天先打开空视图，发送首条消息前会话 ID 为 null。
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
  tool_call_id?: string | null;
  tool_content_length?: number | null;
  tool_content_truncated?: boolean;
  interrupted: boolean;
}

export interface MessageDraft {
  text: string;
  attachments: { id: string; message_id: string; name: string; media_type: string }[];
}

export interface HistoryResponse {
  /** 读取完整 HTTP 会话历史时一并返回的修订版本。 */
  history_version?: string;
  session_id: string;
  title: string;
  items: HistoryItem[];
  summary: string | null;
  has_earlier: boolean;
  earlier_cursor?: number | null;
  current: boolean;
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

export interface UpdateStatus {
  state: 'idle' | 'running' | 'completed' | 'failed';
  message: string;
  progress?: { stage: string; message: string; downloaded?: number | null; total?: number | null } | null;
}

export interface UpdateCheck extends UpdateStatus {
  current: string;
  latest: string;
  available: boolean;
  managed: boolean;
}

export interface ChatSearchHit {
  session_id: string;
  title: string;
  message_id: string;
  role: 'title' | 'assistant' | 'user';
  excerpt: string;
}

export interface PendingEvidence {
  state: 'found' | 'unrecorded' | 'unavailable';
  source: { title: string; content: string; at: string; archived: boolean } | null;
  related: Array<{ id: string; text: string | null }>;
}

/** 共享服务端命令目录的补全和命令面板逻辑，无外部副作用。 */
import { formatLocalTimestamp } from '../time';
import type { CommandInfo, CommandResponse } from '../types';

export interface Suggestion {
  key: string;
  insert: string;
  label: string;
  description: string;
  isSubcommand: boolean;
}

/** 按前缀及子命令补全，正在输入参数时隐藏候选。 */
export function suggestionsFor(value: string, catalog: CommandInfo[]): Suggestion[] {
  if (!value.startsWith('/')) return [];
  const inner = value.slice(1);
  const space = inner.indexOf(' ');
  if (space < 0) {
    const prefix = inner.toLowerCase();
    return catalog
      .filter((command) => command.name.startsWith(prefix))
      .map((command) => ({
        key: command.name,
        insert: `/${command.name}`,
        label: `/${command.name}`,
        description: command.description,
        isSubcommand: false,
      }));
  }
  const head = inner.slice(0, space).toLowerCase();
  const rest = inner.slice(space + 1);
  if (rest.includes(' ')) return [];
  const command = catalog.find((item) => item.name === head);
  if (!command) return [];
  const prefix = rest.toLowerCase();
  return command.subcommands
    .filter((sub) => sub.name.startsWith(prefix))
    .map((sub) => ({
      key: `${command.name} ${sub.name}`,
      insert: `/${command.name} ${sub.name} `,
      label: `/${command.name} ${sub.name}`,
      description: sub.description,
      isSubcommand: true,
    }));
}

export function searchCommands(query: string, catalog: CommandInfo[]): CommandInfo[] {
  const needle = query.trim().toLowerCase().replace(/^\//, '');
  if (!needle) return catalog;
  return catalog.filter((command) =>
    command.name.includes(needle)
    || command.usage.toLowerCase().includes(needle)
    || command.description.toLowerCase().includes(needle));
}

export function isCommandLine(value: string): boolean {
  return value.trimStart().startsWith('/');
}

const THINKING_ORDER = ['auto', '', 'none', 'disabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'enabled'];

export function thinkingOptions(values: unknown[]): string[] {
  const available = values.filter((value): value is string => typeof value === 'string');
  return [
    ...THINKING_ORDER.filter((value) => available.includes(value)),
    ...available.filter((value) => !THINKING_ORDER.includes(value)),
  ];
}

export interface PendingCandidate {
  id: string;
  text: string;
  topic: string;
  question?: string;
  related_ids?: string[];
  status?: string;
}

export interface ReviewPanel {
  kind: 'pending' | 'inferences';
  title: string;
  rows: PendingCandidate[];
  loading?: boolean;
  loadError?: string;
  count: number;
  edit?: { id: string; text?: string };
}

export interface ResultAction {
  label: string;
  line: string;
  field?: string;
  primary?: boolean;
  refresh?: boolean;
  saveShortcut?: boolean;
}

export interface ResultPanel {
  title: string;
  message: string;
  groups?: { title: string; fields: { label: string; value: string }[] }[];
  reading?: boolean;
  metadata?: string[];
  feedback?: 'success' | 'notice' | 'error';
  actions?: ResultAction[];
  rename?: boolean;
  draft?: string;
}

export interface TopicListItem {
  topic: string;
  title: string;
  entries?: number;
  updated?: string;
}

export interface TopicDocument {
  topic: string;
  entries: { id: string; text: string; status: string }[];
}

export interface TopicsPanel {
  kind: 'topics';
  topics?: TopicListItem[];
  document?: TopicDocument;
}

export interface DreamPanelState {
  kind: 'dream';
  state: string;
  retryAt?: number;
  failures?: number;
  errorKind?: string;
}

export type CommandPanel = ReviewPanel | ResultPanel | TopicsPanel | DreamPanelState | { kind: 'sessions' };
export type PendingAction = 'approve' | 'reject' | 'edit';

export function panelFor(result: CommandResponse): CommandPanel | null {
  const data = result.data;
  if (!data) return null;

  if (result.status === 'ok' && result.intent === 'topics') {
    if (typeof data.topic === 'string' && Array.isArray(data.entries)) {
      return { kind: 'topics', document: { topic: data.topic, entries: data.entries as TopicDocument['entries'] } };
    }
    if (Array.isArray(data.topics)) return { kind: 'topics', topics: data.topics as TopicListItem[] };
    // 旧版常驻服务返回同样的主题键，但不带用于展示的元数据。
    if (Array.isArray(data.names)) return {
      kind: 'topics',
      topics: data.names.filter((name): name is string => typeof name === 'string').map(topic => ({ topic, title: '' })),
    };
  }

  if ((data.kind === 'pending' || data.kind === 'inferences') && Array.isArray(data.rows)) {
    return {
      kind: data.kind,
      title: data.kind === 'pending' ? 'Pending review' : 'Inference review',
      rows: data.rows as PendingCandidate[],
      count: typeof data.count === 'number' ? data.count : data.rows.length,
      edit: typeof data.edit_id === 'string'
        ? { id: data.edit_id, text: typeof data.edit_text === 'string' ? data.edit_text : undefined }
        : undefined,
    };
  }

  if (result.status === 'ok' && data.kind === 'dream' && Array.isArray(data.rows)) {
    return {
      kind: 'dream', state: String(data.state ?? ''),
      retryAt: typeof data.retry_at === 'number' ? data.retry_at : undefined,
      failures: typeof data.failures === 'number' ? data.failures : undefined,
      errorKind: typeof data.error_type === 'string' ? data.error_type : undefined,
    };
  }

  return null;
}

function localTimestamp(value: string): string {
  try { return formatLocalTimestamp(value); }
  catch { return value; }
}

function profileStatus(data: CommandResponse['data']): string {
  if (typeof data?.generated_at === 'string' && data.generated_at) {
    return 'Last generated ' + localTimestamp(data.generated_at);
  }
  return typeof data?.status === 'string' ? data.status : '';
}

function commandHeading(result: CommandResponse): string {
  const heading: Record<string, string> = {
    ok: `/${result.command}`,
    client: `/${result.command}`,
    unsupported: `/${result.command} is not available on the Web`,
    unknown: result.message,
    busy: 'Busy',
    confirm_required: 'Confirmation required',
    error: `/${result.command} failed`,
  };
  return heading[result.status] ?? result.status;
}

function commandBody(result: CommandResponse): string {
  if (result.status === 'unknown') return '';
  let message = result.message;
  if (result.intent === 'profile') {
    const status = profileStatus(result.data);
    if (status) message = status + '\n\n' + message;
  }
  const timestamps = result.intent === 'git_status' ? result.data?.timestamps : null;
  if (timestamps && typeof timestamps === 'object') {
    message = message.split('\n').map((line) => {
      const key = line.split(': ', 1)[0];
      const stamp: unknown = Object.hasOwn(timestamps, key)
        ? (timestamps as Record<string, unknown>)[key]
        : null;
      return typeof stamp === 'string' ? key + ': ' + localTimestamp(stamp) : line;
    }).join('\n');
  }
  return message;
}

export function commandMessage(result: CommandResponse): string {
  const message = commandBody(result);
  return message ? commandHeading(result) + '\n' + message : commandHeading(result);
}

const GROUP_FIELDS: Record<'status' | 'git_status', [string, [string, string][]][]> = {
  status: [
    ['Configuration', [['provider', 'Provider'], ['model', 'Model'], ['retrieval', 'Retrieval'],
      ['context_window', 'Context window']]],
    ['Conversation', [['session', 'Session'], ['generating', 'Generating'], ['pending_review', 'Pending review']]],
  ],
  git_status: [
    ['Repository', [['initialized', 'Initialized'], ['branch', 'Branch'], ['remote', 'Remote']]],
    ['Changes', [['modified', 'Modified files'], ['unpushed', 'Unpushed commits'], ['behind', 'Commits behind']]],
    ['Activity', [['last_commit_at', 'Last commit'], ['last_push_at', 'Last push']]],
  ],
};

function statusGroups(intent: keyof typeof GROUP_FIELDS, source: unknown) {
  if (!source || typeof source !== 'object') return [];
  const data = source as Record<string, unknown>;
  return GROUP_FIELDS[intent].map(([title, keys]) => {
    const fields: { label: string; value: string }[] = [];
    for (const [key, label] of keys) {
      const value = data[key];
      if (typeof value === 'string') fields.push({ label, value: key.endsWith('_at') ? localTimestamp(value) : value });
      else if (typeof value === 'number') fields.push({ label, value: value.toLocaleString() });
      else if (typeof value === 'boolean') fields.push({ label, value: value ? 'Yes' : 'No' });
      else if (key === 'session' && value === null) fields.push({ label, value: 'Not created yet' });
    }
    return { title, fields };
  }).filter((group) => group.fields.length > 0);
}

const RESULT_TITLES: Record<string, string> = {
  status: 'Session status', git_status: 'Git status', git: 'Git synchronization',
  summary: 'Session summary', profile: 'Memory profile', model: 'Model', think: 'Reasoning',
  context: 'Context window', compact: 'Context compaction', rename: 'Session title',
  new_session: 'New session', pending: 'Pending review', inferences: 'Inference review',
  dream: 'Dream', undo: 'Undo', leave: 'Conversation view',
};

const RESULT_ACTIONS: Record<string, ResultAction[]> = {
  status: [
    { label: 'Providers', line: '/setup', field: 'Provider' },
    { label: 'Choose model', line: '/model', field: 'Model' },
    { label: 'Set window', line: '/context', field: 'Context window' },
    { label: 'Browse sessions', line: '/sessions', field: 'Session' },
    { label: 'Review pending', line: '/pending', field: 'Pending review' },
    { label: 'Refresh', line: '/status', refresh: true },
  ],
  git_status: [
    { label: 'Refresh', line: '/git', refresh: true },
    { label: 'Synchronize', line: '/git sync', primary: true, saveShortcut: true },
  ],
  git: [{ label: 'Git status', line: '/git' }],
  summary: [{ label: 'Refresh summary', line: '/summary', refresh: true }],
  compact: [{ label: 'View summary', line: '/summary' }],
  profile: [
    { label: 'Dream status', line: '/dream status' },
    { label: 'Run Dream', line: '/dream', primary: true },
  ],
  dream: [{ label: 'Dream status', line: '/dream status' }],
  model: [{ label: 'Choose model', line: '/model' }, { label: 'Refresh models', line: '/model refresh' }],
  think: [{ label: 'Choose effort', line: '/think' }],
  context: [{ label: 'Set window', line: '/context' }],
  rename: [{ label: 'Browse sessions', line: '/sessions' }],
  pending: [{ label: 'Review pending', line: '/pending' }],
  inferences: [{ label: 'Review inferences', line: '/inferences' }],
  new_session: [{ label: 'Browse sessions', line: '/sessions' }],
  leave: [{ label: 'Browse sessions', line: '/sessions' }],
};

export function readPanelTitle(line: string): string | undefined {
  switch (line.trim().toLowerCase()) {
    case '/status': return RESULT_TITLES.status;
    case '/summary': return RESULT_TITLES.summary;
    case '/git': return RESULT_TITLES.git_status;
    case '/profile': return RESULT_TITLES.profile;
    case '/dream status': return RESULT_TITLES.dream;
    case '/dream': return RESULT_TITLES.dream;
    case '/rename': return 'Rename session';
    case '/sessions': return 'Sessions';
    case '/update': return 'Software update';
    case '/setup': return 'Providers';
    case '/help': return 'Commands';
    default: return undefined;
  }
}

export function panelMatches(panel: CommandPanel, title: string) {
  return ('title' in panel && panel.title === title) || ('kind' in panel && panel.kind === 'dream' && title === 'Dream');
}

export function canOpenDuringSelection(line: string) {
  return !!readPanelTitle(line) || ['/pending', '/inferences', '/topics', '/model', '/think', '/context'].includes(line.trim().toLowerCase());
}

// 写入失败后可以打开只读视图，重新写入须由用户再次选择。
const READ_VIEWS = new Set([
  '/status', '/setup', '/model', '/think', '/context', '/pending', '/inferences', '/sessions',
  '/summary', '/git', '/dream status',
]);

export function resultPanelFor(result: CommandResponse): ResultPanel {
  if (result.status === 'unknown') return { title: result.message, message: '', feedback: 'notice' };
  const success = result.status === 'ok' || result.status === 'client';
  const panel: ResultPanel = {
    title: success && Object.hasOwn(RESULT_TITLES, result.intent) ? RESULT_TITLES[result.intent] : commandHeading(result),
    message: result.intent === 'git_status' ? commandBody(result) : result.message,
    feedback: success ? 'success' : result.status === 'error' ? 'error' : 'notice',
  };
  if (!success) {
    if (result.status === 'busy') panel.actions = [{ label: 'Session status', line: '/status' }];
    else if (result.status === 'error' && Object.hasOwn(RESULT_ACTIONS, result.intent)) {
      panel.actions = RESULT_ACTIONS[result.intent].filter(({ line }) => READ_VIEWS.has(line));
    }
    return panel;
  }
  if (Object.hasOwn(RESULT_ACTIONS, result.intent)) panel.actions = RESULT_ACTIONS[result.intent];
  if (result.intent === 'status' || result.intent === 'git_status') {
    panel.groups = statusGroups(result.intent, result.data?.status);
  }
  if (result.intent === 'profile') {
    panel.reading = true;
    const data = result.data ?? {};
    const status = profileStatus(data);
    const metadata = status ? [status] : [];
    if (typeof data.fact_count === 'number') metadata.push(data.fact_count === 1 ? '1 fact' : `${data.fact_count} facts`);
    if (typeof data.unique_count === 'number') metadata.push(`${data.unique_count} unique`);
    panel.metadata = metadata;
  } else if (result.intent === 'summary') {
    panel.reading = true;
    if (result.data?.can_compact === true) {
      panel.actions = [...RESULT_ACTIONS.summary, { label: 'Compact context', line: '/compact', primary: true }];
    }
  }
  return panel;
}

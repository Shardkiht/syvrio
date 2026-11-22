/** Pure completion / palette logic over the shared server command catalog. */
import { formatLocalTimestamp } from '../time';
import type { CommandInfo, CommandResponse } from '../types';

export interface Suggestion {
  key: string;
  insert: string;
  label: string;
  description: string;
  isSubcommand: boolean;
}

/** Prefix and sub-command completion. An argument in progress shows nothing. */
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
        insert: `/${command.name}${command.subcommands.length ? ' ' : ''}`,
        label: command.usage,
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

export interface OptionPanel {
  title: string;
  options: { label: string; value: string }[];
  prefix: string;
  message?: string;
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
  count: number;
  edit?: { id: string; text?: string };
}

export interface ResultPanel {
  title: string;
  message: string;
  action?: { label: string; line: string; saveShortcut?: boolean };
  rename?: boolean;
}

export type CommandPanel = OptionPanel | ReviewPanel | ResultPanel | { kind: 'sessions' };
export type PendingAction = 'approve' | 'reject' | 'edit';

/** Model / reasoning / context choices share one small clickable panel. */
export function panelFor(result: CommandResponse): CommandPanel | null {
  const data = result.data;
  if (!data) return null;

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

  // Inferences and reflection use the generic option panel.
  const rows = data.rows as { id: string; text: string }[] | undefined;
  if (Array.isArray(rows) && rows.length) {
    const kind = String(data.kind ?? '');
    if (kind === 'dream') {
      return {
        title: 'Reflection', prefix: '/dream', message: result.message,
        options: rows.map((row) => ({ label: row.text, value: row.id })),
      };
    }
  }

  if (!Array.isArray(data.options)) return null;
  if (result.intent === 'model') {
    const options = (data.options as { provider?: string; model?: string; current?: boolean }[])
      .filter((item) => item.provider && item.model)
      .map((item) => ({
        label: `${item.current ? '* ' : ''}${item.provider}/${item.model}`,
        value: `${item.provider}/${item.model}`,
      }));
    return options.length ? { title: 'Models', options, prefix: '/model' } : null;
  }
  if (result.intent === 'think') {
    const options = thinkingOptions(data.options).map((value) => ({
      label: value || 'default',
      // The backend accepts "default" as the alias for the empty option;
      // sending "/think " would trim back to list mode and reopen the panel.
      value: value || 'default',
    }));
    return options.length ? { title: 'Reasoning', options, prefix: '/think' } : null;
  }
  if (result.intent === 'context') {
    const options = (data.options as (string | number)[]).map((value) => ({
      label: String(value), value: String(value),
    }));
    return options.length ? { title: 'Context window', options, prefix: '/context' } : null;
  }
  return null;
}

export function commandMessage(result: CommandResponse): string {
  const heading: Record<string, string> = {
    ok: `/${result.command}`,
    client: `/${result.command}`,
    unsupported: `/${result.command} is not available on the Web`,
    unknown: 'Unknown command',
    busy: 'Busy',
    confirm_required: 'Confirmation required',
    error: `/${result.command} failed`,
  };
  const title = heading[result.status] ?? result.status;
  let message = result.message;
  if (result.intent === 'profile' && typeof result.data?.status === 'string') {
    const generated = result.data.generated_at;
    const status = typeof generated === 'string' && generated
      ? 'Last generated ' + formatLocalTimestamp(generated)
      : result.data.status;
    message = status + '\n\n' + message;
  }
  const timestamps = result.intent === 'git_status' ? result.data?.timestamps : null;
  if (timestamps && typeof timestamps === 'object') {
    message = message.split('\n').map((line) => {
      const key = line.split(': ', 1)[0];
      const stamp: unknown = Object.hasOwn(timestamps, key)
        ? (timestamps as Record<string, unknown>)[key]
        : null;
      return typeof stamp === 'string' ? key + ': ' + formatLocalTimestamp(stamp) : line;
    }).join('\n');
  }
  return message ? title + '\n' + message : title;
}

export function resultPanelFor(result: CommandResponse): ResultPanel {
  const [title, ...lines] = commandMessage(result).split('\n');
  const panel: ResultPanel = { title, message: lines.join('\n') };
  if (result.status === 'ok' && result.intent === 'git_status') {
    panel.action = { label: 'Synchronize', line: '/git sync', saveShortcut: true };
  } else if (result.status === 'ok' && result.intent === 'profile') {
    panel.action = { label: 'Run Dream', line: '/dream' };
  }
  return panel;
}

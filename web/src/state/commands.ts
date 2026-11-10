/** Pure completion / palette logic over the shared server command catalog. */
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

export interface CommandPanel {
  title: string;
  options: { label: string; value: string }[];
  prefix: string;
}

/** Model / reasoning / context choices share one small clickable panel. */
export function panelFor(result: CommandResponse): CommandPanel | null {
  const data = result.data;
  if (!data) return null;

  // Review screens (pending / inferences / reflection) expose their items as
  // clickable action rows, so the browser never needs a second business model.
  const rows = data.rows as { id: string; text: string }[] | undefined;
  if (Array.isArray(rows) && rows.length) {
    const kind = String(data.kind ?? '');
    const label = (prefix: string, row: { id: string; text: string }) =>
      `${prefix} · ${row.text || row.id}`.slice(0, 120);
    if (kind === 'pending') {
      return {
        title: 'Pending review', prefix: '/pending',
        options: rows.flatMap((row) => [
          { label: label('Approve', row), value: `approve ${row.id}` },
          { label: label('Reject', row), value: `reject ${row.id}` },
        ]),
      };
    }
    if (kind === 'inferences') {
      return {
        title: 'Inference review', prefix: '/inferences',
        options: rows.flatMap((row) => [
          { label: label('Confirm', row), value: `confirm ${row.id}` },
          { label: label('Reject', row), value: `reject ${row.id}` },
        ]),
      };
    }
    if (kind === 'dream') {
      return {
        title: 'Reflection', prefix: '/dream',
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
    const options = (data.options as string[]).map((value) => ({
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
  return result.message ? `${title}\n${result.message}` : title;
}

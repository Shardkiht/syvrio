import { describe, expect, it } from 'vitest';
import type { CommandInfo, CommandResponse } from '../../types';
import {
  commandMessage,
  isCommandLine,
  panelFor,
  resultPanelFor,
  searchCommands,
  suggestionsFor,
  thinkingOptions,
} from './commands';

const command = (name: string, patch: Partial<CommandInfo> = {}): CommandInfo => ({
  name,
  usage: `/${name}`,
  description: `${name} description`,
  support: 'server',
  intent: name,
  confirm: false,
  note: '',
  shortcut: '',
  subcommands: [],
  ...patch,
});

const CATALOG: CommandInfo[] = [
  command('new'),
  command('sessions', { support: 'client', intent: 'open_sessions' }),
  command('model', {
    usage: '/model [refresh]',
    subcommands: [{ name: 'refresh', description: 'Refresh model list' }],
  }),
  command('undo', { confirm: true }),
  command('import', { support: 'unsupported', note: 'Use the TUI.' }),
  command('help'),
  command('status'),
];

const result = (patch: Partial<CommandResponse> = {}): CommandResponse => ({
  status: 'ok',
  command: 'status',
  intent: 'status',
  message: 'ok',
  data: null,
  session_id: null,
  refresh_sessions: false,
  refresh_history: false,
  ...patch,
});

describe('slash completion', () => {
  it('shows every command on a bare slash and filters by prefix', () => {
    expect(suggestionsFor('/', CATALOG)).toHaveLength(CATALOG.length);
    expect(suggestionsFor('/mod', CATALOG).map((item) => item.key)).toEqual(['model']);
    expect(suggestionsFor('/zz', CATALOG)).toEqual([]);
    expect(suggestionsFor('hello', CATALOG)).toEqual([]);
  });

  it('completes sub-commands once the head has a trailing space', () => {
    const rows = suggestionsFor('/model re', CATALOG);
    expect(rows).toHaveLength(1);
    expect(rows[0].insert).toBe('/model refresh ');
    expect(rows[0].isSubcommand).toBe(true);
    expect(suggestionsFor('/model refresh extra', CATALOG)).toEqual([]);
    expect(suggestionsFor('/new ', CATALOG)).toEqual([]);
  });

  it('completes only the command name until a space is typed', () => {
    expect(suggestionsFor('/mod', CATALOG)[0]).toMatchObject({ insert: '/model', label: '/model' });
    expect(suggestionsFor('/new', CATALOG)[0].insert).toBe('/new');
  });

  it('routes every slash line to the command API', () => {
    expect(isCommandLine('/help')).toBe(true);
    expect(isCommandLine('  /help')).toBe(true);
    expect(isCommandLine('please /help')).toBe(false);
    expect(isCommandLine('')).toBe(false);
  });
});

describe('command palette', () => {
  it('searches name, usage and description without requiring the slash', () => {
    expect(searchCommands('und', CATALOG).map((item) => item.name)).toEqual(['undo']);
    expect(searchCommands('/model', CATALOG).map((item) => item.name)).toEqual(['model']);
    expect(searchCommands('description', CATALOG)).toHaveLength(CATALOG.length);
    expect(searchCommands('', CATALOG)).toEqual(CATALOG);
  });
});

describe('option panels', () => {
  it('preserves the options supplied by the backend', () => {
    expect(thinkingOptions(['auto', 42, 'vendor-level', 'enabled'])).toEqual(['auto', 'vendor-level', 'enabled']);
  });

  it('does not reopen the picker after a choice was applied', () => {
    expect(panelFor(result({ intent: 'model', data: { model_changed: true } }))).toBeNull();
    expect(panelFor(result({ intent: 'model', data: { options: [] } }))).toBeNull();
  });

  it('gives explicit feedback for unsupported, unknown, busy and errors', () => {
    expect(commandMessage(result({ status: 'unsupported', command: 'import' })))
      .toContain('not available on the Web');
    expect(commandMessage(result({ status: 'unknown', command: 'nope', message: 'Invalid command.' })))
      .toBe('Invalid command.');
    expect(commandMessage(result({ status: 'busy' }))).toContain('Busy');
    expect(commandMessage(result({ status: 'confirm_required' })))
      .toContain('Confirmation required');
    expect(commandMessage(result({ status: 'error', message: 'failed' })))
      .toContain('failed');
  });
});

it('keeps the published profile intact and displays its generation time locally', () => {
  const stamp = '2026-11-11T01:05:03+00:00';
  const body = 'A profile paragraph.\n'.repeat(125) + 'End of profile.';
  const panel = resultPanelFor(result({
    command: 'profile', intent: 'profile', message: body,
    data: { status: 'Last generated ' + stamp, generated_at: stamp },
  }));
  expect(panel.metadata?.[0]).toMatch(/^Last generated \d{4}-\d{2}-\d{2} /);
  expect(panel.metadata?.[0]).not.toContain(stamp);
  expect(panel.message).toBe(body);
  expect(panel.actions?.map(action => action.line)).toEqual(['/dream status', '/dream']);
});

it('localizes absolute timestamps in the Web Git status command', () => {
  const stamp = '2026-11-11T01:05:03+00:00';
  const message = commandMessage(result({
    command: 'git',
    intent: 'git_status',
    message: 'initialized: True\nlast_push_at: ' + stamp,
    data: { timestamps: { last_push_at: stamp } },
  }));
  expect(message).toMatch(/last_push_at: \d{4}-\d{2}-\d{2} (?:[01]\d|2[0-3]):\d{2}/);
  expect(message).toContain('initialized: True');
  expect(message).not.toContain(stamp);
  expect(message).not.toMatch(/\b(?:AM|PM)\b/);
});

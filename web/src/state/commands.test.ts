import { describe, expect, it } from 'vitest';
import type { CommandInfo, CommandResponse } from '../types';
import {
  commandMessage,
  isCommandLine,
  panelFor,
  searchCommands,
  suggestionsFor,
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

  it('adds a trailing space only when a command has children', () => {
    expect(suggestionsFor('/mod', CATALOG)[0].insert).toBe('/model ');
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
  it('turns model / reasoning / context options into clickable choices', () => {
    const models = panelFor(result({
      intent: 'model',
      data: { options: [{ provider: 'spark', model: 'x2', current: true }] },
    }));
    expect(models?.options[0]).toEqual({ label: '* spark/x2', value: 'spark/x2' });

    const thinking = panelFor(result({ intent: 'think', data: { options: ['', 'high'] } }));
    expect(thinking?.options[0]).toEqual({ label: 'default', value: 'default' });

    const context = panelFor(result({ intent: 'context', data: { options: [8000, 16000] } }));
    expect(context?.options[1]).toEqual({ label: '16000', value: '16000' });
  });

  it('does not reopen the picker after a choice was applied', () => {
    expect(panelFor(result({ intent: 'model', data: { model_changed: true } }))).toBeNull();
    expect(panelFor(result({ intent: 'model', data: { options: [] } }))).toBeNull();
  });

  it('gives explicit feedback for unsupported, unknown, busy and errors', () => {
    expect(commandMessage(result({ status: 'unsupported', command: 'import' })))
      .toContain('not available on the Web');
    expect(commandMessage(result({ status: 'unknown', command: 'nope' })))
      .toContain('Unknown command');
    expect(commandMessage(result({ status: 'busy' }))).toContain('Busy');
    expect(commandMessage(result({ status: 'confirm_required' })))
      .toContain('Confirmation required');
    expect(commandMessage(result({ status: 'error', message: 'failed' })))
      .toContain('failed');
  });
});

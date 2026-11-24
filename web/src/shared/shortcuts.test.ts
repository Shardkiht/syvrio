import { describe, expect, it } from 'vitest';
import {
  KEY_BINDINGS,
  RESERVED_KEYS,
  SHORTCUT_HELP,
  isComposing,
  matchShortcut,
  type ShortcutKeyEvent,
} from './shortcuts';

const key = (patch: Partial<ShortcutKeyEvent> = {}): ShortcutKeyEvent => ({
  key: 'a',
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  altKey: false,
  ...patch,
});

describe('browser-safe shortcut mapping', () => {
  it('never hijacks browser-reserved Ctrl+P or Ctrl+Q', () => {
    expect(matchShortcut(key({ key: 'p', ctrlKey: true }))).toBeNull();
    expect(matchShortcut(key({ key: 'q', ctrlKey: true }))).toBeNull();
    expect(RESERVED_KEYS.map((item) => item.keys).join(' ')).toContain('Ctrl+P');
    expect(RESERVED_KEYS.map((item) => item.keys).join(' ')).toContain('Ctrl+Q');
  });

  it('offers clear alternatives for the TUI Ctrl+P command palette', () => {
    expect(matchShortcut(key({ key: 'p', ctrlKey: true, shiftKey: true }))).toBe('palette');
    expect(matchShortcut(key({ key: 'p', altKey: true }))).toBe('palette');
    expect(matchShortcut(key({ key: 'P', altKey: true }))).toBe('palette');
    expect(SHORTCUT_HELP[0].note).toContain('Alt+P');
  });

  it('keeps the TUI Ctrl+B sidebar binding with an Alt fallback', () => {
    expect(matchShortcut(key({ key: 'b', ctrlKey: true }))).toBe('sidebar');
    expect(matchShortcut(key({ key: 'b', altKey: true }))).toBe('sidebar');
  });

  it('maps model choices to Alt+M and Escape to the shared handler', () => {
    expect(matchShortcut(key({ key: 'm', altKey: true }))).toBe('model');
    expect(matchShortcut(key({ key: 'Escape' }))).toBe('escape');
    expect(matchShortcut(key({ key: 'escape', shiftKey: true }))).toBeNull();
  });

  it('treats metaKey as Ctrl on macOS and keeps bindings ordered', () => {
    expect(matchShortcut(key({ key: 'b', metaKey: true }))).toBe('sidebar');
    expect(KEY_BINDINGS[0].action).toBe('palette');
    expect(KEY_BINDINGS[0].shift).toBe(true);
  });
});

describe('IME composition guard', () => {
  it('blocks shortcuts and Enter during Chinese composition', () => {
    expect(isComposing({ key: 'Enter', keyCode: 229 })).toBe(true);
    expect(isComposing({ key: 'Process' })).toBe(true);
    expect(isComposing({ key: 'Unidentified' })).toBe(true);
    expect(isComposing({ key: 'Enter', isComposing: true })).toBe(true);
    expect(isComposing({ key: 'Enter', nativeEvent: { isComposing: true } })).toBe(true);
    expect(isComposing({ key: 'Enter', keyCode: 13 })).toBe(false);
    expect(matchShortcut(key({ key: 'p', ctrlKey: true, shiftKey: true, keyCode: 229 }))).toBeNull();
    expect(matchShortcut(key({ key: 'Escape', isComposing: true }))).toBeNull();
  });
});

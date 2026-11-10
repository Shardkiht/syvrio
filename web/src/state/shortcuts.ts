/** One place for every Web shortcut and its browser-conflict fallback.
 *
 * Checked against the TUI's real bindings (chat_app.py):
 *   Ctrl+P -> command palette (Textual action_command_palette)
 *   Ctrl+B -> toggle sidebar
 *   Ctrl+Q -> quit
 *   Escape -> close overlay / cancel generation
 * Browser-reserved keys are deliberately NOT hijacked: Ctrl+P prints the page
 * and Ctrl+Q quits the browser. Each has an explicit alternative below.
 */

export type ShortcutAction = 'palette' | 'model' | 'settings' | 'sidebar' | 'escape' | 'stop';

export interface KeyBinding {
  action: ShortcutAction;
  key: string;
  ctrl?: boolean; // ctrlKey or metaKey (macOS)
  shift?: boolean;
  alt?: boolean;
  label: string;
}

/** Ordered: the more specific binding must come first. */
export const KEY_BINDINGS: KeyBinding[] = [
  { action: 'palette', key: 'p', ctrl: true, shift: true, label: 'Ctrl+Shift+P' },
  { action: 'palette', key: 'p', alt: true, label: 'Alt+P' },
  { action: 'model', key: 'm', alt: true, label: 'Alt+M' },
  { action: 'settings', key: 's', alt: true, label: 'Alt+S' },
  { action: 'sidebar', key: 'b', ctrl: true, label: 'Ctrl+B' },
  { action: 'sidebar', key: 'b', alt: true, label: 'Alt+B' },
  { action: 'escape', key: 'escape', label: 'Esc' },
];

export interface ShortcutHelp {
  keys: string;
  description: string;
  note?: string;
}

/** Keys the Web client intentionally leaves to the browser. */
export const RESERVED_KEYS: ShortcutHelp[] = [
  {
    keys: 'Ctrl+P (TUI: command palette)',
    description: 'Not hijacked: browser Print',
    note: 'Use Ctrl+Shift+P or Alt+P for the palette.',
  },
  {
    keys: 'Ctrl+Q (TUI: quit)',
    description: 'Not hijacked: browser quit',
    note: 'Use /exit to return to the entry view; it never stops the Web server.',
  },
];

export const SHORTCUT_HELP: ShortcutHelp[] = [
  {
    keys: 'Ctrl+Shift+P / Alt+P',
    description: 'Open the command palette (TUI: Ctrl+P)',
    note: 'Firefox reserves Ctrl+Shift+P for a private window; Alt+P works everywhere.',
  },
  { keys: 'Ctrl+B / Alt+B', description: 'Toggle the session sidebar (TUI: Ctrl+B)' },
  {
    keys: 'Alt+M',
    description: 'Open model choices',
    note: 'Also available via /model or the model button in the header.',
  },
  {
    keys: 'Alt+S',
    description: 'Open the settings panel',
    note: 'Also available via /setup or the Settings button. Providers and keys live here.',
  },
  { keys: 'Esc', description: 'Close overlays or stop generation' },
  { keys: '↑ / ↓', description: 'Move through command suggestions' },
  { keys: 'Tab', description: 'Complete the highlighted command' },
  { keys: 'Enter', description: 'Send, or run the highlighted command' },
  { keys: 'Shift+Enter', description: 'New line in the composer' },
  ...RESERVED_KEYS,
];

export interface ShortcutKeyEvent {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  keyCode?: number;
  isComposing?: boolean;
}

type ComposableEvent = {
  key: string;
  keyCode?: number;
  isComposing?: boolean;
  nativeEvent?: { isComposing?: boolean };
};

/** IME composition must never trigger Enter, Tab or a shortcut. */
export function isComposing(event: ComposableEvent): boolean {
  return Boolean(event.isComposing || event.nativeEvent?.isComposing)
    || event.keyCode === 229
    || event.key === 'Process'
    || event.key === 'Unidentified';
}

export function matchShortcut(event: ShortcutKeyEvent): ShortcutAction | null {
  if (isComposing(event)) return null;
  const key = event.key.toLowerCase();
  const ctrl = event.ctrlKey || event.metaKey;
  for (const binding of KEY_BINDINGS) {
    if (binding.key !== key) continue;
    if (Boolean(binding.ctrl) !== ctrl) continue;
    if (Boolean(binding.shift) !== event.shiftKey) continue;
    if (Boolean(binding.alt) !== event.altKey) continue;
    return binding.action;
  }
  return null;
}

/** 集中管理网页快捷键及与浏览器快捷键冲突时的替代键位。
 *
 * 键位与 TUI 的实际绑定核对，见 chat_app.py。
 *   Ctrl+P 在模型选项中管理供应商
 *   Ctrl+B 展开或收起侧栏
 *   Ctrl+Q 退出
 *   Escape 关闭浮层或取消生成
 * Ctrl+P 和 Ctrl+Q 留给浏览器打印页面和退出，网页端另设下列替代键位。
 */

export type ShortcutAction = 'palette' | 'model' | 'settings' | 'sidebar' | 'escape' | 'stop';

export interface KeyBinding {
  action: ShortcutAction;
  key: string;
  ctrl?: boolean; // 匹配 Ctrl 键，macOS 上也匹配 Command 键。
  shift?: boolean;
  alt?: boolean;
  label: string;
}

/** 按顺序匹配，更具体的键位必须排在前面。 */
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

/** 网页端留给浏览器自行处理的按键。 */
export const RESERVED_KEYS: ShortcutHelp[] = [
  {
    keys: 'Ctrl+P',
    description: 'Not hijacked: browser Print',
    note: 'Use Alt+P for commands or Alt+S for providers from model choices.',
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
    description: 'Open the command palette',
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
    note: 'From model choices, opens Providers directly. Also available via /setup or the Settings button.',
  },
  { keys: 'C / R / E', description: 'Approve or confirm, reject, or edit in memory review' },
  { keys: 'Ctrl+S / ⌘S', description: 'Save an edit or request Git synchronization in its dialog' },
  { keys: 'Page Up / Page Down', description: 'Scroll the selected review item' },
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

/** 输入法组词期间不能触发 Enter、Tab 或其他快捷键。 */
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

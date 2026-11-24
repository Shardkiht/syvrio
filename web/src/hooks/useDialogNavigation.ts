import { useCallback, useLayoutEffect, useRef, useState, type SetStateAction } from 'react';
import type { CommandPanel } from '../state/commands';
import type { SessionDeletionPreview, SessionInfo } from '../types';

export type DeleteTarget = SessionInfo & { preview: SessionDeletionPreview | null; notice: string };
type Settings = 'model' | 'thinking' | 'context' | 'providers' | 'about' | { section: 'model'; notice: string };
type Dialog =
  | { kind: 'panel'; panel: CommandPanel }
  | { kind: 'palette' }
  | { kind: 'settings'; section: Settings }
  | { kind: 'confirm'; line: string; message: string; parent?: CommandPanel; triggerLabel?: string }
  | { kind: 'archive'; selected?: SessionInfo }
  | { kind: 'archive-confirm'; session: SessionInfo }
  | { kind: 'delete'; target: DeleteTarget };

export function useDialogNavigation() {
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const origin = useRef<HTMLElement | null>(null);
  const restoreButton = useRef<string>();
  const open = useCallback((next: Dialog, trigger?: HTMLElement) => {
    const active = trigger ?? document.activeElement;
    if (active instanceof HTMLElement && active.closest('.app')) origin.current = active;
    setDialog(next);
  }, []);
  const close = useCallback(() => {
    if (dialog?.kind === 'delete') setDialog({ kind: 'archive', selected: dialog.target });
    else if (dialog?.kind === 'confirm' && dialog.parent) {
      restoreButton.current = dialog.triggerLabel;
      setDialog({ kind: 'panel', panel: dialog.parent });
    } else setDialog(null);
  }, [dialog]);
  const wasOpen = useRef(false);
  useLayoutEffect(() => {
    if (dialog?.kind === 'panel' && restoreButton.current) {
      const buttons = document.querySelectorAll<HTMLButtonElement>('.surface-frame:not([data-closing]) [role="dialog"] button');
      Array.from(buttons).find(button => (button.getAttribute('aria-label') || button.textContent?.trim()) === restoreButton.current)?.focus();
      restoreButton.current = undefined;
    }
    if (!dialog && wasOpen.current) {
      const target = origin.current;
      origin.current = null;
      (target?.isConnected ? target : document.querySelector<HTMLElement>('textarea[aria-label="Message"]'))?.focus();
    }
    wasOpen.current = !!dialog;
  }, [dialog]);
  const setPanel = useCallback((value: SetStateAction<CommandPanel | null>) => {
    if (value && typeof value !== 'function') {
      open({ kind: 'panel', panel: value });
      return;
    }
    setDialog(current => {
      if (current?.kind !== 'panel') return current;
      const next = typeof value === 'function' ? value(current.panel) : value;
      return next ? { kind: 'panel', panel: next } : null;
    });
  }, [open]);
  return { dialog, open, close, setPanel, setDialog };
}

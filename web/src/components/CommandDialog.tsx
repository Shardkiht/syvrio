import { useEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { isComposing } from '../state/shortcuts';

export function CommandDialog({
  title, className, testId, role = 'dialog', overlayClass = '', onClose, onKeyDown, children,
}: {
  title: string;
  className: string;
  testId?: string;
  role?: 'dialog' | 'alertdialog';
  overlayClass?: string;
  onClose: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = dialog.current;
    if (node && !node.contains(document.activeElement)) node.focus();
  }, []);
  return (
    <div className={`overlay ${overlayClass}`} data-testid={testId} onClick={onClose}>
      <div className={className} role={role} aria-label={title} aria-modal="true"
        tabIndex={-1} ref={dialog} onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (isComposing(event)) return;
          onKeyDown?.(event);
          if (event.defaultPrevented) return;
          if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            onClose();
          } else if (event.key === 'Tab') {
            const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
              'button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]',
            )).filter((node) => !node.closest('[hidden]'));
            const first = items[0];
            const last = items.at(-1);
            if (!first || event.target === event.currentTarget || event.target === (event.shiftKey ? first : last)) {
              event.preventDefault();
              (event.shiftKey ? last : first)?.focus();
            }
          }
        }}>
        {children}
      </div>
    </div>
  );
}

import { useRef, useState } from 'react';
import { isComposing } from '../../shared/shortcuts';
import type { SessionInfo } from '../../types';
import { EditIcon } from '../../shared/Icons';

type TitleEdit = { value: string; saving?: boolean; error?: string };

export function useSessionTitleEdits(rename: (id: string, title: string) => Promise<void>) {
  const [edits, setEdits] = useState<Record<string, TitleEdit>>({});
  const pending = useRef(new Set<string>());

  function set(id: string, edit: TitleEdit) {
    setEdits(current => ({ ...current, [id]: edit }));
  }
  function discard(id: string) {
    setEdits(current => {
      const next = { ...current };
      delete next[id];
      return next;
    });
  }
  async function save(session: SessionInfo) {
    const { id } = session;
    const edit = edits[id];
    if (!edit || pending.current.has(id)) return;
    const title = edit.value.trim();
    if (!title || title === session.title) {
      discard(id);
      return;
    }
    pending.current.add(id);
    set(id, { value: title, saving: true });
    try {
      await rename(id, title);
      discard(id);
    } catch (failure) {
      set(id, { value: title, error: failure instanceof Error ? failure.message : 'Could not save the title.' });
    } finally {
      pending.current.delete(id);
    }
  }
  return { edits, set, discard, save };
}

export function SessionTitle({ session, editor }: {
  session: SessionInfo; editor: ReturnType<typeof useSessionTitleEdits>;
}) {
  const cancelBlur = useRef(false);
  const edit = editor.edits[session.id];
  const value = edit?.value ?? session.title;
  return <div className="topbar-title session-name">
    <label className="session-name-field">
      <span className="session-name-width" aria-hidden="true">{value}</span>
      <input type="text" aria-label="Conversation title" title="Edit conversation title"
        autoComplete="off" enterKeyHint="done" value={value}
        readOnly={edit?.saving} aria-invalid={!!edit?.error}
        aria-describedby={edit?.error ? 'session-title-error' : undefined}
        onFocus={event => event.currentTarget.select()}
        onChange={event => editor.set(session.id, { value: event.target.value })}
        onBlur={() => {
          if (!cancelBlur.current) void editor.save(session);
          cancelBlur.current = false;
        }}
        onKeyDown={event => {
          if (isComposing(event) || (event.key !== 'Enter' && event.key !== 'Escape')) return;
          event.preventDefault();
          event.stopPropagation();
          if (event.key === 'Escape') {
            cancelBlur.current = true;
            if (!edit?.saving) editor.discard(session.id);
          }
          event.currentTarget.blur();
        }} />
      <EditIcon size={13} />
    </label>
    {edit?.saving && <small role="status">Saving…</small>}
    {edit?.error && <small role="alert" id="session-title-error">{edit.error}</small>}
  </div>;
}

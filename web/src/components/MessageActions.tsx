import { useState } from 'react';
import { api } from '../api/client';
import type { MessageDraft } from '../types';
import { EditIcon, UndoIcon } from './Icons';

export type MessageActionHandler = (
  sessionId: string, messageId: string, replacement?: { text: string; files: readonly File[] },
) => Promise<boolean>;

export function MessageActions({ sessionId, messageId, undoing, onAction }: {
  sessionId: string;
  messageId: string;
  undoing: boolean;
  onAction: MessageActionHandler;
}) {
  const [draft, setDraft] = useState<MessageDraft | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const disabled = working || undoing;

  async function edit() {
    setWorking(true);
    setError('');
    try {
      setDraft(await api.editDraft(sessionId, messageId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to open this message.');
    } finally {
      setWorking(false);
    }
  }

  async function resend() {
    if (!draft || disabled) return;
    const text = draft.text.trim();
    if (!text) return;
    if (text.startsWith('/')) {
      setError('Enter a chat message. Use the main input for slash commands.');
      return;
    }
    setWorking(true);
    setError('');
    try {
      // Get verified originals before Undo removes the active message and its download links.
      const files = await Promise.all(draft.attachments.map((item) => api.readUpload(sessionId, item)));
      if (await onAction(sessionId, messageId, { text, files })) setDraft(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to resend this message.');
    } finally {
      setWorking(false);
    }
  }

  return <>
    {draft ? (
      <form className="message-editor" onSubmit={(event) => { event.preventDefault(); void resend(); }}>
        <textarea aria-label="Edit last message" autoFocus maxLength={100000} rows={3}
          value={draft.text} disabled={disabled}
          onChange={(event) => setDraft({ ...draft, text: event.target.value })}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && !disabled && !event.nativeEvent.isComposing) {
              event.stopPropagation();
              setDraft(null);
              setError('');
            }
          }} />
        {draft.attachments.map((file) => (
          <button key={file.id} type="button" className="attachment-chip" disabled={disabled}
            aria-label={`Remove ${file.name}`} onClick={() => setDraft({
              ...draft, attachments: draft.attachments.filter((item) => item.id !== file.id),
            })}>{file.name} ×</button>
        ))}
        <div className="message-editor-buttons">
          <button type="button" disabled={disabled} onClick={() => { setDraft(null); setError(''); }}>Cancel</button>
          <button type="submit" disabled={disabled || !draft.text.trim()}>Resend</button>
        </div>
      </form>
    ) : (
      <div className="message-actions" role="group" aria-label="Last message actions">
        <button type="button" className="icon-button" aria-label="Edit and resend last message"
          title="Edit and resend last message" disabled={disabled} onClick={() => void edit()}><EditIcon size={16} /></button>
        <button type="button" className="icon-button" aria-label="Undo last turn" title="Undo last turn"
          disabled={disabled} onClick={() => void onAction(sessionId, messageId)}><UndoIcon size={16} /></button>
      </div>
    )}
    {error && <div className="message-action-error" role="alert">{error}</div>}
  </>;
}

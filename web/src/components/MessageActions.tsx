import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { EditedDraft, FailedEdit } from '../state/editRecovery';
import type { MessageDraft } from '../types';
import { EditIcon, UndoIcon } from './Icons';

export type MessageActionHandler = (
  sessionId: string, messageId: string, replacement?: EditedDraft,
) => Promise<boolean>;

export type OpenEditor = (sessionId: string, messageId: string, draft: EditedDraft, warning?: string) => Promise<void>;

export function MessageActions({ sessionId, messageId, undoing, onAction, onEdit }: {
  sessionId: string;
  messageId: string;
  undoing: boolean;
  onAction: MessageActionHandler;
  onEdit?: OpenEditor;
}) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const disabled = working || undoing;

  async function edit() {
    setWorking(true);
    setError('');
    try {
      const draft: MessageDraft = await api.editDraft(sessionId, messageId);
      try {
        const files = await Promise.all(draft.attachments.map(item => api.readUpload(sessionId, item)));
        await onEdit?.(sessionId, messageId, { ...draft, files });
      } catch (reason) {
        await onEdit?.(sessionId, messageId, draft, reason instanceof Error ? reason.message : 'Could not read the attachments.');
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to open this message.');
    } finally {
      setWorking(false);
    }
  }

  return <>
    <div className="message-actions" role="group" aria-label="Last message actions">
      <button type="button" className="icon-button" aria-label="Edit and resend last message"
        title="Edit and resend last message" disabled={disabled} onClick={() => void edit()}><EditIcon size={16} /></button>
      <button type="button" className="icon-button" aria-label="Undo last turn" title="Undo last turn"
        disabled={disabled} onClick={() => void onAction(sessionId, messageId)}><UndoIcon size={16} /></button>
    </div>
    {error && <div className="message-action-error" role="alert">{error}</div>}
  </>;
}

function DownloadEditFile({ file }: { file: File }) {
  const [url, setUrl] = useState('');
  useEffect(() => {
    const link = URL.createObjectURL(file);
    setUrl(link);
    return () => URL.revokeObjectURL(link);
  }, [file]);
  return <a href={url} download={file.name}>{file.name}</a>;
}

export function RecoveredEdit({ edit, busy, onChange, onRetry, onDiscard }: {
  edit: FailedEdit;
  busy: boolean;
  onChange: (value: FailedEdit['draft']) => void;
  onRetry: () => void;
  onDiscard: () => void;
}) {
  const { draft, mode } = edit;
  const composing = mode === 'compose';
  const text = draft.text.trim();
  const [copied, setCopied] = useState(false);
  useEffect(() => setCopied(false), [draft]);
  return <section className="message-editor" aria-label={composing ? 'Edit last message' : 'Saved message edit'}>
    {edit.error && <div role="alert">{edit.error}</div>}
    {edit.warning && <div role="status">{edit.warning}</div>}
    <textarea aria-label={composing ? 'Edit last message' : 'Saved edit text'} autoFocus={composing}
      onKeyDown={event => {
        if (composing && event.key === 'Escape' && !busy && !event.nativeEvent.isComposing) {
          event.stopPropagation();
          onDiscard();
        }
      }}
      readOnly={mode === 'retry'} rows={composing ? 3 : 2} maxLength={100000}
      disabled={busy} value={draft.text} onChange={event => onChange({ ...draft, text: event.target.value })} />
    {draft.attachments.map((file, index) => (
      <span key={file.id} className="attachment-chip">
        {draft.files?.[index] ? <DownloadEditFile file={draft.files[index]} /> : file.name}
        {mode !== 'retry' && <button type="button" disabled={busy} aria-label={`Remove ${file.name}`}
          onClick={() => onChange({ ...draft, attachments: draft.attachments.filter((_, i) => i !== index),
            files: draft.files?.filter((_, i) => i !== index) })}>×</button>}
      </span>
    ))}
    <div className="message-editor-buttons">
      <button type="button" disabled={busy} onClick={onDiscard}>{composing ? 'Cancel' : 'Discard edit'}</button>
      {!composing && navigator.clipboard && <button type="button" onClick={() =>
        void navigator.clipboard.writeText(draft.text).then(() => setCopied(true)).catch(() => setCopied(false))
      }>{copied ? 'Copied' : 'Copy text'}</button>}
      {mode !== 'stale' && <button type="button" disabled={busy || !text || text.startsWith('/')} onClick={onRetry}>{composing ? 'Resend' : 'Retry edit'}</button>}
    </div>
  </section>;
}

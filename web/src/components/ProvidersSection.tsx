import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import type { ProviderAction, ProviderActionRequest, ProviderInfo } from '../types';
import { FeedbackMessage, type ActionFeedback } from './ActionFeedback';

type EditorAction = ProviderAction | 'model';
type Editor = { key: string; action: EditorAction };
type ProviderFeedback = ActionFeedback & { target: string };

const editorLabels = {
  set_key: { field: 'API key', submit: 'Save', pending: 'Saving key…' },
  update_key: { field: 'API key', submit: 'Save', pending: 'Saving key…' },
  set_endpoint: { field: 'Base URL', submit: 'Save', pending: 'Saving Base URL…' },
  model: { field: 'model ID', submit: 'Apply model', pending: 'Applying model…' },
  remove: { field: '', submit: 'Remove permanently', pending: 'Removing provider…' },
};

async function submitProviderEdit(provider: string, action: EditorAction, value: string) {
  if (action === 'model') {
    const result = await api.runCommand({ line: `/model ${provider}/${value}`, session_id: null, confirmed: false });
    if (result.status !== 'ok') throw new Error(result.message);
    return result.message;
  }
  const body: ProviderActionRequest = { action };
  if (action === 'remove') body.confirmed = true;
  else if (action === 'set_endpoint') body.endpoint = value;
  else body.api_key = value;
  const result = await api.providerAction(provider, body);
  if (!result.ok) throw new Error(result.message);
  return result.message;
}

export function ProvidersSection({ onChanged, refreshControl }: {
  onChanged: () => void;
  refreshControl: ReactNode;
}) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [editor, setEditor] = useState<Editor | null>(null);
  const [value, setValue] = useState('');
  const [feedback, setFeedback] = useState<ProviderFeedback | null>(null);
  const busy = feedback?.kind === 'pending';
  const opener = useRef<HTMLButtonElement | null>(null);
  const restoreFocus = useRef(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await api.providers(signal);
      if (!signal?.aborted) {
        setProviders(result.items);
        setLoadError('');
      }
    } catch (failure) {
      if (!signal?.aborted) {
        setLoadError(failure instanceof Error ? failure.message : 'Could not load providers.');
      }
    } finally {
      if (!signal?.aborted) setLoaded(true);
    }
  }, []);

  useEffect(() => {
    const request = new AbortController();
    void load(request.signal);
    return () => request.abort();
  }, [load]);

  useLayoutEffect(() => {
    if (!editor && restoreFocus.current) {
      restoreFocus.current = false;
      opener.current?.focus();
    }
  }, [editor]);

  function closeEditor() {
    restoreFocus.current = true;
    setEditor(null);
    setValue('');
  }

  function openEditor(provider: ProviderInfo, action: EditorAction, button: HTMLButtonElement) {
    setFeedback(null);
    if (editor?.key === provider.key && editor.action === action) {
      closeEditor();
      return;
    }
    opener.current = button;
    setValue(action === 'set_endpoint' ? provider.endpoint : '');
    setEditor({ key: provider.key, action });
  }

  async function apply(provider: ProviderInfo, action: EditorAction) {
    if (busy || (action !== 'remove' && !value.trim())) return;
    const target = provider.key;
    setFeedback({ target, kind: 'pending', message: editorLabels[action].pending });
    try {
      const message = await submitProviderEdit(target, action, value.trim());
      await load();
      closeEditor();
      setFeedback({ target, kind: 'success', message });
      onChanged();
    } catch (failure) {
      setFeedback({ target, kind: 'error', message: failure instanceof Error ? failure.message : 'Provider action failed.' });
    }
  }

  return (
    <div className="settings-section" data-testid="providers-section">
      <div className="settings-section-title">Providers</div>
      <p className="settings-hint">
        The backend stores API keys using your operating system’s credential protection.
        Removing a provider deletes its saved key.
      </p>
      {refreshControl}
      <div className="providers-list" data-testid="providers-list">
        {!loaded && <div className="empty" role="status">Loading providers…</div>}
        {loadError && <p className="provider-error" role="alert">{loadError}</p>}
        {loaded && !loadError && providers.length === 0 && <div className="empty">No providers available.</div>}
        {providers.map((provider) => {
          const action = editor?.key === provider.key ? editor.action : null;
          const editorId = `provider-editor-${provider.key}`;
          const actions: [EditorAction, string][] = [
            provider.configured ? ['update_key', 'Update key'] : ['set_key', 'Add key'],
            ['set_endpoint', 'Base URL'],
          ];
          if (provider.configured || provider.current) actions.push(['model', 'Use model ID']);
          actions.push(['remove', 'Remove']);
          const keyField = action === 'set_key' || action === 'update_key';
          return (
            <div className="provider-row" key={provider.key} data-testid={`provider-${provider.key}`}>
              <div className="provider-head">
                <span>{provider.label}{provider.current ? ' · current' : ''}</span>
                <span className="provider-state">{provider.configured ? 'configured' : 'no key'}</span>
              </div>
              <div className="provider-endpoint">{provider.endpoint}</div>
              <div className="provider-buttons">
                {actions.map(([choice, label]) => (
                  <button key={choice === 'update_key' ? 'set_key' : choice} type="button"
                    className={choice === 'remove' ? 'danger' : undefined} disabled={busy}
                    aria-expanded={action === choice} aria-controls={action === choice ? editorId : undefined}
                    onClick={(event) => openEditor(provider, choice, event.currentTarget)}>
                    {label}
                  </button>
                ))}
              </div>
              {action && (
                <form key={action} id={editorId} className="provider-form provider-editor"
                  role={action === 'remove' ? 'alertdialog' : undefined}
                  aria-label={action === 'remove' ? `Remove ${provider.label}` : `Edit ${provider.label}`}
                  aria-describedby={action === 'remove' ? `${editorId}-warning` : undefined}
                  data-testid={action === 'remove' ? 'provider-remove' : 'provider-form'}
                  onKeyDown={(event) => {
                    if (event.key !== 'Escape' || event.nativeEvent.isComposing) return;
                    event.preventDefault();
                    event.stopPropagation();
                    if (!busy) { closeEditor(); setFeedback(null); }
                  }}
                  onSubmit={(event) => { event.preventDefault(); void apply(provider, action); }}>
                  {action === 'remove' ? (
                    <div id={`${editorId}-warning`}>Remove {provider.label}? Its saved API key will be deleted.</div>
                  ) : (
                    <label>
                      {provider.label} {editorLabels[action].field}
                      <input autoFocus type={keyField ? 'password' : 'text'}
                        value={value} readOnly={busy} autoComplete="off" spellCheck={false}
                        data-testid={action === 'model' ? 'custom-model' : 'provider-value'}
                        onChange={(event) => setValue(event.target.value)} />
                    </label>
                  )}
                  <div className="confirm-actions">
                    <button type="button" autoFocus={action === 'remove'} disabled={busy}
                      onClick={() => { closeEditor(); setFeedback(null); }}>Cancel</button>
                    <button type="submit" className={action === 'remove' ? 'danger' : undefined}
                      disabled={action !== 'remove' && !value.trim()} aria-disabled={busy || (action !== 'remove' && !value.trim())}>
                      {busy ? editorLabels[action].pending : editorLabels[action].submit}
                    </button>
                  </div>
                </form>
              )}
              <FeedbackMessage feedback={feedback?.target === provider.key ? feedback : null} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { SHORTCUT_HELP } from '../state/shortcuts';
import type { CommandResponse, HealthResponse } from '../types';
import { ProvidersSection } from './ProvidersSection';

type SectionKey = 'model' | 'thinking' | 'context' | 'providers' | 'about';

const SECTIONS: { key: SectionKey; label: string; description: string }[] = [
  { key: 'model', label: 'Model', description: 'Active provider and model' },
  { key: 'thinking', label: 'Thinking', description: 'Reasoning effort' },
  { key: 'context', label: 'Context', description: 'Context window size' },
  { key: 'providers', label: 'Providers', description: 'API keys and endpoints' },
  { key: 'about', label: 'About', description: 'Version, status and shortcuts' },
];

interface ModelOption {
  provider: string;
  model: string;
  label?: string;
  current?: boolean;
}

interface ThinkState {
  current: string;
  options: string[];
}

interface ContextState {
  current: number;
  options: number[];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}

/** Never render an option that the server did not actually send. */
function readModels(result: CommandResponse): ModelOption[] {
  const rows = asRecord(result.data)?.options;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    const item = asRecord(row);
    if (!item || typeof item.provider !== 'string' || typeof item.model !== 'string') return [];
    return [{
      provider: item.provider,
      model: item.model,
      label: typeof item.label === 'string' ? item.label : undefined,
      current: item.current === true,
    }];
  });
}

function readThink(result: CommandResponse): ThinkState | null {
  const data = asRecord(result.data);
  if (!data || typeof data.current !== 'string' || !Array.isArray(data.options)) return null;
  return {
    current: data.current,
    options: data.options.filter((value): value is string => typeof value === 'string'),
  };
}

function readContext(result: CommandResponse): ContextState | null {
  const data = asRecord(result.data);
  if (!data || typeof data.current !== 'number' || !Array.isArray(data.options)) return null;
  return {
    current: data.current,
    options: data.options.filter((value): value is number => typeof value === 'number'),
  };
}

/**
 * Web settings panel: the counterpart of the TUI's /model, /think, /context
 * and /setup flows. Every value is read and written through the same
 * Application use cases that the TUI uses; the panel only presents them.
 */
export function SettingsPanel({
  health,
  onClose,
  onChanged,
  onNotice,
}: {
  health: HealthResponse | null;
  onClose: () => void;
  onChanged: () => void;
  onNotice: (message: string) => void;
}) {
  const [section, setSection] = useState<SectionKey>('model');
  const [models, setModels] = useState<ModelOption[]>([]);
  const [think, setThink] = useState<ThinkState | null>(null);
  const [context, setContext] = useState<ContextState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadingModel, setLoadingModel] = useState(true);
  const [customModel, setCustomModel] = useState('');

  const loadModel = useCallback(async (refresh = false) => {
    setLoadingModel(true);
    setError('');
    try {
      const result = await api.runCommand({
        line: refresh ? '/model refresh' : '/model',
        session_id: null,
        confirmed: false,
      });
      setModels(readModels(result));
      if (result.status === 'error' || result.status === 'busy') setError(result.message);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not load models');
    } finally {
      setLoadingModel(false);
    }
  }, []);

  const loadThinking = useCallback(async () => {
    try {
      const [thinkResult, contextResult] = await Promise.all([
        api.runCommand({ line: '/think', session_id: null, confirmed: false }),
        api.runCommand({ line: '/context', session_id: null, confirmed: false }),
      ]);
      const nextThink = readThink(thinkResult);
      const nextContext = readContext(contextResult);
      if (nextThink) setThink(nextThink);
      if (nextContext) setContext(nextContext);
      const failure = [thinkResult, contextResult].find((item) => item.status === 'error');
      if (failure) setError(failure.message);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not load settings');
    }
  }, []);

  useEffect(() => {
    void loadModel();
    void loadThinking();
  }, [loadModel, loadThinking]);

  const run = async (line: string, after?: () => void) => {
    setBusy(true);
    setError('');
    try {
      const result = await api.runCommand({ line, session_id: null, confirmed: false });
      if (result.status === 'ok') {
        onNotice(result.message);
        after?.();
        onChanged();
      } else {
        setError(result.message);
      }
      return result;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Settings action failed');
      return null;
    } finally {
      setBusy(false);
    }
  };

  const currentModel = models.find((item) => item.current);
  const currentLabel = health ? `${health.provider}/${health.model}` : '(loading)';

  return (
    <div className="overlay" data-testid="settings-overlay" onClick={onClose}>
      <div
        className="settings-dialog"
        role="dialog"
        aria-label="Settings"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="settings-head">
          <div className="confirm-title">Settings</div>
          <button type="button" onClick={onClose} aria-label="Close settings">Close</button>
        </div>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label="Settings sections">
            {SECTIONS.map((item) => (
              <button
                key={item.key}
                type="button"
                className={section === item.key ? 'active' : ''}
                aria-current={section === item.key ? 'page' : undefined}
                data-testid={`settings-tab-${item.key}`}
                onClick={() => setSection(item.key)}
              >
                <span>{item.label}</span>
                <small>{item.description}</small>
              </button>
            ))}
          </nav>
          <div className="settings-content">
            {section === 'model' && (
              <div className="settings-section" data-testid="settings-model">
                <div className="settings-section-title">Model</div>
                <p className="settings-hint">
                  Active: <strong data-testid="settings-current-model">{currentLabel}</strong>
                </p>
                <div className="providers-actions">
                  <button
                    type="button"
                    disabled={busy || loadingModel}
                    onClick={() => void loadModel(true)}
                  >
                    Refresh model list
                  </button>
                </div>
                <div className="option-list" data-testid="settings-models">
                  {loadingModel && !models.length && <div className="empty">Loading models…</div>}
                  {models.map((item) => (
                    <button
                      key={`${item.provider}/${item.model}`}
                      type="button"
                      className={`option-row${item.current ? ' active' : ''}`}
                      disabled={busy}
                      aria-pressed={item.current}
                      data-testid={`model-${item.provider}-${item.model}`}
                      onClick={() => void run(`/model ${item.provider}/${item.model}`, () => {
                        void loadModel();
                      })}
                    >
                      <span>{item.label || `${item.provider}/${item.model}`}</span>
                      <small>{item.current ? 'current' : item.provider}</small>
                    </button>
                  ))}
                  {!loadingModel && !models.length && (
                    <div className="empty">
                      No model list cached. Refresh, or switch by ID below.
                    </div>
                  )}
                </div>
                <div className="provider-form">
                  <label>
                    Switch by model ID
                    <input
                      type="text"
                      value={customModel}
                      onChange={(event) => setCustomModel(event.target.value)}
                      placeholder="model-name"
                      data-testid="settings-custom-model"
                    />
                  </label>
                  <div className="confirm-actions">
                    <button
                      type="button"
                      disabled={busy || !customModel.trim() || !(currentModel?.provider ?? health?.provider)}
                      onClick={() => void run(
                        `/model ${currentModel?.provider ?? health?.provider}/${customModel.trim()}`,
                        () => { setCustomModel(''); void loadModel(); },
                      )}
                    >
                      Apply
                    </button>
                  </div>
                </div>
              </div>
            )}

            {section === 'thinking' && (
              <div className="settings-section" data-testid="settings-thinking">
                <div className="settings-section-title">Thinking</div>
                <p className="settings-hint">
                  Reasoning effort for the active model. “default” leaves the
                  provider default untouched.
                </p>
                <div className="option-list">
                  {!think && <div className="empty">This model exposes no reasoning options.</div>}
                  {think?.options.map((value) => (
                    <button
                      key={value || 'default'}
                      type="button"
                      className={`option-row${think.current === value ? ' active' : ''}`}
                      aria-pressed={think.current === value}
                      disabled={busy}
                      data-testid={`think-${value || 'default'}`}
                      onClick={() => void run(`/think ${value || 'default'}`, () => {
                        void loadThinking();
                      })}
                    >
                      <span>{value || 'default'}</span>
                      <small>{think.current === value ? 'current' : ''}</small>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {section === 'context' && (
              <div className="settings-section" data-testid="settings-context">
                <div className="settings-section-title">Context window</div>
                <p className="settings-hint">
                  Larger windows keep more history in one turn and cost more tokens.
                </p>
                <div className="option-list">
                  {!context && <div className="empty">No context options available.</div>}
                  {context?.options.map((value) => (
                    <button
                      key={value}
                      type="button"
                      className={`option-row${context.current === value ? ' active' : ''}`}
                      aria-pressed={context.current === value}
                      disabled={busy}
                      data-testid={`context-${value}`}
                      onClick={() => void run(`/context ${value}`, () => {
                        void loadThinking();
                      })}
                    >
                      <span>{value.toLocaleString()} tokens</span>
                      <small>{context.current === value ? 'current' : ''}</small>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {section === 'providers' && (
              <ProvidersSection
                onChanged={() => {
                  onChanged();
                  void loadModel();
                }}
                onNotice={onNotice}
              />
            )}

            {section === 'about' && (
              <div className="settings-section" data-testid="settings-about">
                <div className="settings-section-title">About</div>
                <dl className="settings-facts">
                  <dt>Version</dt><dd>{health?.version ?? 'unknown'}</dd>
                  <dt>Provider</dt><dd>{health?.provider ?? 'unknown'}</dd>
                  <dt>Model</dt><dd>{health?.model ?? 'unknown'}</dd>
                  <dt>Generating</dt><dd>{health?.busy ? 'yes' : 'no'}</dd>
                  <dt>Single client</dt>
                  <dd>{health?.single_client ? 'yes — one Vault writer at a time' : 'no'}</dd>
                  <dt>Static assets</dt>
                  <dd>{health?.static_ready ? 'built' : 'not built (run scripts/build_web.py)'}</dd>
                </dl>
                <div className="settings-section-title">Shortcuts</div>
                <div className="settings-keys">
                  {SHORTCUT_HELP.map((item) => (
                    <div key={item.keys} className="settings-key-row">
                      <span>{item.keys}</span>
                      <span>{item.description}{item.note ? ` · ${item.note}` : ''}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {error && <div className="provider-error" data-testid="settings-error">{error}</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

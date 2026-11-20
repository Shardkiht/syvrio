import { useCallback, useEffect, useRef, useState } from 'react';
import { CommandDialog } from './CommandUI';
import { api } from '../api/client';
import { thinkingOptions } from '../state/commands';
import { SHORTCUT_HELP } from '../state/shortcuts';
import type { ThemeMode } from '../state/theme';
import type { CommandResponse, HealthResponse, ModelSearchResponse } from '../types';
import { ProvidersSection } from './ProvidersSection';

type SectionKey = 'model' | 'appearance' | 'thinking' | 'context' | 'providers' | 'about';

const SECTIONS: { key: SectionKey; label: string; description: string }[] = [
  { key: 'model', label: 'Model', description: 'Active provider and model' },
  { key: 'appearance', label: 'Appearance', description: 'Colour theme' },
  { key: 'thinking', label: 'Thinking', description: 'Reasoning effort' },
  { key: 'context', label: 'Context', description: 'Context window size' },
  { key: 'providers', label: 'Providers', description: 'API keys and endpoints' },
  { key: 'about', label: 'About', description: 'Version, status and shortcuts' },
];

const THEMES: { key: ThemeMode; label: string; description: string }[] = [
  { key: 'auto', label: 'Follow system', description: 'Match the browser setting' },
  { key: 'light', label: 'Light', description: 'Paper background, dark text' },
  { key: 'dark', label: 'Dark', description: 'Dim background, light text' },
];

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

function readThink(result: CommandResponse): ThinkState | null {
  const data = asRecord(result.data);
  if (!data || typeof data.current !== 'string' || !Array.isArray(data.options)) return null;
  return {
    current: data.current,
    options: thinkingOptions(data.options),
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
  theme,
  onTheme,
  onClose,
  onChanged,
  initialSection = 'model',
}: {
  health: HealthResponse | null;
  theme: ThemeMode;
  onTheme: (mode: ThemeMode) => void;
  onClose: () => void;
  onChanged: () => void;
  initialSection?: SectionKey;
}) {
  const [section, setSection] = useState<SectionKey>(initialSection);
  const [catalog, setCatalog] = useState<(ModelSearchResponse & { query: string }) | null>(null);
  const [think, setThink] = useState<ThinkState | null>(null);
  const [context, setContext] = useState<ContextState | null>(null);
  const [customContext, setCustomContext] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loadingModel, setLoadingModel] = useState(true);
  const [modelQuery, setModelQuery] = useState('');
  const searchRequest = useRef<AbortController | null>(null);
  const query = modelQuery.trim();

  const loadModel = useCallback(async (refresh = false) => {
    searchRequest.current?.abort();
    const request = new AbortController();
    searchRequest.current = request;
    setLoadingModel(true);
    setError('');
    try {
      const result = await api.searchModels(query, refresh, request.signal);
      if (!request.signal.aborted) setCatalog({ ...result, query });
    } catch (failure) {
      if (!request.signal.aborted) {
        setError(failure instanceof Error ? failure.message : 'Could not load models');
        setCatalog(null);
      }
    } finally {
      if (!request.signal.aborted) setLoadingModel(false);
    }
  }, [query]);

  useEffect(() => {
    if (section !== 'model') return;
    setLoadingModel(true);
    const timer = window.setTimeout(() => void loadModel(), query ? 160 : 0);
    return () => {
      window.clearTimeout(timer);
      searchRequest.current?.abort();
    };
  }, [section, query, loadModel, health?.provider, health?.model]);

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
    void loadThinking();
  }, [loadThinking]);

  const run = async (line: string, after?: () => void) => {
    setBusy(true);
    setNotice('');
    setError('');
    try {
      const result = await api.runCommand({ line, session_id: null, confirmed: false });
      if (result.status === 'ok') {
        setNotice(result.message);
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

  const applyContext = (value: number) => void run(`/context ${value}`, () => {
    setCustomContext('');
    void loadThinking();
  });
  const customContextValue = Number(customContext);
  const validCustomContext = /^\d+$/.test(customContext.trim())
    && Number.isSafeInteger(customContextValue) && customContextValue >= 2_000;

  const results = catalog?.query === query ? catalog : null;
  const currentLabel = health ? `${health.provider}/${health.model}` : '(loading)';

  return (
    <CommandDialog title="Settings" className="settings-dialog"
      testId="settings-overlay" onClose={onClose}>
        <div className="settings-head">
          <h2 className="confirm-title">Settings</h2>
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
            {section === 'appearance' && (
              <div className="settings-section" data-testid="settings-appearance">
                <div className="settings-section-title">Appearance</div>
                <p className="settings-hint">
                  The interface follows your browser unless you pin a theme. The
                  choice is stored in this browser only.
                </p>
                <div className="theme-options" role="radiogroup" aria-label="Colour theme">
                  {THEMES.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      role="radio"
                      aria-checked={theme === item.key}
                      className={theme === item.key ? 'active' : ''}
                      data-testid={`theme-${item.key}`}
                      onClick={() => onTheme(item.key)}
                    >
                      <span className={`theme-swatch theme-swatch-${item.key}`} aria-hidden="true" />
                      <span className="theme-label">{item.label}</span>
                      <small>{item.description}</small>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {section === 'model' && (
              <div className="settings-section" data-testid="settings-model">
                <div className="settings-section-title">Model</div>
                <p className="settings-hint">
                  Active: <strong data-testid="settings-current-model">{currentLabel}</strong>
                </p>
                <div className="providers-actions model-toolbar">
                  <button
                    type="button"
                    disabled={busy || loadingModel}
                    onClick={() => void loadModel(true)}
                  >
                    Refresh model list
                  </button>
                  <input
                    type="search"
                    className="model-search"
                    aria-label="Search models by name"
                    aria-controls="settings-model-list"
                    placeholder="Search model name"
                    autoComplete="off"
                    maxLength={512}
                    spellCheck={false}
                    value={modelQuery}
                    onChange={event => setModelQuery(event.target.value)}
                  />
                </div>
                <div className="option-list" id="settings-model-list" data-testid="settings-models">
                  {loadingModel && !results && <div className="empty" role="status">Searching models…</div>}
                  {results?.items.map((item) => (
                    <button
                      key={`${item.provider}/${item.model}`}
                      type="button"
                      className={`option-row${item.current ? ' active' : ''}`}
                      disabled={busy}
                      aria-pressed={item.current}
                      data-testid={`model-${item.provider}-${item.model}`}
                      onClick={() => void run(`/model ${item.provider}/${item.model}`)}
                    >
                      <span>{item.label || `${item.provider}/${item.model}`}</span>
                      <small>{item.current ? 'current' : item.provider}</small>
                    </button>
                  ))}
                  {!loadingModel && results && results.items.length === 0 && (
                    <div className="empty" role="status">
                      {query ? 'No matching models.' : 'No models available.'}
                    </div>
                  )}
                  {results && results.unavailable_providers.length > 0 && (
                    <p className="settings-hint">Model catalog unavailable for {results.unavailable_providers.join(', ')}.</p>
                  )}
                </div>
              </div>
            )}

            {section === 'thinking' && (
              <div className="settings-section" data-testid="settings-thinking">
                <div className="settings-section-title">Thinking</div>
                <p className="settings-hint">
                  Reasoning effort for the active model.
                  {think?.options.includes('') && ' “default” uses the provider setting.'}
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
                  {context && `Current: ${context.current.toLocaleString()} tokens. `}
                  Larger windows can keep more history. The selected model may cap the budget.
                </p>
                <div className="option-list">
                  {context && !context.options.length && <p className="settings-hint">Use a custom budget for this model.</p>}
                  {context?.options.map((value) => (
                    <button
                      key={value}
                      type="button"
                      className={`option-row${context.current === value ? ' active' : ''}`}
                      aria-pressed={context.current === value}
                      disabled={busy}
                      data-testid={`context-${value}`}
                      onClick={() => applyContext(value)}
                    >
                      <span>{value.toLocaleString()} tokens</span>
                      <small>{context.current === value ? 'current' : ''}</small>
                    </button>
                  ))}
                </div>
                <form className="context-custom" onSubmit={(event) => {
                  event.preventDefault();
                  if (!busy && validCustomContext) applyContext(customContextValue);
                }}>
                  <label className="settings-section-title" htmlFor="custom-context-window">Custom budget</label>
                  <div className="context-custom-controls">
                    <input id="custom-context-window" type="number" min={2000} step={1} inputMode="numeric"
                      placeholder="Tokens" disabled={busy} value={customContext}
                      onChange={(event) => setCustomContext(event.target.value)} />
                    <button type="submit" disabled={busy || !validCustomContext}>Apply</button>
                  </div>
                </form>
              </div>
            )}

            {section === 'providers' && (
              <ProvidersSection
                onChanged={onChanged}
                onNotice={setNotice}
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

            {notice && <div role="status" className="command-result-body">{notice}</div>}
            {error && <div className="provider-error" data-testid="settings-error">{error}</div>}
          </div>
        </div>
    </CommandDialog>
  );
}

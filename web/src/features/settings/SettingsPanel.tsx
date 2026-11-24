import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { CommandDialog, moveOption } from '../commands/CommandUI';
import { api } from '../../api/client';
import { thinkingOptions } from '../commands/commands';
import { SHORTCUT_HELP } from '../../shared/shortcuts';
import type { ThemeMode } from '../../shared/theme';
import type { CommandResponse, HealthResponse } from '../../types';
import { useModelCatalog } from './useModelCatalog';
import { FeedbackMessage } from '../../shared/ActionFeedback';
import { ProvidersSection } from './ProvidersSection';
import { SoftwareUpdate } from './SoftwareUpdate';
import { CheckIcon, RefreshIcon, SearchIcon } from '../../shared/Icons';

type SectionKey = 'model' | 'appearance' | 'thinking' | 'context' | 'providers' | 'shortcuts' | 'about';

const SECTIONS: { key: SectionKey; label: string; description: string }[] = [
  { key: 'model', label: 'Model', description: 'Active provider and model' },
  { key: 'appearance', label: 'Appearance', description: 'Colour theme' },
  { key: 'thinking', label: 'Thinking', description: 'Reasoning effort' },
  { key: 'context', label: 'Context', description: 'Context window size' },
  { key: 'providers', label: 'Providers', description: 'API keys and endpoints' },
  { key: 'shortcuts', label: 'Shortcuts', description: 'Keyboard shortcuts' },
  { key: 'about', label: 'About', description: 'Version and status' },
];

const THEMES: { key: ThemeMode; label: string }[] = [
  { key: 'auto', label: 'System' },
  { key: 'light', label: 'Light' },
  { key: 'dark', label: 'Dark' },
];

interface ThinkState {
  current: string;
  options: string[];
}

interface ContextState {
  current: number;
  options: number[];
  minimum: number;
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
    minimum: typeof data.minimum === 'number' ? data.minimum : 50_000,
  };
}

/**
 * 网页设置面板对应 TUI 中的 /model、/think、/context 和 /setup。
 * 两端通过同一套应用层用例读写设置，面板只负责展示。
 */
export function SettingsPanel({
  health,
  theme,
  onTheme,
  onClose,
  onChanged,
  initialSection = 'model',
  initialNotice = '',
}: {
  health: HealthResponse | null;
  theme: ThemeMode;
  onTheme: (mode: ThemeMode) => void;
  onClose: () => void;
  onChanged: () => void;
  initialSection?: SectionKey;
  initialNotice?: string;
}) {
  const [selectedSection, setSection] = useState<SectionKey | null>(null);
  const section = selectedSection ?? initialSection;
  const [think, setThink] = useState<ThinkState | null>(null);
  const [context, setContext] = useState<ContextState | null>(null);
  const [customContext, setCustomContext] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(initialNotice);
  const [modelQuery, setModelQuery] = useState('');
  const query = modelQuery.trim();
  const catalog = useModelCatalog(section === 'model', query, health);
  const { results, loading: loadingModel } = catalog;
  const refreshing = catalog.feedback?.kind === 'pending';
  const refreshControl = useRef<HTMLButtonElement>(null);
  const earliestStop = useRef(1);

  useLayoutEffect(() => {
    const spin = refreshControl.current?.querySelector('svg')?.getAnimations?.()[0];
    if (!spin?.effect) return;
    if (refreshing && spin.playState === 'finished') spin.currentTime = 0;
    const turns = Number(spin.currentTime) / Number(spin.effect.getTiming().duration);
    if (refreshing) {
      // 在上一圈尚未转完时重试，从当前角度继续转动。
      earliestStop.current = Math.ceil(turns) + 1;
      spin.effect.updateTiming({ iterations: Infinity });
      spin.play();
    } else if (spin.playState === 'running') {
      spin.effect.updateTiming({ iterations: Math.max(earliestStop.current, Math.ceil(turns)) });
    }
  }, [refreshing, section]);

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
  const minimumContext = context?.minimum ?? 50_000;
  const validCustomContext = /^\d+$/.test(customContext.trim())
    && Number.isSafeInteger(customContextValue) && customContextValue >= minimumContext;

  const currentLabel = health ? `${health.provider}/${health.model}` : '(loading)';
  const refreshButton = (
    <button ref={refreshControl} type="button" className={section === 'model' ? 'model-refresh icon-button' : 'model-refresh'}
      aria-label="Refresh model list" title="Refresh model list" aria-busy={refreshing}
      disabled={busy} aria-disabled={busy || refreshing} onClick={() => void catalog.refresh()}>
      <RefreshIcon size={17} />
      {section === 'providers' && 'Refresh model list'}
    </button>
  );

  return (
    <CommandDialog title="Settings" className="settings-dialog" overlayClass="settings-overlay"
      testId="settings-overlay" onClose={onClose} onKeyDown={event => {
        if (section === 'model' && event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === 's') {
          event.preventDefault();
          setSection('providers');
        } else if (event.target === event.currentTarget || (event.target as Element).closest('.model-choices, .setting-options')) {
          const choices = event.currentTarget.querySelectorAll<HTMLButtonElement>(
            '.model-choices button:not(:disabled), .setting-options button:not(:disabled)',
          );
          moveOption(event, Array.from(choices));
        }
      }}>
        <div className="settings-head">
          <h2 className="confirm-title">Settings</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close settings" title="Close settings">
            <span aria-hidden="true">×</span>
          </button>
        </div>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label="Settings sections">
            {SECTIONS.map((item) => (
              <button
                key={item.key}
                type="button"
                className={section === item.key ? 'active' : ''}
                aria-current={section === item.key ? 'page' : undefined}
                title={item.description}
                data-testid={`settings-tab-${item.key}`}
                onClick={() => {
                  if (item.key !== section) {
                    setNotice('');
                    setError('');
                    setSection(item.key);
                  }
                }}
              >
                {item.label}
              </button>
            ))}
          </nav>
          <div className="settings-content">
            {section === 'appearance' && (
              <div className="settings-section" data-testid="settings-appearance">
                <div className="settings-section-title">Colour theme</div>
                <p className="settings-hint">System follows your device. Changes apply to this browser.</p>
                <div className="setting-options" role="radiogroup" aria-label="Colour theme"
                  onKeyDown={(event) => {
                    const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
                    if (!step) return;
                    event.preventDefault();
                    const choices = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button'));
                    const index = choices.indexOf(event.target as HTMLButtonElement);
                    const next = choices[(index + step + choices.length) % choices.length];
                    next.focus();
                    next.click();
                  }}>
                  {THEMES.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      role="radio"
                      aria-checked={theme === item.key}
                      className="setting-choice"
                      tabIndex={theme === item.key ? 0 : -1}
                      data-testid={`theme-${item.key}`}
                      onClick={() => onTheme(item.key)}
                    >
                      <span className={`theme-swatch theme-swatch-${item.key}`} aria-hidden="true" />
                      <span>{item.label}</span>
                      {theme === item.key && <CheckIcon size={14} />}
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
                <div className="model-catalog">
                  <div className="model-toolbar">
                    <label className="model-search">
                      <SearchIcon size={16} />
                      <input
                        type="search"
                        aria-label="Search models by name"
                        aria-controls="settings-model-list"
                        placeholder="Search models"
                        autoComplete="off"
                        maxLength={512}
                        spellCheck={false}
                        value={modelQuery}
                        onChange={event => setModelQuery(event.target.value)}
                      />
                    </label>
                    {refreshButton}
                  </div>
                  <FeedbackMessage feedback={catalog.feedback} />
                  {catalog.error && <p className="provider-error" role="alert">{catalog.error}</p>}
                  <div className="model-choices" id="settings-model-list" data-testid="settings-models">
                    {loadingModel && !refreshing && !results && <div className="empty" role="status">Searching models…</div>}
                    {results?.items.map((item) => (
                      <button
                        key={`${item.provider}/${item.model}`}
                        type="button"
                        className="model-choice"
                        disabled={busy}
                        aria-pressed={item.current}
                        data-testid={`model-${item.provider}-${item.model}`}
                        onClick={() => void run(`/model ${item.provider}/${item.model}`)}
                      >
                        {item.current && <CheckIcon size={15} />}
                        <span>{item.label || `${item.provider}/${item.model}`}</span>
                      </button>
                    ))}
                    {!loadingModel && results && results.items.length === 0 && (
                      <div className="empty" role="status">
                        {query ? 'No matching models.' : 'No models available.'}
                      </div>
                    )}
                    {results && results.unavailable_providers.length > 0 && catalog.feedback?.kind !== 'warning' && (
                      <p className="settings-hint">Model catalog unavailable for {results.unavailable_providers.join(', ')}.</p>
                    )}
                  </div>
                </div>
              </div>
            )}

            {section === 'thinking' && (
              <div className="settings-section" data-testid="settings-thinking">
                <div className="settings-section-title">Reasoning effort</div>
                {think?.options.includes('') && (
                  <p className="settings-hint">“default” uses the provider setting.</p>
                )}
                <div className="setting-options" role="group" aria-label="Reasoning effort">
                  {!think?.options.length && <p className="settings-hint">This model exposes no reasoning options.</p>}
                  {think?.options.map((value) => (
                    <button
                      key={value || 'default'}
                      type="button"
                      className="setting-choice"
                      aria-pressed={think.current === value}
                      disabled={busy}
                      data-testid={`think-${value || 'default'}`}
                      onClick={() => void run(`/think ${value || 'default'}`, () => {
                        void loadThinking();
                      })}
                    >
                      {think.current === value && <CheckIcon size={14} />}
                      <span>{value || 'default'}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {section === 'context' && (
              <div className="settings-section" data-testid="settings-context">
                <div className="settings-section-title">Context window</div>
                <p className="settings-hint">
                  Minimum {minimumContext.toLocaleString()} tokens. Model limits apply.
                </p>
                {context && context.options.length > 0 && (
                  <div className="setting-options" role="group" aria-label="Context window presets">
                    {context.options.map((value) => (
                      <button
                        key={value}
                        type="button"
                        className="setting-choice"
                        aria-label={`${value.toLocaleString()} tokens`}
                        aria-pressed={context.current === value}
                        disabled={busy}
                        data-testid={`context-${value}`}
                        onClick={() => applyContext(value)}
                      >
                        {context.current === value && <CheckIcon size={14} />}
                        <span>{value % 1000 === 0 ? `${value / 1000}k` : value.toLocaleString()}</span>
                      </button>
                    ))}
                  </div>
                )}
                <form className="context-custom" onSubmit={(event) => {
                  event.preventDefault();
                  if (!busy && validCustomContext) applyContext(customContextValue);
                }}>
                  <label htmlFor="custom-context-window">Custom budget</label>
                  <div className="context-custom-controls">
                    <input id="custom-context-window" type="number" min={minimumContext} step={1} inputMode="numeric"
                      placeholder="Tokens" disabled={busy} value={customContext}
                      onChange={(event) => setCustomContext(event.target.value)} />
                    <button type="submit" disabled={busy || !validCustomContext}>Apply</button>
                  </div>
                  {context && !context.options.includes(context.current) && (
                    <p className="settings-hint">Current: {context.current.toLocaleString()} tokens</p>
                  )}
                </form>
              </div>
            )}

            {section === 'providers' && (
              <ProvidersSection
                onChanged={onChanged}
                refreshControl={<div className="providers-actions">
                  {refreshButton}
                  <FeedbackMessage feedback={catalog.feedback} />
                </div>}
              />
            )}

            {section === 'shortcuts' && (
              <section className="settings-section" aria-labelledby="settings-shortcuts-title">
                <h2 className="settings-section-title" id="settings-shortcuts-title">Keyboard shortcuts</h2>
                <dl className="settings-keys">
                  {SHORTCUT_HELP.map(({ keys, description, note }) => (
                    <div key={keys} className="settings-key-row">
                      <dt><kbd>{keys}</kbd></dt>
                      <dd>{description}{note && <small>{note}</small>}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {section === 'about' && (
              <div className="settings-section" data-testid="settings-about">
                <div className="settings-section-title">About</div>
                <dl className="settings-facts">
                  <dt>Version</dt><dd><SoftwareUpdate version={health?.version} onChanged={onChanged}
                    checkOnOpen={selectedSection === null && initialSection === 'about'} /></dd>
                  <dt>Provider</dt><dd>{health?.provider ?? 'unknown'}</dd>
                  <dt>Model</dt><dd>{health?.model ?? 'unknown'}</dd>
                  <dt>Generating</dt><dd>{health?.busy ? 'yes' : 'no'}</dd>
                  <dt>Single client</dt>
                  <dd>{health?.single_client ? 'yes — one Vault writer at a time' : 'no'}</dd>
                  <dt>Static assets</dt>
                  <dd>{health?.static_ready ? 'built' : 'not built (run scripts/build_web.py)'}</dd>
                </dl>
              </div>
            )}

            {notice && <div role="status" className="command-result-body">{notice}</div>}
            {error && <div className="provider-error" data-testid="settings-error">{error}</div>}
          </div>
        </div>
    </CommandDialog>
  );
}

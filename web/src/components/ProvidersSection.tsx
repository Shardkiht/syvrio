import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { ProviderAction, ProviderActionRequest, ProviderInfo } from '../types';

/**
 * Provider management shared by the settings panel (Web equivalent of the
 * TUI provider manager). A saved API key is write-only: it never comes back.
 */
export function ProvidersSection({
  onChanged,
  onNotice,
}: {
  onChanged: () => void;
  onNotice: (message: string) => void;
}) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [target, setTarget] = useState<{ provider: ProviderInfo; action: ProviderAction } | null>(null);
  const [value, setValue] = useState('');
  const [confirmRemove, setConfirmRemove] = useState<ProviderInfo | null>(null);
  const [customModel, setCustomModel] = useState('');

  const load = useCallback(() => {
    void api.providers()
      .then((result) => {
        setProviders(result.items);
        setError('');
      })
      .catch((failure: unknown) =>
        setError(failure instanceof Error ? failure.message : 'Could not load providers'))
      .finally(() => setLoaded(true));
  }, []);

  useEffect(load, [load]);

  const apply = async (provider: string, body: ProviderActionRequest) => {
    setBusy(true);
    setError('');
    try {
      const result = await api.providerAction(provider, body);
      onNotice(result.message);
      if (!result.ok) setError(result.message);
      load();
      onChanged();
      setTarget(null);
      setConfirmRemove(null);
      setValue('');
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Provider action failed');
    } finally {
      setBusy(false);
    }
  };

  const refreshModels = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await api.runCommand({ line: '/model refresh', session_id: null, confirmed: false });
      onNotice(result.message);
      if (result.status === 'error') setError(result.message);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Model refresh failed');
    } finally {
      setBusy(false);
    }
  };

  const applyCustomModel = async () => {
    const current = providers.find((item) => item.current) ?? providers[0];
    if (!current || !customModel.trim()) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.runCommand({
        line: `/model ${current.key}/${customModel.trim()}`,
        session_id: null,
        confirmed: false,
      });
      onNotice(result.message);
      if (result.status !== 'ok') setError(result.message);
      else {
        setCustomModel('');
        onChanged();
        load();
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not apply the model');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-section" data-testid="providers-section">
      <div className="settings-section-title">Providers</div>
      <p className="settings-hint">
        API keys are stored in the system credential store and are never sent back
        to the browser. Removing a provider deletes its saved key.
      </p>
      <div className="providers-actions">
        <button type="button" onClick={() => void refreshModels()} disabled={busy}>
          Refresh model list
        </button>
      </div>
      <div className="providers-list" data-testid="providers-list">
        {!loaded && <div className="empty">Loading providers…</div>}
        {loaded && providers.length === 0 && <div className="empty">No providers available.</div>}
        {providers.map((provider) => (
          <div className="provider-row" key={provider.key} data-testid={`provider-${provider.key}`}>
            <div className="provider-head">
              <span>{provider.label}{provider.current ? ' · current' : ''}</span>
              <span className="provider-state">{provider.configured ? 'configured' : 'no key'}</span>
            </div>
            <div className="provider-endpoint">{provider.endpoint}</div>
            <div className="provider-buttons">
              <button
                type="button"
                onClick={() => {
                  setTarget({ provider, action: provider.configured ? 'update_key' : 'set_key' });
                  setValue('');
                }}
              >
                {provider.configured ? 'Update key' : 'Add key'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setTarget({ provider, action: 'set_endpoint' });
                  setValue(provider.endpoint);
                }}
              >
                Base URL
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => setConfirmRemove(provider)}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>

      {target && (
        <div className="provider-form" data-testid="provider-form">
          <label>
            {target.action === 'set_endpoint'
              ? `${target.provider.label} Base URL`
              : `${target.provider.label} API key`}
            <input
              type={target.action === 'set_endpoint' ? 'text' : 'password'}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              data-testid="provider-value"
              autoComplete="off"
            />
          </label>
          <div className="confirm-actions">
            <button type="button" onClick={() => setTarget(null)}>Cancel</button>
            <button
              type="button"
              disabled={busy || !value.trim()}
              onClick={() => void apply(target.provider.key, {
                action: target.action,
                api_key: target.action === 'set_endpoint' ? '' : value,
                endpoint: target.action === 'set_endpoint' ? value : '',
              })}
            >
              Save
            </button>
          </div>
        </div>
      )}

      {confirmRemove && (
        <div className="provider-form" role="alertdialog" data-testid="provider-remove">
          <div>Remove {confirmRemove.label}? Its saved API key will be deleted.</div>
          <div className="confirm-actions">
            <button type="button" onClick={() => setConfirmRemove(null)}>Cancel</button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={() => void apply(confirmRemove.key, { action: 'remove', confirmed: true })}
            >
              Remove permanently
            </button>
          </div>
        </div>
      )}

      <div className="provider-form">
        <label>
          Custom model ID
          <input
            type="text"
            value={customModel}
            onChange={(event) => setCustomModel(event.target.value)}
            data-testid="custom-model"
            placeholder="model-name"
          />
        </label>
        <div className="confirm-actions">
          <button
            type="button"
            onClick={() => void applyCustomModel()}
            disabled={busy || !customModel.trim()}
          >
            Apply model
          </button>
        </div>
      </div>

      {error && <div className="provider-error" data-testid="provider-error">{error}</div>}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/client';
import type { ActionFeedback } from '../../shared/ActionFeedback';
import type { HealthResponse, ModelSearchResponse } from '../../types';

export function useModelCatalog(visible: boolean, query: string, health: HealthResponse | null) {
  const [catalog, setCatalog] = useState<ModelSearchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState<ActionFeedback | null>(null);
  const [generation, setGeneration] = useState(0);
  const active = useRef<AbortController | null>(null);
  const refreshActive = useRef(false);

  const invalidate = useCallback(() => {
    active.current?.abort();
    active.current = null;
    refreshActive.current = false;
    setGeneration(value => value + 1);
    setCatalog(null);
    setLoading(true);
    setError('');
    setFeedback(null);
  }, []);

  const load = useCallback(async (refresh: boolean) => {
    if (active.current && (!refresh || refreshActive.current)) return;
    active.current?.abort();
    const request = new AbortController();
    active.current = request;
    refreshActive.current = refresh;
    setError('');
    if (refresh) setFeedback({ kind: 'pending', message: 'Refreshing model list…' });
    try {
      const result = await api.searchModels('', refresh, request.signal);
      if (request.signal.aborted) return;
      setCatalog(result);
      if (refresh) {
        const unavailable = result.unavailable_providers;
        setFeedback(unavailable.length
          ? { kind: 'warning', message: `Catalog unavailable for ${unavailable.join(', ')}. Try refreshing again.` }
          : { kind: 'success', message: 'Model list refreshed.' });
      }
    } catch (failure) {
      if (request.signal.aborted) return;
      const message = failure instanceof Error ? failure.message : 'Could not load models';
      if (refresh) setFeedback({ kind: 'error', message: `Could not refresh the model list. ${message}` });
      else setError(message);
    } finally {
      if (!request.signal.aborted) {
        active.current = null;
        refreshActive.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    invalidate();
    return () => active.current?.abort();
  }, [health?.provider, health?.model, invalidate]);

  useEffect(() => {
    if (visible && loading && !active.current) void load(false);
  }, [visible, loading, health?.provider, health?.model, generation, load]);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!catalog || !needle) return catalog;
    return { ...catalog, items: catalog.items.filter(item =>
      `${item.provider}/${item.model}`.toLowerCase().includes(needle) || item.label.toLowerCase().includes(needle)) };
  }, [catalog, query]);

  return { results, loading, error, feedback, invalidate, refresh: () => load(true) };
}

import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { ActionFeedback } from '../components/ActionFeedback';
import type { HealthResponse, ModelSearchResponse } from '../types';

export function useModelCatalog(visible: boolean, query: string, health: HealthResponse | null) {
  const [catalog, setCatalog] = useState<(ModelSearchResponse & { query: string }) | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState<ActionFeedback | null>(null);
  const refreshRequest = useRef<AbortController | null>(null);
  const refreshed = useRef<{ query: string; result?: ModelSearchResponse } | null>(null);

  useEffect(() => () => refreshRequest.current?.abort(), []);

  async function refresh() {
    if (refreshRequest.current) return;
    const request = new AbortController();
    refreshRequest.current = request;
    const filter = visible ? query : '';
    setError('');
    setFeedback({ kind: 'pending', message: 'Refreshing model list…' });
    try {
      const result = await api.searchModels(filter, true, request.signal);
      if (request.signal.aborted) return;
      refreshed.current = { query: filter, result };
      const unavailable = result.unavailable_providers;
      setFeedback(unavailable.length
        ? { kind: 'warning', message: `Catalog unavailable for ${unavailable.join(', ')}. Try refreshing again.` }
        : { kind: 'success', message: 'Model list refreshed.' });
    } catch (failure) {
      if (request.signal.aborted) return;
      refreshed.current = { query: filter };
      const reason = failure instanceof Error ? failure.message : 'Request failed.';
      setFeedback({ kind: 'error', message: `Could not refresh the model list. ${reason}` });
    } finally {
      refreshRequest.current = null;
    }
  }

  useEffect(() => {
    if (feedback?.kind === 'pending') return;
    // Consume the manual response once. Later searches use the latest typed filter.
    const completed = refreshed.current;
    refreshed.current = null;
    if (!visible) return;
    if (completed?.query === query) {
      if (completed.result) setCatalog({ ...completed.result, query });
      setLoading(false);
      return;
    }
    const request = new AbortController();
    setLoading(true);
    setError('');
    const timer = window.setTimeout(async () => {
      try {
        const result = await api.searchModels(query, false, request.signal);
        if (!request.signal.aborted) setCatalog({ ...result, query });
      } catch (failure) {
        if (!request.signal.aborted) {
          setError(failure instanceof Error ? failure.message : 'Could not load models');
        }
      } finally {
        if (!request.signal.aborted) setLoading(false);
      }
    }, query ? 160 : 0);
    return () => {
      window.clearTimeout(timer);
      request.abort();
    };
  }, [visible, query, health?.provider, health?.model, feedback]);

  return { results: catalog?.query === query ? catalog : null, loading, error, refresh, feedback };
}

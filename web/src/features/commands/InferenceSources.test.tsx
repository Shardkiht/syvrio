import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { PendingReview } from './PendingReview';

it('loads every source only on expansion and distinguishes changed, unchanged and missing versions', async () => {
  const cited = { commit: 2, topic: 'general/notes', text: 'Original evidence', status: 'active', context: null };
  const stable = { ...cited, text: 'Unchanged evidence' };
  const current = { ...cited, commit: 9, text: 'Updated evidence' };
  const load = vi.spyOn(api, 'inferenceSources').mockResolvedValue({ sources: [
    { id: 'a', cited_commit: 2, cited, current },
    { id: 'b', cited_commit: 2, cited: stable, current: stable },
    { id: 'c', cited_commit: null, cited: null, current: null },
  ] });
  render(<PendingReview panel={{ kind: 'inferences', title: 'Inference review', count: 1,
    rows: [{ id: 'example', text: 'Synthetic inference', topic: 'habit', status: 'hypothesis' }] }}
    onRetry={vi.fn()} onResolve={vi.fn()} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: /Synthetic inference/ }));
  expect(load).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Compare sources'));
  expect(await screen.findByText('Updated evidence')).toBeTruthy();
  expect(screen.getByText('Original evidence')).toBeTruthy();
  expect(screen.getAllByText('Unchanged evidence')).toHaveLength(1);
  expect(screen.getByText('Version was not recorded.')).toBeTruthy();
  expect(screen.getByText('Current memory unavailable.')).toBeTruthy();
  expect(load).toHaveBeenCalledWith('example', expect.any(AbortSignal));
});

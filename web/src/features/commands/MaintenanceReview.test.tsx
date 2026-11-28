import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { MaintenancePage } from '../../types';
import { PendingReview } from './PendingReview';

const fact = { id:'a', revision:1, text:'Travels by bus', topic:'travel', recorded_at:'2026-10-01T10:00:00Z',
  context:{}, session_id:'s', message_id:'m', source:'Original bus message', source_at:null, source_archived:false };
const page: MaintenancePage = { job:{state:'completed',cursor:8}, next:null, items:[
  {id:42, kind:'conflict', reason:'Both describe the same commute.', facts:[fact,{...fact,id:'b',text:'Travels by bike'}]},
]};
afterEach(() => vi.restoreAllMocks());
function open() {
  const regular = vi.fn();
  render(<PendingReview panel={{kind:'pending', title:'Pending', rows:[], count:0}} onRetry={vi.fn()} onResolve={regular} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole('button',{name:'Maintenance suggestions'}));
  return regular;
}
it('shows the drawer before loading and requires direction, boundaries and a second confirmation', async () => {
  let finish!: (p:MaintenancePage) => void;
  vi.spyOn(api,'maintenance').mockImplementationOnce(() => new Promise(resolve => {finish=resolve;}))
    .mockResolvedValue({...page,items:[]});
  const apply=vi.spyOn(api,'reviewMaintenance').mockResolvedValue({state:'applied'});
  const regular=open();
  expect(screen.getByText('Loading suggestions…')).toBeTruthy();
  await act(async () => finish(page));
  expect(screen.getAllByText('Original bus message')).toHaveLength(2);
  fireEvent.change(screen.getByLabelText('Resolution'),{target:{value:'transition'}});
  const review=screen.getByRole('button',{name:'Review change'}) as HTMLButtonElement;
  expect(review.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('Later fact'),{target:{value:'b'}});
  expect(review.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('Previous fact valid through'),{target:{value:'2026-08'}});
  fireEvent.change(screen.getByLabelText('Later fact valid from'),{target:{value:'2026-09'}});
  fireEvent.click(review);
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Confirm'}));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(42,{action:'transition',selected_id:'b',previous_end:'2026-08',current_start:'2026-09'}));
  expect(await screen.findByText('No maintenance suggestions on this page.')).toBeTruthy();
  expect(regular).not.toHaveBeenCalled();
});
it('shows a stale review error and lets the user reload without applying a different pair', async () => {
  vi.spyOn(api,'maintenance').mockResolvedValueOnce(page).mockResolvedValue({...page,items:[]});
  const apply=vi.spyOn(api,'reviewMaintenance').mockRejectedValue(new Error('The facts changed. Refresh before reviewing.'));
  open();
  fireEvent.click(await screen.findByRole('button',{name:'Ignore'}));
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Confirm'}));
  expect((await screen.findByRole('alert')).textContent).toContain('facts changed');
  fireEvent.click(screen.getByRole('button',{name:'Refresh'}));
  expect(await screen.findByText('No maintenance suggestions on this page.')).toBeTruthy();
  expect(apply).toHaveBeenCalledTimes(1);
});

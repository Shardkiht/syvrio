import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, NetworkError } from '../api/client';
import { SoftwareUpdate } from './SoftwareUpdate';

const release = {
  current: '1.0.0', latest: '1.0.1', available: true, managed: true, state: 'idle' as const, message: '',
};

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('software update', () => {
  it('checks explicitly and reconciles a lost confirmation receipt without resending', async () => {
    const status = vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'idle', message: '' });
    const check = vi.spyOn(api, 'checkUpdate').mockResolvedValue(release);
    const install = vi.spyOn(api, 'installUpdate').mockRejectedValue(new NetworkError('Disconnected'));
    const changed = vi.fn();
    render(<SoftwareUpdate version={release.current} onChanged={changed} />);
    const checkButton = screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' });
    await waitFor(() => expect(checkButton.disabled).toBe(false));
    expect(check).not.toHaveBeenCalled();
    fireEvent.click(checkButton);
    const button = await screen.findByRole('button', { name: 'Install 1.0.1' });
    fireEvent.click(button);
    const dialog = within(screen.getByRole('alertdialog'));
    expect(dialog.getByText(/from 1\.0\.0 to 1\.0\.1/)).toBeTruthy();
    expect(document.activeElement).toBe(dialog.getByRole('button', { name: 'Cancel' }));
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(document.activeElement).toBe(button);
    expect(install).not.toHaveBeenCalled();
    status.mockRejectedValueOnce(new ApiError('Endpoint offline', undefined, 404))
      .mockResolvedValue({ state: 'completed', message: 'Updated to 1.0.1.' });
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('button', { name: 'Install and restart' }));
    expect(await screen.findByText('Reconnecting to the update service…')).toBeTruthy();
    expect(await screen.findByText('Updated to 1.0.1.', {}, { timeout: 3500 })).toBeTruthy();
    expect(install.mock.calls).toEqual([['1.0.1']]);
    expect(changed).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Reload updated interface' })).toBeTruthy();
  });

  it('restores a completed update after About was closed during installation', async () => {
    const status = vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'running', message: 'Installing…' });
    const changed = vi.fn();
    const about = render(<SoftwareUpdate version={release.current} onChanged={changed} />);
    await screen.findByText('Installing…');
    about.unmount();
    status.mockResolvedValue({ state: 'completed', message: 'Update installed.' });
    render(<SoftwareUpdate version={release.current} onChanged={changed} />);
    expect(await screen.findByRole('button', { name: 'Reload updated interface' })).toBeTruthy();
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it.each([403, 404])('finishes checking an inaccessible status endpoint (HTTP %i)', async code => {
    vi.useFakeTimers();
    const status = vi.spyOn(api, 'updateStatus')
      .mockResolvedValueOnce({ state: 'running', message: 'Installing…' })
      .mockRejectedValue(new ApiError('Unavailable', undefined, code));
    render(<SoftwareUpdate version={release.current} onChanged={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(140_000));
    expect(screen.getByRole('status').textContent).toContain(code === 403 ? 'Sign in again' : 'Reopen About');
    const calls = status.mock.calls.length;
    if (code === 403) expect(calls).toBe(2);
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(status).toHaveBeenCalledTimes(calls);
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' }).disabled).toBe(false);
  });

  it('can check an unmanaged installation without offering to restart it', async () => {
    vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'idle', message: '' });
    vi.spyOn(api, 'checkUpdate').mockResolvedValue({ ...release, managed: false });
    render(<SoftwareUpdate version={release.current} onChanged={vi.fn()} />);
    const button = screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    expect(await screen.findByText(/1\.0\.1 is available. Update with your package manager/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull();
  });
});

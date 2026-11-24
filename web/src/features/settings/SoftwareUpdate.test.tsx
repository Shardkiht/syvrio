import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, NetworkError } from '../../api/client';
import { SoftwareUpdate } from './SoftwareUpdate';

const current: string = import.meta.env.VITE_SYVRIO_VERSION;
const latest = current.replace(/\d+$/, patch => String(Number(patch) + 1));
const release = {
  current, latest, available: true, managed: true, state: 'idle' as const, message: '',
};

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('software update', () => {
  it('restores measured download progress and clears it on install and failure', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'updateStatus')
      .mockResolvedValueOnce({ state: 'running', message: 'Downloading wheel',
        progress: { stage: 'download', message: 'Downloading wheel', downloaded: 256, total: 1024 } })
      .mockResolvedValueOnce({ state: 'running', message: 'Installing dependencies',
        progress: { stage: 'install', message: 'Installing dependencies' } })
      .mockResolvedValue({ state: 'failed', message: 'The previous version is running again.' });
    render(<SoftwareUpdate version={release.current} onChanged={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByRole('progressbar', { name: 'Downloading wheel' }).getAttribute('value')).toBe('256');
    expect(screen.getByText(/256 B.*25%/)).toBeTruthy();
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByRole('progressbar', { name: 'Installing dependencies' }).hasAttribute('value')).toBe(false);
    expect(screen.queryByText(/25%/)).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.getByRole('status').textContent).toMatch(/previous version/);
  });

  it('reports the latest version without offering installation when /update finds no upgrade', async () => {
    vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'idle', message: '' });
    const check = vi.spyOn(api, 'checkUpdate').mockResolvedValue({
      ...release, latest: release.current, available: false,
    });
    const install = vi.spyOn(api, 'installUpdate');
    render(<SoftwareUpdate version={release.current} onChanged={vi.fn()} checkOnOpen />);
    expect(await screen.findByText('You’re up to date.')).toBeTruthy();
    expect(check).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull();
    expect(install).not.toHaveBeenCalled();
  });

  it('checks explicitly and reconciles a lost confirmation receipt without resending', async () => {
    const status = vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'idle', message: '' });
    const check = vi.spyOn(api, 'checkUpdate').mockResolvedValue(release);
    const install = vi.spyOn(api, 'installUpdate').mockRejectedValue(new NetworkError('Disconnected'));
    const changed = vi.fn();
    const about = render(<SoftwareUpdate version={release.current} onChanged={changed} />);
    const checkButton = screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' });
    await waitFor(() => expect(checkButton.disabled).toBe(false));
    expect(check).not.toHaveBeenCalled();
    fireEvent.click(checkButton);
    const button = await screen.findByRole('button', { name: `Install ${latest}` });
    fireEvent.click(button);
    const dialog = within(screen.getByRole('alertdialog'));
    expect(dialog.getByText(text => text.includes(`from ${current} to ${latest}`))).toBeTruthy();
    expect(document.activeElement).toBe(dialog.getByRole('button', { name: 'Cancel' }));
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(document.activeElement).toBe(button);
    expect(install).not.toHaveBeenCalled();
    status.mockRejectedValueOnce(new ApiError('Endpoint offline', undefined, 404))
      .mockResolvedValue({ state: 'completed', message: `Updated to ${latest}.` });
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('button', { name: 'Install and restart' }));
    expect(await screen.findByText('Reconnecting to the update service…')).toBeTruthy();
    expect(await screen.findByText(`Updated to ${latest}.`, {}, { timeout: 3500 })).toBeTruthy();
    expect(install.mock.calls).toEqual([[latest]]);
    expect(changed).toHaveBeenCalledTimes(1);
    about.rerender(<SoftwareUpdate version={latest} onChanged={changed} />);
    expect(screen.getByRole('button', { name: 'Reload updated interface' })).toBeTruthy();
  });

  it('restores a completed update after About was closed during installation', async () => {
    const status = vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'running', message: 'Installing…' });
    const changed = vi.fn();
    const about = render(<SoftwareUpdate version={release.current} onChanged={changed} />);
    await screen.findByText('Installing…');
    about.unmount();
    status.mockResolvedValue({ state: 'completed', message: 'Update installed.' });
    render(<SoftwareUpdate version={latest} onChanged={changed} />);
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: 'Reload updated interface' })).toBeTruthy();
    expect(screen.queryByText('Update installed.')).toBeNull();

    vi.spyOn(api, 'checkUpdate').mockResolvedValue({ ...release, current: latest, available: false, state: 'completed' });
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    await screen.findByText('You’re up to date.');
    expect(screen.getByRole('button', { name: 'Reload updated interface' })).toBeTruthy();
  });

  it('does not replay a past success or ask to reload the current interface', async () => {
    vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'completed', message: 'Update installed.' });
    const check = vi.spyOn(api, 'checkUpdate');
    render(<SoftwareUpdate version={current} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' }).disabled).toBe(false));
    expect(screen.queryByText('Update installed.')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reload updated interface' })).toBeNull();
    expect(check).not.toHaveBeenCalled();
  });

  it('does not offer a reload on a separately served interface with no update', async () => {
    vi.spyOn(api, 'updateStatus').mockResolvedValue({ state: 'idle', message: '' });
    render(<SoftwareUpdate version={latest} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Check for updates' }).disabled).toBe(false));
    expect(screen.queryByRole('button', { name: 'Reload updated interface' })).toBeNull();
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
    expect(await screen.findByText(`Syvrio ${latest} is available. Update with your package manager.`)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull();
  });
});

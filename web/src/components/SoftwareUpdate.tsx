import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, ApiError } from '../api/client';
import type { UpdateCheck, UpdateStatus } from '../types';
import { ConfirmDialog } from './CommandUI';

const POLL_INTERVAL = 2000;
const RECONNECT_TIMEOUT = 120_000;

export function SoftwareUpdate({ version, onChanged }: { version?: string; onChanged: () => void }) {
  const [release, setRelease] = useState<UpdateCheck | null>(null);
  const [busy, setBusy] = useState<'status' | 'check' | 'install' | null>('status');
  const [running, setRunning] = useState(false);
  const [watch, setWatch] = useState(0);
  const [canReload, setCanReload] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [message, setMessage] = useState('');
  const trigger = useRef<HTMLButtonElement>(null);
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);

  // Recover the job on reopening About. Only an explicit check contacts PyPI.
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    let tracking = watch > 0;
    let disconnectedAt: number | undefined;
    const poll = async () => {
      let status: UpdateStatus;
      try {
        status = await api.updateStatus(controller.signal);
      } catch (error) {
        if (controller.signal.aborted) return;
        setBusy(null);
        const code = error instanceof ApiError ? error.status : undefined;
        const retryable = !code || code === 404 || code >= 500;
        disconnectedAt ??= Date.now();
        if (tracking && retryable && Date.now() - disconnectedAt < RECONNECT_TIMEOUT) {
          setMessage('Reconnecting to the update service…');
          timer = window.setTimeout(poll, POLL_INTERVAL);
        } else {
          setRunning(false);
          if (code === 401 || code === 403) setMessage('Access denied. Sign in again to check the update.');
          else if (tracking && retryable) setMessage('Could not reconnect. Reopen About to check the update.');
          else setMessage(error instanceof Error ? error.message : 'Could not read update status.');
        }
        return;
      }
      if (controller.signal.aborted) return;
      setBusy(null);
      disconnectedAt = undefined;
      if (status.state === 'running') {
        tracking = true;
        setRunning(true);
        setMessage(status.message || 'Installing the update…');
        timer = window.setTimeout(poll, POLL_INTERVAL);
      } else if (status.state !== 'idle' || tracking) {
        setMessage(status.message || 'No active update. Check again for the installed version.');
        setRelease(null);
        setCanReload(status.state === 'completed');
        if (tracking || status.state === 'completed') changed.current();
        setRunning(false);
      }
    };
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [watch]);

  const watchUpdate = () => {
    setRunning(true);
    setWatch(sequence => sequence + 1);
  };

  const check = async () => {
    setBusy('check');
    setRelease(null);
    setCanReload(false);
    setMessage('');
    try {
      const result = await api.checkUpdate();
      setRelease(result);
      if (result.state === 'running') {
        setMessage(result.message);
        watchUpdate();
      }
      else if (!result.available) setMessage('You’re up to date.');
      else if (result.managed) setMessage(`Syvrio ${result.latest} is available.`);
      else setMessage(`Syvrio ${result.latest} is available. Update with your package manager.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Could not check for updates.');
    } finally {
      setBusy(null);
    }
  };

  const install = async () => {
    if (!release || !confirm) return;
    setConfirm(false);
    setBusy('install');
    setRelease(null);
    try {
      const accepted = await api.installUpdate(release.latest);
      setMessage(accepted.message);
      watchUpdate();
    } catch (error) {
      if (error instanceof ApiError && error.status && error.status < 500) {
        setMessage(error.message);
      } else {
        // A gateway restart can interrupt the receipt. Reconcile without repeating the POST.
        setMessage('Checking whether the update started…');
        watchUpdate();
      }
    } finally {
      setBusy(null);
    }
  };

  return <>
    <div className="software-update">
      <span>{release?.current ?? version ?? 'unknown'}</span>
      <button type="button" className="setting-choice" disabled={!!busy || running} onClick={() => void check()}>
        {busy === 'check' ? 'Checking…' : 'Check for updates'}
      </button>
      {release?.available && release.managed && !running && (
        <button ref={trigger} type="button" className="setting-choice" disabled={!!busy} onClick={() => setConfirm(true)}>
          Install {release.latest}
        </button>
      )}
      {canReload && (
        <button type="button" className="setting-choice" onClick={() => window.location.reload()}>
          Reload updated interface
        </button>
      )}
    </div>
    {message && <p role="status" className="settings-hint update-result">{message}</p>}
    {confirm && release && createPortal(
      <ConfirmDialog title="Install software update?" confirmLabel="Install and restart" focusCancel
        message={`Update Syvrio from ${release.current} to ${release.latest}? The service will restart and connected clients will briefly disconnect.`}
        onCancel={() => { setConfirm(false); trigger.current?.focus(); }}
        onConfirm={() => void install()} />, document.body,
    )}
  </>;
}

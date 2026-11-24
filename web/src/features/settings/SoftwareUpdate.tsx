import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, ApiError } from '../../api/client';
import type { UpdateCheck, UpdateStatus } from '../../types';
import { ConfirmDialog } from '../commands/CommandUI';
import { SurfacePresence } from '../../shared/SurfacePresence';

const POLL_INTERVAL = 2000;
const RECONNECT_TIMEOUT = 120_000;

function downloadSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

export function SoftwareUpdate({ version, onChanged, checkOnOpen = false }: {
  version?: string; onChanged: () => void; checkOnOpen?: boolean;
}) {
  const [release, setRelease] = useState<UpdateCheck | null>(null);
  const [busy, setBusy] = useState<'status' | 'check' | 'install' | null>('status');
  const [running, setRunning] = useState(false);
  const [watch, setWatch] = useState(0);
  const [canReload, setCanReload] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [message, setMessage] = useState('');
  const [progress, setProgress] = useState<UpdateStatus['progress']>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);

  const watchUpdate = useCallback(() => {
    setRunning(true);
    setWatch(sequence => sequence + 1);
  }, []);

  const check = useCallback(async (signal?: AbortSignal) => {
    setBusy('check');
    setRelease(null);
    setCanReload(false);
    setMessage('');
    setProgress(null);
    try {
      const result = await api.checkUpdate(signal);
      if (signal?.aborted) return;
      setRelease(result);
      if (result.state === 'running') {
        setMessage(result.message);
        setProgress(result.progress);
        watchUpdate();
      }
      else if (!result.available) setMessage('You’re up to date.');
      else if (result.managed) setMessage(`Syvrio ${result.latest} is available.`);
      else setMessage(`Syvrio ${result.latest} is available. Update with your package manager.`);
    } catch (error) {
      if (!signal?.aborted) setMessage(error instanceof Error ? error.message : 'Could not check for updates.');
    } finally {
      if (!signal?.aborted) setBusy(null);
    }
  }, [watchUpdate]);

  // 先恢复进行中的任务，用户主动打开“关于”时只读取本地状态。
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
        setProgress(null);
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
      setProgress(status.state === 'running' ? status.progress : null);
      if (status.state === 'running') {
        tracking = true;
        setRunning(true);
        setMessage(status.message || 'Installing the update…');
        timer = window.setTimeout(poll, POLL_INTERVAL);
      } else if (checkOnOpen && !tracking) {
        await check(controller.signal);
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
  }, [watch, checkOnOpen, check]);

  const install = async () => {
    if (!release || !confirm) return;
    setConfirm(false);
    setBusy('install');
    setMessage('Requesting installation…');
    setProgress(null);
    setRelease(null);
    try {
      const accepted = await api.installUpdate(release.latest);
      setMessage(accepted.message);
      setProgress(accepted.progress);
      watchUpdate();
    } catch (error) {
      if (error instanceof ApiError && error.status && error.status < 500) {
        setMessage(error.message);
      } else {
        // 网关重启可能中断回执，此时查询任务状态，避免重复提交 POST。
        setMessage('Checking whether the update started…');
        watchUpdate();
      }
    } finally {
      setBusy(null);
    }
  };

  const active = running || busy === 'install';
  const downloaded = progress?.downloaded;
  const total = progress?.total;
  const measured = downloaded != null && total != null && total > 0;
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
    {message && <p role="status" className="settings-hint update-result">
      {active && progress ? progress.message : message}
    </p>}
    {active && <div className="update-progress">
      <progress aria-label={progress?.message || message || 'Updating Syvrio'}
        value={measured ? downloaded : undefined} max={measured ? total : undefined} />
      {downloaded != null && <small>{downloadSize(downloaded)}
        {measured && ` / ${downloadSize(total)} · ${Math.floor(downloaded * 100 / total)}%`}</small>}
    </div>}
    {createPortal(<SurfacePresence modal>
      {confirm && release && <ConfirmDialog title="Install software update?" confirmLabel="Install and restart" focusCancel
        message={`Update Syvrio from ${release.current} to ${release.latest}? The service will restart and connected clients will briefly disconnect.`}
        onCancel={() => { setConfirm(false); trigger.current?.focus(); }}
        onConfirm={() => void install()} />}
    </SurfacePresence>, document.body)}
  </>;
}

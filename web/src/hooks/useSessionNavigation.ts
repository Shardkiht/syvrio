import { useCallback, useRef, useState } from 'react';
import { api, ApiError, NetworkError, queueState } from '../api/client';
import type { ChatState } from '../state/chat';

type Options = {
  currentSession: () => string | null;
  onRequested: (id: string | null, waiting: boolean) => void;
  switchHistory: (id: string) => Promise<ChatState | undefined>;
  onSelected: (id: string | null, latest: boolean, confirmed?: ChatState) => void;
  onFailure: (error: unknown) => void;
  onSettled: () => void;
  onDeliveryError: (error: unknown) => void;
};

/** 同一浏览器视图的变更与消息受理和编辑按顺序执行。
 * 后端确认收到消息后仍会继续生成。 */
export function useSessionNavigation({ currentSession, onRequested, switchHistory, onSelected, onFailure, onSettled, onDeliveryError }: Options) {
  const navigation = useRef<Promise<boolean> | null>(null);
  const submissions = useRef<Promise<void> | null>(null);
  const editing = useRef<Promise<void> | null>(null);
  const [undoing, setUndoing] = useState(false);
  const unconfirmed = useRef<{ target: string | null } | null>(null);
  const [checkingSelection, setCheckingSelection] = useState(false);

  const reconcileSelection = useCallback((actual: string | null) => {
    const pending = unconfirmed.current;
    if (!pending) return;
    unconfirmed.current = null;
    setCheckingSelection(false);
    if (actual === pending.target) onSelected(actual, true);
  }, [onSelected]);

  const beginEdit = useCallback(() => {
    let resolve!: () => void;
    editing.current = new Promise<void>(done => { resolve = done; });
    setUndoing(true);
    return () => {
      editing.current = null;
      setUndoing(false);
      resolve();
    };
  }, []);

  const navigate = useCallback((id: string | null, command?: string) => {
    onRequested(id, Boolean(navigation.current || submissions.current || editing.current));
    setCheckingSelection(true);
    const next: Promise<boolean> = Promise.all([navigation.current, submissions.current, editing.current]).then(async () => {
      if (navigation.current !== next) return false;
      unconfirmed.current = null;
      try {
        let confirmed: ChatState | undefined;
        if (command) {
          const result = await api.runCommand({ line: command, session_id: currentSession(), confirmed: false });
          if (result.status !== 'ok') throw new Error(result.message);
        } else if (id === null) {
          await api.createSession();
        } else {
          confirmed = await switchHistory(id);
        }
        onSelected(id, navigation.current === next, confirmed);
        return true;
      } catch (error) {
        // 回执丢失前，网关可能已更改绑定。
        const uncertain = error instanceof NetworkError || error instanceof TypeError || error instanceof SyntaxError ||
          (error instanceof ApiError && error.status != null && error.status >= 500);
        if (uncertain) {
          try {
            const state = await queueState();
            if (state.session_id === id) {
              onSelected(id, navigation.current === next);
              return true;
            }
          } catch {
            if (navigation.current === next) {
              unconfirmed.current = { target: id };
            }
          }
        }
        if (navigation.current === next) onFailure(error);
        return false;
      }
    });
    navigation.current = next;
    void next.finally(() => {
      if (navigation.current === next) {
        navigation.current = null;
        setCheckingSelection(unconfirmed.current !== null);
        onSettled();
      }
    });
    return next;
  }, [currentSession, onFailure, onRequested, onSelected, onSettled, switchHistory]);

  const queueSubmission = useCallback((task: () => Promise<void>) => {
    const preceding = navigation.current;
    const next = (submissions.current ?? Promise.resolve()).then(async () => {
      if (preceding && !await preceding) throw new Error('会话未能切换，请在目标会话重新发送');
      if (unconfirmed.current) throw new Error('等待重新连接以确认当前会话');
      await task();
    }).catch(onDeliveryError);
    submissions.current = next;
    void next.then(() => { if (submissions.current === next) submissions.current = null; });
    return next;
  }, [onDeliveryError]);

  const submitWhenSelected = useCallback(async (matches: () => boolean, task: () => Promise<void>) => {
    const desired = navigation.current;
    if (desired && !await desired) return;
    if ((navigation.current && navigation.current !== desired) || unconfirmed.current || !matches()) return;
    await queueSubmission(task);
  }, [queueSubmission]);

  return { navigation, navigate, queueSubmission, submitWhenSelected, editing, beginEdit, undoing, checkingSelection, reconcileSelection };
}

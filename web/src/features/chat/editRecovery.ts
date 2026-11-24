import type { MessageDraft } from '../../types';
import { removeEditFiles } from './editFiles';

const prefix = 'syvrio.edit.v1.';
export const tabOnly = 'Browser storage is unavailable. Keep this tab open to retain this edit.';
export const missingFiles = 'Attachment copies are unavailable after a refresh. Save them from this tab if possible.';

export type EditedDraft = MessageDraft & {
  editId?: string;
  files?: readonly File[];
  filesStored?: boolean;
  receiptUnknown?: boolean;
};
export type FailedEdit = {
  sessionId: string;
  messageId: string;
  draft: EditedDraft;
  error: string;
  mode: 'compose' | 'retry' | 'edit' | 'stale';
  dirty?: boolean;
  warning?: string;
};

export function persistEdit(edit: FailedEdit): FailedEdit {
  const { files: _files, ...draft } = edit.draft;
  const current = edit.warning === tabOnly ? { ...edit, warning: undefined } : edit;
  const key = prefix + edit.sessionId;
  try {
    sessionStorage.setItem(key, JSON.stringify({ ...current, draft }));
    return current;
  } catch {
    // 存储额度不足时，要防止旧的失实草稿继续留在磁盘上。
    try { sessionStorage.removeItem(key); } catch { /* 无法访问本地存储。 */ }
    return { ...edit, warning: tabOnly };
  }
}

export function loadEdits(): Record<string, FailedEdit> {
  const entries: [string, FailedEdit][] = [];
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (!key.startsWith(prefix)) continue;
      try {
        const edit: FailedEdit = JSON.parse(sessionStorage.getItem(key) ?? 'null');
        if (key === prefix + edit.sessionId && /^[a-f0-9]{32}$/.test(edit.draft.editId ?? '')
            && typeof edit.messageId === 'string' && typeof edit.draft.text === 'string'
            && Array.isArray(edit.draft.attachments) && ['compose', 'retry', 'edit', 'stale'].includes(edit.mode)) {
          if (edit.mode === 'compose' && !edit.dirty) forgetEdit(edit.sessionId, edit.draft.editId);
          else entries.push([edit.sessionId, edit]);
        }
      } catch { /* 忽略损坏条目，保留其他会话的数据。 */ }
    }
  } catch { /* 本地存储可能被禁用，新编辑仍可在内存中进行。 */ }
  return Object.fromEntries(entries);
}

export function forgetEdit(sessionId: string, id?: string) {
  try {
    const key = prefix + sessionId;
    const saved = sessionStorage.getItem(key);
    const storedId: string | undefined = saved ? JSON.parse(saved).draft.editId : undefined;
    if (!id || id === storedId) sessionStorage.removeItem(key);
    id ??= storedId;
  } catch { /* 仅从内存恢复的内容也可以关闭。 */ }
  if (id) void removeEditFiles(id).catch(() => undefined);
}

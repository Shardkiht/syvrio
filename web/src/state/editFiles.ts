/** 保存编辑所需的二进制副本，因为原件可能从聊天历史中消失。 */
const database = 'syvrio-edit-recovery';
const storeName = 'attachments';
const week = 7 * 24 * 60 * 60 * 1000;

type Batch = { saved: number; files: Record<string, Blob> };

async function access<T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const open = indexedDB.open(database, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(storeName).createIndex('saved', 'saved');
    open.onerror = () => reject(open.error);
    open.onsuccess = () => resolve(open.result);
  });
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(storeName, mode);
      const store = tx.objectStore(storeName);
      if (mode === 'readwrite') {
        // 标签页关闭后，其 sessionStorage 清单会丢失，残留文件副本需到期清理。
        const expired = store.index('saved').openKeyCursor(IDBKeyRange.upperBound(Date.now() - week));
        expired.onsuccess = () => {
          const cursor = expired.result;
          if (cursor) { store.delete(cursor.primaryKey); cursor.continue(); }
        };
      }
      const request = action(store);
      tx.oncomplete = () => resolve(request.result);
      tx.onabort = () => reject(tx.error ?? request.error);
    });
  } finally {
    db.close();
  }
}

export async function saveEditFiles(id: string, ids: string[], files: readonly File[]) {
  const batch: Batch = { saved: Date.now(), files: Object.fromEntries(ids.map((key, i) => [key, files[i]])) };
  await access('readwrite', store => store.put(batch, id));
}

export async function readEditFiles(id: string, items: { id: string; name: string; media_type: string }[]) {
  const batch: Batch | undefined = await access('readonly', store => store.get(id));
  return items.map(item => {
    const blob = batch?.files[item.id];
    if (!blob) throw new Error('Saved attachment unavailable');
    return new File([blob], item.name, { type: item.media_type });
  });
}

export async function removeEditFiles(id: string) {
  await access('readwrite', store => store.delete(id));
}

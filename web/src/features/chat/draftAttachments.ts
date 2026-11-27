import type { AttachmentPolicy } from '../../types';

const imageExtensions: Record<string, string> = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' };

export function clipboardFiles(data: DataTransfer): File[] {
  const files = Array.from(data.files);
  if (!files.length) {
    for (const item of Array.from(data.items)) {
      if (item.kind !== 'file') continue;
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  return files.map(file => {
    const ext = imageExtensions[file.type];
    if (!ext || /\.[^./]+$/.test(file.name)) return file;
    return new File([file], (file.name || 'pasted-image') + '.' + ext, { type: file.type, lastModified: file.lastModified });
  });
}

const MiB = 1024 * 1024;

function validateBatch(files: File[], policy: AttachmentPolicy) {
  if (files.length > policy.max_files || files.reduce((sum, file) => sum + file.size, 0) > policy.max_batch_bytes) {
    throw new Error(`Attach at most ${policy.max_files} files and ${policy.max_batch_bytes / MiB} MiB per message.`);
  }
  return files;
}

function readBytes(file: File): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(new Error('Could not read the selected file. Select it again.'));
    reader.readAsArrayBuffer(file);
  });
}

async function compareContents(files: File[], policy: AttachmentPolicy) {
  const unique: File[] = [];
  const cache = new Map<File, Uint8Array>();
  async function bytes(file: File) {
    const saved = cache.get(file);
    if (saved) return saved;
    const data = await readBytes(file);
    cache.set(file, data);
    return data;
  }
  for (const file of files) {
    let duplicate = false;
    for (const other of unique) {
      if (other.size !== file.size) continue;
      const a = await bytes(file);
      const b = await bytes(other);
      if (a.every((byte, index) => byte === b[index])) {
        duplicate = true;
        break;
      }
    }
    if (duplicate) cache.delete(file);
    else {
      unique.push(file);
      validateBatch(unique, policy);
    }
  }
  return unique;
}

export function uniqueDraftFiles(selection: File[], policy: AttachmentPolicy): File[] | Promise<File[]> {
  const files = [...new Set(selection)];
  for (const file of files) {
    const name = file.name;
    const dot = name.lastIndexOf('.');
    const chars = Array.from(name);
    if (dot < 1 || name.includes('/') || name.includes('\\') || /^[a-z]:/i.test(name)
      || chars.length > policy.max_name_length || chars.some(char => char.charCodeAt(0) < 32)
      || !policy.extensions.includes(name.slice(dot).toLowerCase())) {
      throw new Error('Unsupported attachment name or format.');
    }
    if (!file.size) throw new Error('Empty attachments cannot be read.');
    if (file.size > policy.max_file_bytes) {
      throw new Error(`Attach files of at most ${policy.max_file_bytes / MiB} MiB each.`);
    }
  }
  // 长度不同必定不是同一份内容，普通选择无需读取文件或等待异步比较。
  return new Set(files.map(file => file.size)).size === files.length
    ? validateBatch(files, policy) : compareContents(files, policy);
}

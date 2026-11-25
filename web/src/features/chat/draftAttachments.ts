export const attachmentTypes = '.md,.markdown,.txt,.text,.log,.json,.jsonl,.ndjson,.csv,.tsv,.yaml,.yml,.xml,.pdf,.docx,.xlsx,.xls,.pptx,.html,.htm,.epub,.msg,.ipynb,.png,.jpg,.jpeg,.webp,.gif';
const extensions = new Set(attachmentTypes.split(','));
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
const limitMessage = 'Choose up to 8 non-empty files, 20 MiB each and 40 MiB total.';

function validateBatch(files: File[]) {
  if (files.length > 8 || files.reduce((sum, file) => sum + file.size, 0) > 40 * MiB) {
    throw new Error(limitMessage);
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

async function compareContents(files: File[]) {
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
      validateBatch(unique);
    }
  }
  return unique;
}

export function uniqueDraftFiles(selection: File[]): File[] | Promise<File[]> {
  const files = [...new Set(selection)];
  if (files.some(file => !extensions.has(file.name.match(/\.[^.]+$/)?.[0].toLowerCase() ?? ''))) {
    throw new Error('Unsupported attachment format.');
  }
  if (files.some(file => !file.size || file.size > 20 * MiB)) throw new Error(limitMessage);
  // 长度不同必定不是同一份内容，普通选择无需读取文件或等待异步比较。
  return new Set(files.map(file => file.size)).size === files.length ? validateBatch(files) : compareContents(files);
}

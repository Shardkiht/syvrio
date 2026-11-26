import type { AttachmentPolicy } from '../types';

export const testAttachmentPolicy: AttachmentPolicy = {
  extensions: ['.txt', '.md', '.pdf', '.png', '.gif'],
  max_files: 6,
  max_file_bytes: 12 * 1024 * 1024,
  max_batch_bytes: 24 * 1024 * 1024,
  max_name_length: 255,
};

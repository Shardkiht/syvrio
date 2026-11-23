import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CommandDialog } from './CommandDialog';
import { FileIcon } from './Icons';
import { SurfacePresence } from './SurfacePresence';

export function isPreviewImage(mediaType: string) {
  return ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(mediaType);
}

export function ImagePreview({ src, name, draft = false, linked = false, title }: {
  src: string; name: string; draft?: boolean; linked?: boolean; title?: string;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [actualSize, setActualSize] = useState(false);
  const [visited, setVisited] = useState(false);
  const close = () => {
    setOpen(false);
    trigger.current?.focus({ preventScroll: true });
  };
  if (failed) return <span className="image-unavailable" title={name}>Preview unavailable</span>;
  const image = <img src={src} alt={linked ? name : ''} title={title}
    loading={draft ? 'eager' : 'lazy'} decoding="async" onError={() => setFailed(true)} />;
  if (linked) return <span className="message-image">{image}</span>;
  return <>
    <button type="button" className={draft ? 'draft-image' : 'message-image'} ref={trigger}
      aria-haspopup="dialog" aria-label={`Preview ${name}`}
      onClick={() => { setActualSize(false); setVisited(true); setOpen(true); }}>
      {image}
    </button>
    {visited && createPortal(<SurfacePresence modal>
      {open && <CommandDialog title={`Preview ${name}`} className="command-panel attachment-viewer"
        onKeyDown={event => event.stopPropagation()} onClose={close}>
        <header><strong title={name}>{name}</strong>
          <button type="button" className="image-size-toggle" aria-pressed={actualSize}
            onClick={() => setActualSize(value => !value)}>{actualSize ? 'Fit to screen' : 'Actual size'}</button>
          <button type="button" className="icon-button" aria-label="Close image preview" onClick={close}>×</button>
        </header>
        <div className="image-viewport" data-actual-size={actualSize || undefined}
          tabIndex={actualSize ? 0 : undefined} aria-label={actualSize ? 'Scrollable image' : undefined}
          role={actualSize ? 'region' : undefined}>
          <img src={src} alt={name} onError={() => { close(); setFailed(true); }} />
        </div>
      </CommandDialog>}
    </SurfacePresence>, document.body)}
  </>;
}

export function DraftAttachmentIcon({ file }: { file: File }) {
  const [preview, setPreview] = useState<{ file: File; url: string } | null>(null);
  useEffect(() => {
    const image = isPreviewImage(file.type) || (!file.type && /\.(?:png|jpe?g|webp|gif)$/i.test(file.name));
    if (!image || typeof URL.createObjectURL !== 'function') return;
    const url = URL.createObjectURL(file);
    setPreview({ file, url });
    return () => URL.revokeObjectURL(url);
  }, [file]);
  return preview?.file === file
    ? <ImagePreview key={preview.url} src={preview.url} name={file.name} draft />
    : <FileIcon size={18} />;
}

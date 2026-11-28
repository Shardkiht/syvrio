import { RefreshIcon } from '../../shared/Icons';
import type { Delivery } from './useDeliveries';

export function OutgoingMessage({ item, retry, discard }: {
  item: Delivery;
  retry: (item: Delivery) => Promise<void>;
  discard: (id: string) => void;
}) {
  const errorId = `delivery-error-${item.id}`;
  return <div className="turn user outgoing" data-testid="user-message">
    <div className="delivery-line">
      {item.error ? (
        <button type="button" className="delivery-retry" aria-label="Retry delivery" aria-describedby={errorId}
          title="Retry delivery" onClick={() => void retry(item)}><RefreshIcon size={16} /></button>
      ) : !item.receipt && (
        <span className="delivery-dots" role="status" aria-label="Sending message">
          <i /><i /><i />
        </span>
      )}
      <div className="bubble">{item.text}</div>
    </div>
    {item.attachments.length > 0 && <ul className="message-attachments" aria-label="Message attachments">
      {item.attachments.map(file => <li key={file.id}>{file.name}</li>)}
    </ul>}
    {item.error && <div className="delivery-error">
      <span id={errorId} role="status">{item.error}</span>
      <button type="button" onClick={() => discard(item.id)}>Dismiss</button>
    </div>}
  </div>;
}

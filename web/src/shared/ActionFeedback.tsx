export interface ActionFeedback {
  kind: 'pending' | 'success' | 'warning' | 'error';
  message: string;
}

export function FeedbackMessage({ feedback }: { feedback: ActionFeedback | null }) {
  return (
    <p className="action-feedback" data-kind={feedback?.kind} aria-atomic="true"
      role={feedback?.kind === 'error' ? 'alert' : 'status'}>
      {feedback?.message}
    </p>
  );
}

/**
 * "Should we reply?" panel (issue #4).
 *
 * Shows what the research model proposed about the post, what code checked,
 * which skip rule fired (if any), and the confidence inputs code computed.
 * Cards stored before issue #4 have no reply_fit; the panel then renders nothing.
 */

import type { ReplyFit } from '../api/client';

interface ReplyFitPanelProps {
  replyFit?: ReplyFit | null;
  confidenceInputs: Record<string, number>;
}

const SKIP_RULE_LABELS: Record<string, string> = {
  replies_limited: 'Replies are limited',
  research_incomplete: 'Research incomplete',
  vendor_post: 'Vendor post',
  broadcast_post: 'Broadcast post with no question',
  no_problem_span: 'No problem the brief solves',
  others_thread: "Inside someone else's conversation",
  no_founder_material: 'No supporting founder material',
};

const SPAN_CHECK_LABELS: Record<string, string> = {
  ok: 'found in the post',
  missing: 'none proposed',
  not_in_post: 'not found in the post',
  too_short: 'too short',
  not_checked: 'not checked',
};

function yesNo(value: boolean | null): string {
  if (value === null) return 'unknown';
  return value ? 'yes' : 'no';
}

export function ReplyFitPanel({ replyFit, confidenceInputs }: ReplyFitPanelProps) {
  if (!replyFit) return null;

  const threadSource =
    replyFit.thread_source === 'metadata' ? 'from X metadata'
    : replyFit.thread_source === 'model' ? 'from research'
    : 'unknown';

  return (
    <div style={{
      marginBottom: '12px',
      padding: '12px',
      border: '1px solid #ddd',
      borderRadius: '4px',
      fontSize: '14px',
    }}>
      <strong>Should we reply?</strong>{' '}
      {replyFit.skip_rule ? (
        <span style={{ color: '#c62828' }}>
          No: {SKIP_RULE_LABELS[replyFit.skip_rule] ?? replyFit.skip_rule}
        </span>
      ) : (
        <span style={{ color: '#2e7d32' }}>No skip rule fired</span>
      )}
      <ul style={{ margin: '8px 0 0', paddingLeft: '20px' }}>
        <li>Post kind: {replyFit.post_kind ?? 'unknown'}</li>
        <li>
          Problem the brief solves:{' '}
          {replyFit.brief_problem_span ? <em>“{replyFit.brief_problem_span}”</em> : 'none'}
          {' '}({SPAN_CHECK_LABELS[replyFit.span_check] ?? replyFit.span_check})
        </li>
        <li>
          In someone else's thread: {yesNo(replyFit.in_others_thread_used)} ({threadSource})
        </li>
        <li>Vendor post: {yesNo(replyFit.is_vendor_post)}</li>
      </ul>
      <details style={{ marginTop: '8px' }}>
        <summary style={{ cursor: 'pointer', color: '#666' }}>
          Confidence inputs (rule {replyFit.rule_version})
        </summary>
        <ul style={{ margin: '4px 0 0', paddingLeft: '20px', color: '#666' }}>
          {Object.entries(confidenceInputs).map(([key, value]) => (
            <li key={key}>
              {key}: {typeof value === 'number' ? value.toFixed(2) : String(value)}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

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
  others_thread: "Replying in another account's conversation",
  no_founder_material: 'No supporting founder material',
};

const SPAN_CHECK_LABELS: Record<string, string> = {
  ok: 'found in the post',
  missing: 'none proposed',
  not_in_post: 'not found in the post',
  too_short: 'too few content words',
  off_brief: 'shares no words with the brief',
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
    <div className="ev-box">
      <strong>Should we reply?</strong>{' '}
      {replyFit.skip_rule ? (
        <span className="ev-fail">
          No: {SKIP_RULE_LABELS[replyFit.skip_rule] ?? replyFit.skip_rule}
        </span>
      ) : (
        <span className="ev-pass">No skip rule fired</span>
      )}
      <ul className="ev-list">
        <li>Post kind: {replyFit.post_kind ?? 'unknown'}</li>
        <li>
          Problem the brief solves:{' '}
          {replyFit.brief_problem_span ? <em>“{replyFit.brief_problem_span}”</em> : 'none'}
          {' '}({SPAN_CHECK_LABELS[replyFit.span_check] ?? replyFit.span_check})
          {replyFit.rejected_span && (
            <div className="ev-item__sub">
              Research proposed a span that was rejected (not post text): {replyFit.rejected_span}
            </div>
          )}
        </li>
        <li>
          Replying to another account: {yesNo(replyFit.in_others_thread_used)} ({threadSource})
        </li>
        <li>Vendor post: {yesNo(replyFit.is_vendor_post)}</li>
        {(replyFit.materials_dropped ?? 0) > 0 && (
          <li>
            Founder passages dropped (not found in the brief): {replyFit.materials_dropped}
          </li>
        )}
      </ul>
      <details className="ev-details" style={{ marginTop: 'var(--op-space-2)' }}>
        <summary>
          Confidence inputs (rule {replyFit.rule_version})
        </summary>
        <ul className="ev-list app-muted">
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

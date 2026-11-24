/**
 * ReplyPlanPanel - the reply plan (issue #5), shown above the draft.
 *
 * Shows the reply type, the part of the post the reply responds to, and the
 * product-mention decision with its reason, so the founder can see why the
 * draft took the shape it did. A plan that failed validation is shown with
 * the reasons it was rejected.
 */

import type { ReviewCard as ReviewCardType } from '../api/client';

const REPLY_TYPE_LABELS: Record<string, string> = {
  answer_question: 'Answer their question',
  add_observation: 'React to their point',
  share_experience: 'Share our experience',
};

interface ReplyPlanPanelProps {
  evidence: ReviewCardType['evidence'];
  // True when the current draft text is a founder edit. The edit gate state
  // is set by an edit and cleared when a gated nudge replaces the draft.
  edited: boolean;
}

export function ReplyPlanPanel({ evidence, edited }: ReplyPlanPanelProps) {
  const plan = evidence.reply_plan;
  if (!plan) {
    return null;
  }
  const errors = evidence.reply_plan_errors ?? [];
  const rejected = errors.length > 0;
  const attempts = evidence.reply_plan_attempts ?? 1;
  const draftFailures = evidence.draft?.plan_conformance_failures ?? [];

  return (
    <div style={{ marginBottom: '20px' }}>
      <h4>Reply Plan</h4>
      <div style={{
        backgroundColor: rejected ? '#ffebee' : '#f3f8ff',
        border: `1px solid ${rejected ? '#ef9a9a' : '#bbdefb'}`,
        padding: '12px',
        borderRadius: '4px',
        fontSize: '14px',
      }}>
        <div style={{ marginBottom: '6px' }}>
          <strong>Reply type:</strong>{' '}
          {REPLY_TYPE_LABELS[plan.reply_type] ?? (plan.reply_type || '(none)')}
          <span style={{ color: '#666', fontSize: '12px' }}> ({plan.reply_type || 'missing'})</span>
        </div>
        <div style={{ marginBottom: '6px' }}>
          <strong>Responds to:</strong>{' '}
          {plan.target_point ? <q>{plan.target_point}</q> : <em>(no target point)</em>}
        </div>
        <div>
          <strong>Mention the product:</strong>{' '}
          {plan.mention_product === true ? 'Yes' : plan.mention_product === false ? 'No' : '(not decided)'}
          {plan.mention_product_reason && (
            <span style={{ color: '#666' }}> — {plan.mention_product_reason}</span>
          )}
        </div>
        {plan.mention_product === false && !rejected && edited && evidence.draft && (
          <div style={{ fontSize: '12px', color: '#666', marginTop: '6px' }}>
            The drafted reply had no product claim; the text below has since been edited (see the edit gate results).
          </div>
        )}
        {plan.mention_product === false && !rejected && !edited && evidence.draft && !evidence.reply_plan_skip_rule && (
          <div style={{ fontSize: '12px', color: '#666', marginTop: '6px' }}>
            No product claim: the draft below uses fixed phrases only.
            {(plan.reply_type === 'answer_question' || plan.reply_type === 'share_experience') && (
              <> Fixed phrases cannot {plan.reply_type === 'answer_question' ? 'answer the question' : 'share our experience'};
              {' '}edit the draft in your own words (edits are re-gated) or skip.</>
            )}
          </div>
        )}
        {attempts > 1 && (
          <div style={{ fontSize: '12px', color: '#666', marginTop: '6px' }}>
            Plan requested {attempts} times; earlier plans failed validation.
          </div>
        )}
        {evidence.reply_plan_skip_rule === 'needs_written_drafts' && (
          <div style={{ marginTop: '8px', fontSize: '12px', color: '#c62828' }}>
            <strong>Skipped:</strong> this reply needs written drafts. Assembled drafts can only use
            fixed phrases here, and fixed phrases cannot{' '}
            {plan.reply_type === 'answer_question' ? 'answer the question' : 'share our experience'}.
          </div>
        )}
        {draftFailures.length > 0 && (
          <div style={{ marginTop: '8px', fontSize: '12px', color: '#c62828' }}>
            <strong>Draft does not follow this plan:</strong> {draftFailures.join('; ')}
          </div>
        )}
        {rejected && (
          <div style={{ marginTop: '8px' }}>
            <div style={{ fontWeight: 'bold', color: '#c62828' }}>Plan rejected:</div>
            <ul style={{ margin: '4px 0 0 0', paddingLeft: '20px', color: '#c62828', fontSize: '12px' }}>
              {errors.map((error, idx) => (
                <li key={idx}>{error}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

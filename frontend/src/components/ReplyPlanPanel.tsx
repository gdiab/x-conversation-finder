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
    <section className="rq-sec">
      <h4 className="rq-sec__title">Reply Plan</h4>
      <div className={rejected ? 'ev-box ev-item--fail' : 'ev-box'}>
        <div style={{ marginBottom: 'var(--op-space-1)' }}>
          <strong>Reply type:</strong>{' '}
          {REPLY_TYPE_LABELS[plan.reply_type] ?? (plan.reply_type || '(none)')}
          <span className="app-muted" style={{ fontSize: '12px' }}> ({plan.reply_type || 'missing'})</span>
        </div>
        <div style={{ marginBottom: 'var(--op-space-1)' }}>
          <strong>Responds to:</strong>{' '}
          {plan.target_point ? <q>{plan.target_point}</q> : <em>(no target point)</em>}
        </div>
        <div>
          <strong>Mention the product:</strong>{' '}
          {plan.mention_product === true ? 'Yes' : plan.mention_product === false ? 'No' : '(not decided)'}
          {plan.mention_product_reason && (
            <span className="app-muted"> — {plan.mention_product_reason}</span>
          )}
        </div>
        {plan.mention_product === false && !rejected && edited && evidence.draft && (
          <div className="ev-note">
            The drafted reply had no product claim; the text below has since been edited (see the edit gate results).
          </div>
        )}
        {/* Only when the check classified every sentence as not about the product: a rejected draft may have
            made a claim against the plan, or may never have reached the checker */}
        {plan.mention_product === false && !rejected && !edited && evidence.draft?.mode === 'written'
          && draftFailures.length === 0
          && (evidence.draft.sentence_checks ?? []).length > 0
          && (evidence.draft.sentence_checks ?? []).every((check) => check.verdict === 'not_a_product_claim') && (
          <div className="ev-note">
            No product claim: the written reply below does not mention the product.
          </div>
        )}
        {plan.mention_product === false && !rejected && !edited && evidence.draft && evidence.draft.mode !== 'written' && !evidence.reply_plan_skip_rule && (
          <div className="ev-note">
            No product claim: the draft below uses fixed phrases only.
            {(plan.reply_type === 'answer_question' || plan.reply_type === 'share_experience') && (
              <> Fixed phrases cannot {plan.reply_type === 'answer_question' ? 'answer the question' : 'share our experience'};
              {' '}edit the draft in your own words (edits are re-gated) or skip.</>
            )}
          </div>
        )}
        {attempts > 1 && (
          <div className="ev-note">
            Plan requested {attempts} times; earlier plans failed validation.
          </div>
        )}
        {evidence.reply_plan_skip_rule === 'needs_written_drafts' && (
          <div className="ev-note ev-error">
            <strong>Skipped:</strong> this reply needs written drafts. Assembled drafts can only use
            fixed phrases here, and fixed phrases cannot{' '}
            {plan.reply_type === 'answer_question' ? 'answer the question' : 'share our experience'}.
          </div>
        )}
        {draftFailures.length > 0 && (
          <div className="ev-note ev-error">
            <strong>Draft does not follow this plan:</strong> {draftFailures.join('; ')}
          </div>
        )}
        {rejected && (
          <div style={{ marginTop: 'var(--op-space-2)' }}>
            <div className="ev-fail">Plan rejected:</div>
            <ul className="ev-list ev-error" style={{ fontSize: '12px' }}>
              {errors.map((error, idx) => (
                <li key={idx}>{error}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * SentenceCheckList - the product-claim check of a written draft, per sentence
 * (issue #12; ADR-0006 amendment 2026-11-24, item 6).
 *
 * For each sentence: whether it was checked as a product claim, why (which
 * trigger fired, or the independent checker), the verdict, and the brief
 * sentences that support it. Used for the model's draft and for a founder
 * edit of it, which goes through the same check.
 */

import type { EditGateResult, SentenceCheck, TriggerHit } from '../api/client';
import { Tag } from '@outpost/ds';

const TRIGGER_LABELS: Record<string, string> = {
  product_name: 'names the product',
  first_person_plural: 'uses we/our/us/ours',
  number: 'contains a number',
  comparison: 'makes a comparison',
  feature_term: 'uses a key feature',
};

/** "names the product ("Acme"), uses we/our/us/ours ("our")" */
function describeTriggers(triggers: TriggerHit[]): string {
  const matches = new Map<string, string[]>();
  for (const hit of triggers) {
    const found = matches.get(hit.trigger) ?? [];
    if (hit.match) found.push(`"${hit.match}"`);
    matches.set(hit.trigger, found);
  }
  return [...matches.entries()]
    .map(([trigger, found]) => {
      const label = TRIGGER_LABELS[trigger] ?? trigger;
      return found.length > 0 ? `${label} (${found.join(', ')})` : label;
    })
    .join(', ');
}

function verdictLabel(check: SentenceCheck | EditGateResult): { text: string; accent: 'neutral' | 'oxide' | 'moss' } {
  if (check.kind !== 'claim') {
    return check.passed
      ? { text: 'Not a product claim', accent: 'neutral' }
      : { text: 'Not checked', accent: 'oxide' };
  }
  if (check.verdict === 'supported' && check.passed) {
    return { text: 'Supported by the brief', accent: 'moss' };
  }
  if (check.verdict === 'not_checked') {
    return { text: 'Not checked', accent: 'oxide' };
  }
  if (check.verdict === 'supported') {
    // Supported by the brief, but the reply breaks another rule (see the reason)
    return { text: 'Rejected', accent: 'oxide' };
  }
  return { text: 'Not supported', accent: 'oxide' };
}

interface SentenceCheckListProps {
  checks: Array<SentenceCheck | EditGateResult>;
}

export function SentenceCheckList({ checks }: SentenceCheckListProps) {
  return (
    <div>
      {checks.map((check, idx) => {
        const isClaim = check.kind === 'claim';
        const verdict = verdictLabel(check);
        const triggers = check.triggers ?? [];
        const supporting = check.supporting ?? [];
        return (
          <div
            key={idx}
            data-testid="sentence-check"
            className={!check.passed ? 'ev-item ev-item--fail' : isClaim ? 'ev-item ev-item--pass' : 'ev-item'}
          >
            <div className="ev-item__tags">
              <Tag accent={isClaim ? 'oxide' : 'neutral'}>
                {/* A row the checker was never asked about has no classification, only code's triggers */}
                {isClaim ? 'Product claim' : check.verdict === 'not_checked' ? 'Not classified' : 'Not about the product'}
              </Tag>
              <Tag accent={verdict.accent}>
                {verdict.text}
              </Tag>
            </div>
            <div className="ev-item__text">"{check.sentence}"</div>
            {isClaim && (
              <div className="ev-item__sub">
                <strong>Checked because:</strong>{' '}
                {triggers.length > 0
                  ? `it ${describeTriggers(triggers)}`
                  : check.checker_flagged
                    ? 'the checker read it as a statement about the product'
                    : 'the checker did not clearly say that it is not about the product'}
                {triggers.length > 0 && check.checker_flagged && '; the checker also read it as about the product'}
              </div>
            )}
            {!isClaim && check.passed && (
              <div className="ev-item__sub">
                No trigger fired and the checker saw no product statement. Not checked against the brief; your review is the check.
              </div>
            )}
            {supporting.length > 0 && (
              <div className="ev-item__source">
                <div className="app-label" style={{ marginBottom: 'var(--op-space-1)' }}>
                  Supporting brief {supporting.length === 1 ? 'sentence' : 'sentences'}:
                </div>
                {supporting.map((sentence) => (
                  <div key={sentence.id}>
                    <span className="app-muted">{sentence.id}</span>{' '}
                    {sentence.context && <span className="app-muted">({sentence.context}) </span>}
                    {sentence.text}
                  </div>
                ))}
              </div>
            )}
            {!check.passed && check.reason && (
              <div className="ev-item__sub ev-error">
                <strong>Reason:</strong> {check.reason}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

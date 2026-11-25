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

function verdictLabel(check: SentenceCheck | EditGateResult): { text: string; color: string } {
  if (check.kind !== 'claim') {
    return check.passed
      ? { text: 'Not a product claim', color: '#757575' }
      : { text: 'Not checked', color: '#f44336' };
  }
  if (check.verdict === 'supported' && check.passed) {
    return { text: 'Supported by the brief', color: '#4caf50' };
  }
  if (check.verdict === 'not_checked') {
    return { text: 'Not checked', color: '#f44336' };
  }
  if (check.verdict === 'supported') {
    // Supported by the brief, but the reply breaks another rule (see the reason)
    return { text: 'Rejected', color: '#f44336' };
  }
  return { text: 'Not supported', color: '#f44336' };
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
            style={{
              backgroundColor: !check.passed ? '#ffebee' : isClaim ? '#e3f2fd' : '#f5f5f5',
              padding: '12px',
              borderRadius: '4px',
              marginBottom: '8px',
              border: `1px solid ${!check.passed ? '#f44336' : isClaim ? '#90caf9' : '#e0e0e0'}`,
            }}
          >
            <div style={{ marginBottom: '8px' }}>
              <span style={{
                display: 'inline-block',
                padding: '2px 8px',
                borderRadius: '4px',
                fontSize: '12px',
                fontWeight: 'bold',
                backgroundColor: isClaim ? '#1976d2' : '#757575',
                color: '#fff',
                marginRight: '8px',
              }}>
                {/* A row the checker was never asked about has no classification, only code's triggers */}
                {isClaim ? 'Product claim' : check.verdict === 'not_checked' ? 'Not classified' : 'Not about the product'}
              </span>
              <span style={{
                display: 'inline-block',
                padding: '2px 8px',
                borderRadius: '4px',
                fontSize: '12px',
                fontWeight: 'bold',
                backgroundColor: verdict.color,
                color: '#fff',
              }}>
                {verdict.text}
              </span>
            </div>
            <div style={{ marginBottom: '8px' }}>"{check.sentence}"</div>
            {isClaim && (
              <div style={{ fontSize: '12px', color: '#666' }}>
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
              <div style={{ fontSize: '12px', color: '#666' }}>
                No trigger fired and the checker saw no product statement. Not checked against the brief; your review is the check.
              </div>
            )}
            {supporting.length > 0 && (
              <div style={{
                backgroundColor: '#fff',
                padding: '8px',
                borderRadius: '4px',
                marginTop: '8px',
                fontSize: '12px',
                borderLeft: '3px solid #1976d2',
              }}>
                <div style={{ fontWeight: 'bold', color: '#1976d2', marginBottom: '4px' }}>
                  Supporting brief {supporting.length === 1 ? 'sentence' : 'sentences'}:
                </div>
                {supporting.map((sentence) => (
                  <div key={sentence.id}>
                    <span style={{ color: '#666' }}>{sentence.id}</span>{' '}
                    {sentence.context && <span style={{ color: '#666' }}>({sentence.context}) </span>}
                    {sentence.text}
                  </div>
                ))}
              </div>
            )}
            {!check.passed && check.reason && (
              <div style={{
                fontSize: '12px',
                color: '#c62828',
                padding: '8px',
                backgroundColor: '#fff',
                borderRadius: '4px',
                marginTop: '8px',
              }}>
                <strong>Reason:</strong> {check.reason}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ReviewCard } from './ReviewCard';
import { SentenceCheckList } from './SentenceCheckList';
import {
  makeCard,
  makeEvidence,
  makeReplyPlan,
  makeSentenceCheck,
  makeSupportedClaim,
  makeWrittenDraft,
} from '../test/fixtures';

// Written drafts (issue #12; ADR-0006 amendment 2026-11-24, item 6): for each
// sentence the card shows product claim or not, why it was checked, the
// verdict, and the supporting brief sentences. Synthetic data only.

describe('SentenceCheckList', () => {
  it('shows a supported claim with why it was checked and the brief sentence that supports it', () => {
    render(<SentenceCheckList checks={[makeSupportedClaim()]} />);

    const row = screen.getByTestId('sentence-check');
    expect(within(row).getByText('Product claim')).toBeInTheDocument();
    expect(within(row).getByText('Supported by the brief')).toBeInTheDocument();
    expect(within(row).getByText('"Acme Relay can retry a failed delivery up to five times."')).toBeInTheDocument();
    // Which name matched, and every other trigger that fired
    expect(row).toHaveTextContent('Checked because: it names the product ("Acme Relay"), contains a number ("five")');
    expect(within(row).getByText('Supporting brief sentence:')).toBeInTheDocument();
    expect(row).toHaveTextContent('b2 (Acme Relay) Relay can retry a failed delivery up to five times.');
  });

  it('shows a sentence that is not about the product as left to the founder', () => {
    render(<SentenceCheckList checks={[makeSentenceCheck()]} />);

    const row = screen.getByTestId('sentence-check');
    expect(within(row).getByText('Not about the product')).toBeInTheDocument();
    expect(within(row).getByText('Not a product claim')).toBeInTheDocument();
    expect(row).toHaveTextContent('Not checked against the brief; your review is the check.');
    expect(within(row).queryByText(/Checked because/)).not.toBeInTheDocument();
  });

  it('shows an unsupported claim with the reason', () => {
    const check = makeSupportedClaim({
      sentence: 'Relay will retry a failed delivery.',
      passed: false,
      verdict: 'unsupported',
      supporting: [],
      triggers: [{ trigger: 'product_name', match: 'Relay' }],
      reason: 'Checked as a product claim (names the product ("Relay")). The brief does not support it: The brief says can, not will.',
    });
    render(<SentenceCheckList checks={[check]} />);

    const row = screen.getByTestId('sentence-check');
    expect(within(row).getByText('Not supported')).toBeInTheDocument();
    expect(row).toHaveTextContent('Reason: Checked as a product claim');
    expect(row).toHaveTextContent('The brief says can, not will.');
    expect(within(row).queryByText(/Supporting brief/)).not.toBeInTheDocument();
  });

  it('says so when only the checker put the sentence in the checked set', () => {
    const check = makeSentenceCheck({
      sentence: 'It retries failed deliveries for you.',
      kind: 'claim',
      passed: false,
      verdict: 'unsupported',
      checker_flagged: true,
      reason: 'The brief does not support it.',
    });
    render(<SentenceCheckList checks={[check]} />);

    expect(screen.getByTestId('sentence-check')).toHaveTextContent(
      'Checked because: the checker read it as a statement about the product',
    );
  });

  it('names an alias match and a trigger it has no label for', () => {
    const check = makeSupportedClaim({
      triggers: [
        { trigger: 'product_name', match: 'Relay' },
        { trigger: 'first_person_plural', match: 'our' },
        { trigger: 'future_trigger', match: '' },
      ],
    });
    render(<SentenceCheckList checks={[check]} />);

    expect(screen.getByTestId('sentence-check')).toHaveTextContent(
      'it names the product ("Relay"), uses we/our/us/ours ("our"), future_trigger',
    );
  });

  it('does not call a supported claim unsupported when another rule rejects the reply', () => {
    const check = makeSupportedClaim({
      passed: false,
      reason: 'The reply must have at most 1 product claim, and 2 sentences are checked as product claims.',
    });
    render(<SentenceCheckList checks={[check]} />);

    const row = screen.getByTestId('sentence-check');
    expect(within(row).getByText('Rejected')).toBeInTheDocument();
    expect(within(row).queryByText('Not supported')).not.toBeInTheDocument();
    expect(row).toHaveTextContent('at most 1 product claim');
    expect(within(row).getByText('Supporting brief sentence:')).toBeInTheDocument();
  });

  it('marks a sentence the checker was never asked about as not checked', () => {
    const check = makeSentenceCheck({
      passed: false,
      verdict: 'not_checked',
      reason: 'The reply must have at most 3 sentences (got 4)',
    });
    render(<SentenceCheckList checks={[check]} />);

    const row = screen.getByTestId('sentence-check');
    expect(within(row).getByText('Not checked')).toBeInTheDocument();
    expect(row).toHaveTextContent('at most 3 sentences (got 4)');
    // Nobody classified it, so the card does not say it is "not about the product"
    expect(within(row).getByText('Not classified')).toBeInTheDocument();
    expect(within(row).queryByText('Not about the product')).not.toBeInTheDocument();
  });
});

describe('ReviewCard: written draft (issue #12)', () => {
  const writtenCard = (overrides = {}) =>
    makeCard({
      evidence: makeEvidence({ draft_mode: 'written', draft: makeWrittenDraft() }),
      ...overrides,
    });

  it('shows the per-sentence check instead of the segment display', () => {
    render(<ReviewCard card={writtenCard()} onUpdate={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'Product Claim Check' })).toBeInTheDocument();
    expect(screen.getAllByTestId('sentence-check')).toHaveLength(3);
    expect(screen.queryByRole('heading', { name: 'Reply Segments' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Segments (Evidence Trail)' })).not.toBeInTheDocument();
    expect(screen.getByText('Product claim check:')).toBeInTheDocument();
    expect(screen.queryByText('Claim Gate:')).not.toBeInTheDocument();
  });

  it('names the product, its aliases and the feature terms the check used', () => {
    render(<ReviewCard card={writtenCard()} onUpdate={vi.fn()} />);

    const section = screen.getByRole('heading', { name: 'Product Claim Check' }).parentElement as HTMLElement;
    expect(section).toHaveTextContent('names the product ("Acme Relay", "Relay")');
    expect(section).toHaveTextContent('Feature terms read from your brief: Signed deliveries');
  });

  it('says when the brief lists no feature terms', () => {
    const draft = makeWrittenDraft();
    draft.written = { product_name: 'Acme Relay', product_aliases: [], feature_terms: [] };
    render(<ReviewCard card={makeCard({ evidence: makeEvidence({ draft }) })} onUpdate={vi.fn()} />);

    expect(screen.getByText(/none \(the brief has no list under a "features" heading\)/)).toBeInTheDocument();
  });

  it('shows the edit check, not the stale draft check, once the draft was edited', () => {
    const edit = makeSupportedClaim({
      sentence: 'Ours is 10x quicker.',
      passed: false,
      verdict: 'unsupported',
      supporting: [],
      reason: 'Checked as a product claim. The brief does not support it.',
    });
    const card = writtenCard({ edited_text_gate_state: 'failed', edited_text_gate_results: [edit] });
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'Check of the Edited Text' })).toBeInTheDocument();
    expect(screen.getByText(/The draft was edited after this check/)).toBeInTheDocument();
    // The PASSED summary is labelled as the earlier draft's, so it is not read as the edit's
    expect(screen.getByText("Product claim check of the model's draft (before your edit):")).toBeInTheDocument();
    expect(screen.queryByText('Product claim check:')).not.toBeInTheDocument();
    const rows = screen.getAllByTestId('sentence-check');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('"Ours is 10x quicker."');
    expect(screen.getByText('Gate State: FAILED')).toBeInTheDocument();
    expect(screen.getByText(/Approval requires "Post anyway" acknowledgement/)).toBeInTheDocument();
    expect(screen.getByText(/went through the same product-claim check as the draft/)).toBeInTheDocument();
  });

  it('says so when the check of an edit could not run', () => {
    const card = writtenCard({ edited_text_gate_state: 'not_checked', edited_text_gate_results: null });
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByText(/the check of the edited text could not run/)).toBeInTheDocument();
    expect(screen.queryByTestId('sentence-check')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Check of the Edited Text' })).not.toBeInTheDocument();
  });

  it('empties and closes the edit box when the post is removed while it is open', async () => {
    const user = userEvent.setup();
    const card = writtenCard();
    const { rerender } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    expect(screen.getByDisplayValue(card.evidence.draft?.text ?? '')).toBeInTheDocument();

    // Another Review of the post recorded its removal; the refresh brings the redacted card
    const removed = {
      ...card,
      post: { ...card.post, text: '[removed - post deleted]', text_removed: true },
      evidence: makeEvidence({ draft_mode: 'written', draft: makeWrittenDraft({ text: '[removed - post deleted]', sentence_checks: [] }) }),
    };
    rerender(<ReviewCard card={removed} onUpdate={vi.fn()} />);

    expect(screen.queryByText(/Acme Relay can retry a failed delivery/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save Edit' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    expect(screen.getByDisplayValue('[removed - post deleted]')).toBeInTheDocument();
  });

  it('opens the edit box on the current draft after the card changed', async () => {
    const user = userEvent.setup();
    const card = writtenCard();
    const { rerender } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    const changed = { ...card, evidence: makeEvidence({ draft_mode: 'written', draft: makeWrittenDraft({ text: 'A newer synthetic draft.' }) }) };
    rerender(<ReviewCard card={changed} onUpdate={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));

    expect(screen.getByDisplayValue('A newer synthetic draft.')).toBeInTheDocument();
  });

  it('keeps the segment display for an assembled draft', () => {
    render(<ReviewCard card={makeCard()} onUpdate={vi.fn()} />);

    expect(screen.queryByRole('heading', { name: 'Product Claim Check' })).not.toBeInTheDocument();
    expect(screen.getByText('Claim Gate:')).toBeInTheDocument();
    expect(screen.queryByTestId('sentence-check')).not.toBeInTheDocument();
  });

  it('explains why a review was assembled although written drafts are on', () => {
    const note = 'Written drafts are switched on (DRAFT_MODE=written), but this watch was started without a product name, so this reply was assembled from fixed phrases. Stop the watch and start it again with a product name to get written drafts.';
    const card = makeCard({ evidence: makeEvidence({ draft_mode: 'assembled', draft_mode_note: note }) });
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByRole('note')).toHaveTextContent(note);
  });

  it('does not say "does not mention the product" for a rejected draft that made a claim', () => {
    const claim = makeSupportedClaim({ passed: false, verdict: 'not_checked', supporting: [] });
    const draft = makeWrittenDraft({
      sentence_checks: [claim],
      plan_conformance_failures: ['the reply plan says not to mention the product, but the draft has a product claim'],
    });
    const evidence = makeEvidence({
      draft,
      reply_plan: makeReplyPlan({ mention_product: false }),
    });
    render(<ReviewCard card={makeCard({ status: 'skipped', skip_reason: 'Not enough evidence', evidence })} onUpdate={vi.fn()} />);

    expect(screen.queryByText(/does not mention the product/)).not.toBeInTheDocument();
    expect(screen.getByText(/Draft does not follow this plan/)).toBeInTheDocument();
  });

  it('does not say "does not mention the product" for a draft the checker never classified', () => {
    // Four sentences: rejected in code before the checker ran, so no row has a classification
    const unchecked = makeSentenceCheck({
      passed: false,
      verdict: 'not_checked',
      reason: 'The reply must have at most 3 sentences (got 4)',
    });
    const evidence = makeEvidence({
      draft: makeWrittenDraft({ sentence_checks: [unchecked, unchecked, unchecked, unchecked] }),
      reply_plan: makeReplyPlan({ mention_product: false }),
    });
    render(<ReviewCard card={makeCard({ status: 'skipped', skip_reason: 'Not enough evidence', evidence })} onUpdate={vi.fn()} />);

    expect(screen.queryByText(/does not mention the product/)).not.toBeInTheDocument();
  });

  it('says a written reply without a product claim does not mention the product', () => {
    const draft = makeWrittenDraft({ sentence_checks: [makeSentenceCheck()] });
    const evidence = makeEvidence({
      draft,
      reply_plan: makeReplyPlan({ reply_type: 'answer_question', mention_product: false }),
    });
    render(<ReviewCard card={makeCard({ evidence })} onUpdate={vi.fn()} />);

    expect(screen.getByText(/the written reply below does not mention the product/)).toBeInTheDocument();
    expect(screen.queryByText(/uses fixed phrases only/)).not.toBeInTheDocument();
  });
});

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReplyPlanPanel } from './ReplyPlanPanel';
import { makeDraft, makeEvidence, makeReplyPlan } from '../test/fixtures';

type Evidence = ReturnType<typeof makeEvidence>;

function renderPanel(evidence: Evidence, edited = false) {
  return render(<ReplyPlanPanel evidence={evidence} edited={edited} />);
}

describe('ReplyPlanPanel', () => {
  it('renders nothing for a card stored before the plan step (no reply_plan)', () => {
    const { container } = renderPanel(makeEvidence());
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when reply_plan is null', () => {
    const { container } = renderPanel(makeEvidence({ reply_plan: null }));
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the reply type, target point and product decision with its reason', () => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan() }));

    expect(screen.getByRole('heading', { name: 'Reply Plan' })).toBeInTheDocument();
    expect(screen.getByText(/Reply type:/).parentElement).toHaveTextContent(
      'Reply type: React to their point (add_observation)',
    );
    expect(screen.getByText('onboarding takes weeks')).toBeInTheDocument();
    expect(screen.getByText(/Mention the product:/).parentElement).toHaveTextContent(
      'Mention the product: Yes — the post describes the problem the brief solves',
    );
  });

  it.each([
    ['answer_question', 'Answer their question'],
    ['add_observation', 'React to their point'],
    ['share_experience', 'Share our experience'],
  ])('labels the %s reply type', (type, label) => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan({ reply_type: type }) }));
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText(`(${type})`)).toBeInTheDocument();
  });

  it('shows an unknown reply type as it was proposed', () => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan({ reply_type: 'brand_new_type' }) }));
    expect(screen.getByText(/Reply type:/).parentElement).toHaveTextContent(
      'Reply type: brand_new_type (brand_new_type)',
    );
  });

  it('marks an empty reply type and an empty target point', () => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan({ reply_type: '', target_point: '' }) }));
    expect(screen.getByText(/Reply type:/).parentElement).toHaveTextContent('Reply type: (none) (missing)');
    expect(screen.getByText('(no target point)')).toBeInTheDocument();
  });

  it.each([
    [true, 'Yes'],
    [false, 'No'],
    [null, '(not decided)'],
  ])('shows mention_product=%s as "%s"', (value, text) => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan({ mention_product: value, mention_product_reason: '' }) }));
    expect(screen.getByText(/Mention the product:/).parentElement).toHaveTextContent(
      `Mention the product: ${text}`,
    );
  });

  describe('when the plan says not to mention the product', () => {
    const noMention = (type: string) => makeReplyPlan({ reply_type: type, mention_product: false });

    it('explains that fixed phrases cannot answer a question', () => {
      renderPanel(makeEvidence({ reply_plan: noMention('answer_question') }));
      expect(screen.getByText(/No product claim: the draft below uses fixed phrases only\./)).toHaveTextContent(
        'Fixed phrases cannot answer the question; edit the draft in your own words (edits are re-gated) or skip.',
      );
    });

    it('explains that fixed phrases cannot share an experience', () => {
      renderPanel(makeEvidence({ reply_plan: noMention('share_experience') }));
      expect(screen.getByText(/No product claim:/)).toHaveTextContent('Fixed phrases cannot share our experience');
    });

    it('gives no "cannot" warning for an observation, which fixed phrases can carry', () => {
      renderPanel(makeEvidence({ reply_plan: noMention('add_observation') }));
      const note = screen.getByText(/No product claim:/);
      expect(note).not.toHaveTextContent('Fixed phrases cannot');
    });

    it('says the draft was edited once the founder has edited it', () => {
      renderPanel(makeEvidence({ reply_plan: noMention('answer_question') }), true);
      expect(screen.getByText(/the text below has since been edited/)).toBeInTheDocument();
      expect(screen.queryByText(/No product claim:/)).not.toBeInTheDocument();
    });

    it('shows neither note when the card has no draft', () => {
      renderPanel(makeEvidence({ reply_plan: noMention('answer_question'), draft: null }));
      expect(screen.queryByText(/No product claim:/)).not.toBeInTheDocument();
      expect(screen.queryByText(/has since been edited/)).not.toBeInTheDocument();
    });
  });

  it('shows no fixed-phrase note when the plan allows a product mention', () => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan({ mention_product: true }) }));
    expect(screen.queryByText(/No product claim:/)).not.toBeInTheDocument();
  });

  it('mentions repeated plan requests only when there was more than one attempt', () => {
    const { unmount } = renderPanel(makeEvidence({ reply_plan: makeReplyPlan(), reply_plan_attempts: 1 }));
    expect(screen.queryByText(/Plan requested/)).not.toBeInTheDocument();
    unmount();

    renderPanel(makeEvidence({ reply_plan: makeReplyPlan(), reply_plan_attempts: 2 }));
    expect(screen.getByText('Plan requested 2 times; earlier plans failed validation.')).toBeInTheDocument();
  });

  it('explains a needs_written_drafts skip for each reply type it applies to', () => {
    const { unmount } = renderPanel(
      makeEvidence({
        reply_plan: makeReplyPlan({ reply_type: 'answer_question', mention_product: false }),
        reply_plan_skip_rule: 'needs_written_drafts',
      }),
    );
    expect(screen.getByText('Skipped:')).toBeInTheDocument();
    expect(screen.getByText(/this reply needs written drafts/)).toHaveTextContent(
      'fixed phrases cannot answer the question.',
    );
    // The skip explanation replaces the "edit the draft" hint.
    expect(screen.queryByText(/No product claim:/)).not.toBeInTheDocument();
    unmount();

    renderPanel(
      makeEvidence({
        reply_plan: makeReplyPlan({ reply_type: 'share_experience', mention_product: false }),
        reply_plan_skip_rule: 'needs_written_drafts',
      }),
    );
    expect(screen.getByText(/this reply needs written drafts/)).toHaveTextContent(
      'fixed phrases cannot share our experience.',
    );
  });

  it('lists the ways the draft does not follow the plan', () => {
    renderPanel(
      makeEvidence({
        reply_plan: makeReplyPlan(),
        draft: makeDraft({ plan_conformance_failures: ['mentions the product', 'does not address the target point'] }),
      }),
    );
    expect(screen.getByText(/Draft does not follow this plan:/).parentElement).toHaveTextContent(
      'mentions the product; does not address the target point',
    );
  });

  it('shows no conformance warning when the draft follows the plan', () => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan(), draft: makeDraft({ plan_conformance_failures: [] }) }));
    expect(screen.queryByText(/Draft does not follow this plan/)).not.toBeInTheDocument();
  });

  it('lists the reasons a rejected plan was rejected and drops the fixed-phrase notes', () => {
    renderPanel(
      makeEvidence({
        reply_plan: makeReplyPlan({ mention_product: false }),
        reply_plan_errors: ['target_point is not in the post', 'reply_type is not allowed'],
      }),
    );
    expect(screen.getByText('Plan rejected:')).toBeInTheDocument();
    expect(screen.getByText('target_point is not in the post')).toBeInTheDocument();
    expect(screen.getByText('reply_type is not allowed')).toBeInTheDocument();
    expect(screen.queryByText(/No product claim:/)).not.toBeInTheDocument();
  });

  it('shows no rejection block for an accepted plan', () => {
    renderPanel(makeEvidence({ reply_plan: makeReplyPlan(), reply_plan_errors: [] }));
    expect(screen.queryByText('Plan rejected:')).not.toBeInTheDocument();
  });
});

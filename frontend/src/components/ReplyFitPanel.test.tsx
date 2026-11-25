import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReplyFitPanel } from './ReplyFitPanel';
import { makeReplyFit } from '../test/fixtures';

const INPUTS = { topic_match: 0.9, engagement: 0.5 };

describe('ReplyFitPanel', () => {
  it('renders nothing for a card stored before issue #4 (no reply_fit)', () => {
    const { container: undefinedFit } = render(<ReplyFitPanel confidenceInputs={INPUTS} />);
    expect(undefinedFit).toBeEmptyDOMElement();

    const { container: nullFit } = render(<ReplyFitPanel replyFit={null} confidenceInputs={INPUTS} />);
    expect(nullFit).toBeEmptyDOMElement();
  });

  it('says no skip rule fired and lists the proposed and checked fields', () => {
    render(<ReplyFitPanel replyFit={makeReplyFit()} confidenceInputs={INPUTS} />);

    expect(screen.getByText('Should we reply?')).toBeInTheDocument();
    expect(screen.getByText('No skip rule fired')).toBeInTheDocument();
    expect(screen.getByText('Post kind: question')).toBeInTheDocument();
    expect(screen.getByText(/Problem the brief solves:/)).toHaveTextContent(
      'Problem the brief solves: “slow onboarding for new teammates” (found in the post)',
    );
    expect(screen.getByText('Replying to another account: no (from X metadata)')).toBeInTheDocument();
    expect(screen.getByText('Vendor post: no')).toBeInTheDocument();
  });

  it.each([
    ['replies_limited', 'Replies are limited'],
    ['research_incomplete', 'Research incomplete'],
    ['vendor_post', 'Vendor post'],
    ['broadcast_post', 'Broadcast post with no question'],
    ['no_problem_span', 'No problem the brief solves'],
    ['others_thread', "Replying in another account's conversation"],
    ['no_founder_material', 'No supporting founder material'],
  ])('labels the %s skip rule', (rule, label) => {
    render(<ReplyFitPanel replyFit={makeReplyFit({ skip_rule: rule })} confidenceInputs={INPUTS} />);

    expect(screen.getByText(`No: ${label}`)).toBeInTheDocument();
    expect(screen.queryByText('No skip rule fired')).not.toBeInTheDocument();
  });

  it('shows an unknown skip rule as its raw name', () => {
    render(<ReplyFitPanel replyFit={makeReplyFit({ skip_rule: 'some_future_rule' })} confidenceInputs={INPUTS} />);
    expect(screen.getByText('No: some_future_rule')).toBeInTheDocument();
  });

  it.each([
    ['missing', 'none proposed'],
    ['not_in_post', 'not found in the post'],
    ['too_short', 'too few content words'],
    ['off_brief', 'shares no words with the brief'],
    ['not_checked', 'not checked'],
  ] as const)('explains span check %s', (spanCheck, label) => {
    render(
      <ReplyFitPanel
        replyFit={makeReplyFit({ span_check: spanCheck, brief_problem_span: 'some span' })}
        confidenceInputs={INPUTS}
      />,
    );
    expect(screen.getByText(/Problem the brief solves:/)).toHaveTextContent(`(${label})`);
  });

  it('shows "none" when the research proposed no problem span', () => {
    render(
      <ReplyFitPanel
        replyFit={makeReplyFit({ brief_problem_span: null, span_check: 'missing' })}
        confidenceInputs={INPUTS}
      />,
    );
    expect(screen.getByText(/Problem the brief solves:/)).toHaveTextContent(
      'Problem the brief solves: none (none proposed)',
    );
  });

  it('shows a rejected span with its explanation', () => {
    render(
      <ReplyFitPanel
        replyFit={makeReplyFit({ span_check: 'not_in_post', rejected_span: 'invented synthetic span' })}
        confidenceInputs={INPUTS}
      />,
    );
    expect(
      screen.getByText('Research proposed a span that was rejected (not post text): invented synthetic span'),
    ).toBeInTheDocument();
  });

  it('shows unknown for null flags and a thread source of unknown', () => {
    render(
      <ReplyFitPanel
        replyFit={makeReplyFit({
          post_kind: null,
          in_others_thread_used: null,
          is_vendor_post: null,
          thread_source: 'not_checked',
        })}
        confidenceInputs={INPUTS}
      />,
    );
    expect(screen.getByText('Post kind: unknown')).toBeInTheDocument();
    expect(screen.getByText('Replying to another account: unknown (unknown)')).toBeInTheDocument();
    expect(screen.getByText('Vendor post: unknown')).toBeInTheDocument();
  });

  it('labels where the thread flag came from', () => {
    const { unmount } = render(
      <ReplyFitPanel
        replyFit={makeReplyFit({ in_others_thread_used: true, thread_source: 'model' })}
        confidenceInputs={INPUTS}
      />,
    );
    expect(screen.getByText('Replying to another account: yes (from research)')).toBeInTheDocument();
    unmount();

    render(
      <ReplyFitPanel
        replyFit={makeReplyFit({ in_others_thread_used: true, thread_source: 'missing' })}
        confidenceInputs={INPUTS}
      />,
    );
    expect(screen.getByText('Replying to another account: yes (unknown)')).toBeInTheDocument();
  });

  it('shows the dropped-passages line only when passages were dropped', () => {
    const { unmount } = render(
      <ReplyFitPanel replyFit={makeReplyFit({ materials_dropped: 0 })} confidenceInputs={INPUTS} />,
    );
    expect(screen.queryByText(/Founder passages dropped/)).not.toBeInTheDocument();
    unmount();

    render(<ReplyFitPanel replyFit={makeReplyFit({ materials_dropped: 2 })} confidenceInputs={INPUTS} />);
    expect(screen.getByText('Founder passages dropped (not found in the brief): 2')).toBeInTheDocument();
  });

  it('lists the confidence inputs to two decimals under the rule version', () => {
    render(
      <ReplyFitPanel
        replyFit={makeReplyFit({ rule_version: 'test-rule-v7' })}
        confidenceInputs={{ topic_match: 0.9, engagement: 0.5, author_reach: 0.123 }}
      />,
    );

    const summary = screen.getByText('Confidence inputs (rule test-rule-v7)');
    const details = summary.closest('details')!;
    expect(within(details).getByText('topic_match: 0.90')).toBeInTheDocument();
    expect(within(details).getByText('engagement: 0.50')).toBeInTheDocument();
    expect(within(details).getByText('author_reach: 0.12')).toBeInTheDocument();
  });
});

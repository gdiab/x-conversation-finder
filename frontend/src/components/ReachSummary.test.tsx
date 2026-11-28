import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ReviewCard } from './ReviewCard';
import { compareQueueOrder, queueSortKey } from './ReachSummary';
import { makeCard, makeEvidence, makeReach } from '../test/fixtures';

describe('Reach on the review card (issue #49)', () => {
  it('highlights a card at or above the reach level and shows the raw counts', () => {
    const card = makeCard({ reach: makeReach(), reach_highlighted: true, queue_priority: 0.92 });
    const { container } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByText('High reach')).toBeInTheDocument();
    expect(container.querySelector('[data-high-reach="true"]')).not.toBeNull();
    expect(screen.getByTestId('reach-counts')).toHaveTextContent(
      '120 likes · 23 replies · 3 reposts · 0 quotes · 119,000 followers · Reach: 92%',
    );
  });

  it('does not highlight a scored card below the level, but still shows its counts and reach', () => {
    const card = makeCard({
      reach: makeReach({ score: 0.21, inputs: { like_count: 0, reply_count: 1, repost_count: 0, quote_count: 0, followers_count: 463 } }),
      reach_highlighted: false,
    });
    const { container } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.queryByText('High reach')).not.toBeInTheDocument();
    expect(container.querySelector('[data-high-reach]')).toBeNull();
    expect(screen.getByTestId('reach-counts')).toHaveTextContent(
      '0 likes · 1 replies · 0 reposts · 0 quotes · 463 followers · Reach: 21%',
    );
  });

  it('renders a card without reach (older card or query result) from the post counts', () => {
    const card = makeCard({ reach: null });
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.queryByText('High reach')).not.toBeInTheDocument();
    expect(screen.getByTestId('reach-counts')).toHaveTextContent('3 likes · 1 replies · 1,200 followers');
    expect(screen.getByTestId('reach-counts')).not.toHaveTextContent('Reach');
  });

  it('does not show a badge when the flag is set but the record is missing', () => {
    render(<ReviewCard card={makeCard({ reach_highlighted: true })} onUpdate={vi.fn()} />);
    expect(screen.queryByText('High reach')).not.toBeInTheDocument();
  });
});

describe('Pending queue order (issue #49)', () => {
  it('sorts by queue_priority, and by confidence for a card without one', () => {
    const a = makeCard({ evidence: makeEvidence({ confidence_score: 0.9 }), queue_priority: 0.9 });
    const b = makeCard({ evidence: makeEvidence({ confidence_score: 0.74 }), queue_priority: 0.92 });
    const c = makeCard({ evidence: makeEvidence({ confidence_score: 0.8 }) });

    expect(queueSortKey(c)).toBe(0.8);
    expect([a, b, c].sort(compareQueueOrder)).toEqual([b, a, c]);
  });

  it('puts every pending card before the others, which go newest first', () => {
    const pendingLow = makeCard({ evidence: makeEvidence({ confidence_score: 0.56 }), queue_priority: 0.56 });
    const expiredHigh = makeCard({
      status: 'expired',
      created_at: '2026-11-25T10:00:00Z',
      evidence: makeEvidence({ confidence_score: 0.96 }),
      queue_priority: 0.99,
    });
    const approvedNewer = makeCard({
      status: 'approved',
      created_at: '2026-11-26T10:00:00Z',
      evidence: makeEvidence({ confidence_score: 0.6 }),
      queue_priority: 0.6,
    });

    expect([expiredHigh, approvedNewer, pendingLow].sort(compareQueueOrder)).toEqual([
      pendingLow,
      approvedNewer,
      expiredHigh,
    ]);
  });
});

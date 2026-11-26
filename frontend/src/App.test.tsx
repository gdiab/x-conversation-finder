import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { listReviews } from './api/client';
import { makeCard, makeCardBy, makeEvidence } from './test/fixtures';

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>();
  return { ...actual, listReviews: vi.fn(), startWatch: vi.fn() };
});

const listReviewsMock = vi.mocked(listReviews);

function serve(cards: ReturnType<typeof makeCard>[]) {
  listReviewsMock.mockResolvedValue({ cards, count: cards.length });
}

beforeEach(() => {
  listReviewsMock.mockReset();
});

describe('App review queue', () => {
  it('counts only active cards and hides skipped ones', async () => {
    serve([
      makeCardBy('pendinguser'),
      makeCardBy('approveduser', { status: 'approved' }),
      makeCardBy('skippeduser', { status: 'skipped', skip_reason: 'synthetic skip reason' }),
    ]);

    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Review Queue (2)' })).toBeInTheDocument();
    expect(screen.getByText('Review: pendinguser')).toBeInTheDocument();
    expect(screen.getByText('Review: approveduser')).toBeInTheDocument();
    expect(screen.queryByText('Review: skippeduser')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show skipped (1)' })).toBeInTheDocument();
  });

  it('reveals skipped cards behind the toggle and hides them again', async () => {
    const user = userEvent.setup();
    serve([
      makeCard(),
      makeCardBy('skippeduser', { status: 'skipped', skip_reason: 'synthetic skip reason' }),
    ]);

    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Show skipped (1)' }));

    expect(screen.getByText('Review: skippeduser')).toBeInTheDocument();
    // The queue count does not change when skipped cards are shown.
    expect(screen.getByRole('heading', { name: 'Review Queue (1)' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Hide skipped (1)' }));
    expect(screen.queryByText('Review: skippeduser')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show skipped (1)' })).toBeInTheDocument();
  });

  it('shows no toggle when nothing was skipped', async () => {
    serve([makeCard()]);
    render(<App />);
    await screen.findByRole('heading', { name: 'Review Queue (1)' });
    expect(screen.queryByRole('button', { name: /skipped/i })).not.toBeInTheDocument();
  });

  it('explains an all-skipped queue instead of the empty-start message', async () => {
    serve([makeCard({ status: 'skipped', skip_reason: 'synthetic skip reason' })]);
    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Review Queue (0)' })).toBeInTheDocument();
    expect(screen.getByText(/Every post found so far was skipped/)).toBeInTheDocument();
    expect(screen.queryByText(/No reviews yet/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show skipped (1)' })).toBeInTheDocument();
  });

  it('shows the empty-start message when there are no cards', async () => {
    serve([]);
    render(<App />);

    expect(await screen.findByText(/No reviews yet/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Review Queue (0)' })).toBeInTheDocument();
  });

  it('orders active cards by confidence, highest first', async () => {
    const withConfidence = (username: string, confidence: number) =>
      makeCardBy(username, { evidence: makeEvidence({ confidence_score: confidence }) });
    serve([withConfidence('lowconf', 0.4), withConfidence('highconf', 0.9), withConfidence('midconf', 0.6)]);

    render(<App />);
    await screen.findByRole('heading', { name: 'Review Queue (3)' });

    const order = screen
      .getAllByRole('heading', { level: 3 })
      .map((h) => h.textContent)
      .filter((text) => text?.startsWith('Review: '));
    expect(order).toEqual(['Review: highconf', 'Review: midconf', 'Review: lowconf']);
  });

  it('shows a loading message until the first response arrives', async () => {
    listReviewsMock.mockReturnValue(new Promise(() => {}));
    render(<App />);
    expect(screen.getByText('Loading reviews...')).toBeInTheDocument();
  });

  it('shows the error when loading fails', async () => {
    listReviewsMock.mockRejectedValue(new Error('Failed to list reviews: Internal Server Error'));
    render(<App />);
    expect(await screen.findByText('Failed to list reviews: Internal Server Error')).toBeInTheDocument();
  });

  it('ignores a refresh response that a newer refresh superseded', async () => {
    // The 30 s refresh and a card's reload after an edit can overlap; an
    // older response must not put back the draft the newer one replaced.
    vi.useFakeTimers();
    try {
      const byName = (username: string) => {
        const card = makeCard({ workflow_id: `review:test:${username}` });
        card.post = { ...card.post, author: { ...card.post.author!, username } };
        return { cards: [card], count: 1 };
      };
      listReviewsMock.mockResolvedValueOnce(byName('initial'));
      render(<App />);
      await act(async () => {});

      let finishStale: (value: ReturnType<typeof byName>) => void = () => {};
      listReviewsMock.mockReturnValueOnce(new Promise((resolve) => { finishStale = resolve; }));
      await act(async () => { vi.advanceTimersByTime(30000); });
      listReviewsMock.mockResolvedValueOnce(byName('fresh'));
      await act(async () => { vi.advanceTimersByTime(30000); });
      expect(screen.getByRole('heading', { name: 'Review: fresh' })).toBeInTheDocument();

      await act(async () => finishStale(byName('stale')));

      expect(screen.getByRole('heading', { name: 'Review: fresh' })).toBeInTheDocument();
      expect(screen.queryByRole('heading', { name: 'Review: stale' })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reloads when Refresh is clicked', async () => {
    const user = userEvent.setup();
    serve([]);
    render(<App />);
    await screen.findByText(/No reviews yet/);
    expect(listReviewsMock).toHaveBeenCalledTimes(1);

    serve([makeCard()]);
    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByRole('heading', { name: 'Review Queue (1)' })).toBeInTheDocument();
    expect(listReviewsMock).toHaveBeenCalledTimes(2);
  });
});

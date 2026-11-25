import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  approveReview,
  confirmPostedReview,
  editReview,
  generateRequestId,
  listReviews,
  nudgeReview,
  PostRemovedError,
  ReviewApiError,
  skipReview,
  startWatch,
} from './client';

// src/test/setup.ts replaces global fetch with a stub that throws; each test
// here gives it a canned Response. Nothing reaches the network.
const fetchMock = vi.mocked(fetch);

beforeEach(() => {
  fetchMock.mockReset();
});

const SHOWN = { text: 'Shown synthetic draft.', gateState: null };

describe('approveReview', () => {
  it('posts the URL, request id, post_anyway and the draft shown', async () => {
    fetchMock.mockResolvedValue(new Response('{"message":"ok"}', { status: 200 }));

    await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', false, SHOWN);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/reviews/review-wf-1/approve');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({
      posted_url: 'https://x.example/me/status/9',
      client_request_id: 'req_1',
      post_anyway: false,
      draft_text: 'Shown synthetic draft.',
      draft_gate_state: null,
    });
  });

  it('posts the draft the founder saw (text and gate state), for the server to check', async () => {
    fetchMock.mockResolvedValue(new Response('{"message":"ok"}', { status: 200 }));

    await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', true, {
      text: 'Shown synthetic draft.',
      gateState: 'failed',
    });

    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init?.body as string);
    expect(body.draft_text).toBe('Shown synthetic draft.');
    expect(body.draft_gate_state).toBe('failed');
  });

  it('posts post_anyway=true when the founder overrides the edit gate', async () => {
    fetchMock.mockResolvedValue(new Response('{"message":"ok"}', { status: 200 }));

    await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', true, SHOWN);

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init?.body as string).post_anyway).toBe(true);
  });

  it('turns a 409 post_anyway_required into a ReviewApiError with the failing sentences (issue #15)', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 'post_anyway_required',
          detail: 'Edited text has 1 failing sentence(s). Set post_anyway=true to approve anyway.',
          gate_state: 'failed',
          failing_sentences: ['Unsupported synthetic sentence.'],
        }),
        { status: 409, statusText: 'Conflict' },
      ),
    );

    const err = await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', false, SHOWN).catch((e) => e);
    expect(err).toBeInstanceOf(ReviewApiError);
    expect(err).not.toBeInstanceOf(PostRemovedError);
    expect(err.status).toBe(409);
    expect(err.code).toBe('post_anyway_required');
    expect(err.failingSentences).toEqual(['Unsupported synthetic sentence.']);
    expect(err.retryLater).toBe(false);
    expect(err.message).toBe(
      'Failed to approve review: Edited text has 1 failing sentence(s). Set post_anyway=true to approve anyway.',
    );
  });

  it('throws PostRemovedError for code post_removed (issue #9: the post was removed and the review skipped)', async () => {
    fetchMock.mockResolvedValue(
      new Response('{"code":"post_removed","detail":"Post removed"}', { status: 409, statusText: 'Conflict' }),
    );

    const err = await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', false, SHOWN).catch((e) => e);
    expect(err).toBeInstanceOf(PostRemovedError);
    expect(err.code).toBe('post_removed');
  });

  it('marks not_ready as retry-later, not a failure (issue #20)', async () => {
    fetchMock.mockResolvedValue(
      new Response('{"code":"not_ready","detail":"Cannot approve before the draft card is ready"}', { status: 409 }),
    );

    const err = await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', false, SHOWN).catch((e) => e);
    expect(err).toBeInstanceOf(ReviewApiError);
    expect(err.code).toBe('not_ready');
    expect(err.retryLater).toBe(true);
  });

  it('falls back to the status text when the body is not JSON', async () => {
    fetchMock.mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502, statusText: 'Bad Gateway' }));

    const err = await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', false, SHOWN).catch((e) => e);
    expect(err).toBeInstanceOf(ReviewApiError);
    expect(err.code).toBeNull();
    expect(err.message).toBe('Failed to approve review: Bad Gateway');
  });
});

describe('edit, nudge, confirm and skip errors (issue #15)', () => {
  const failing = () =>
    new Response('{"code":"busy","detail":"Cannot edit while a nudge is in progress. Retry after it completes."}', {
      status: 409,
    });

  it.each([
    ['edit', () => editReview('review-wf-1', 'Synthetic edit.', 'req_1'), 'Failed to edit review: '],
    ['nudge', () => nudgeReview('review-wf-1', 'Shorter', 'req_1'), 'Failed to nudge review: '],
    ['confirm', () => confirmPostedReview('review-wf-1', 'https://x.example/me/status/9', 'req_1'), 'Failed to confirm posted: '],
    ['skip', () => skipReview('review-wf-1', 'Not relevant', 'req_1'), 'Failed to skip review: '],
  ])('%s carries the code and the server message', async (_name, call, prefix) => {
    fetchMock.mockResolvedValue(failing());

    const err = await call().catch((e) => e);
    expect(err).toBeInstanceOf(ReviewApiError);
    expect(err.code).toBe('busy');
    expect(err.retryLater).toBe(true);
    expect(err.message).toBe(`${prefix}Cannot edit while a nudge is in progress. Retry after it completes.`);
  });
});

describe('listReviews', () => {
  it('returns the cards from the response', async () => {
    fetchMock.mockResolvedValue(new Response('{"cards":[],"count":0}', { status: 200 }));
    await expect(listReviews()).resolves.toEqual({ cards: [], count: 0 });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/reviews');
  });

  it('throws with the status text when the API fails', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 502, statusText: 'Bad Gateway' }));
    await expect(listReviews()).rejects.toThrow('Failed to list reviews: Bad Gateway');
  });
});

describe('generateRequestId', () => {
  it('returns a req_ id that differs between calls', () => {
    const first = generateRequestId();
    const second = generateRequestId();
    expect(first).toMatch(/^req_\d+_[a-z0-9]+$/);
    expect(second).not.toBe(first);
  });
});

describe('startWatch (issue #12: product name and aliases)', () => {
  const REQUEST = {
    account: 'synthetic_founder',
    topic: 'webhooks',
    query: 'webhook retries',
    product_brief: 'Acme Relay delivers webhooks.',
    product_name: 'Acme Relay',
    product_aliases: ['Relay'],
  };

  it('posts the product name and the aliases with the watch', async () => {
    fetchMock.mockResolvedValue(
      new Response('{"workflow_id":"watch:synthetic_founder:webhooks","message":"Started"}', { status: 200 }),
    );

    const result = await startWatch(REQUEST);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/watch/start');
    expect(JSON.parse(init?.body as string)).toEqual(REQUEST);
    expect(result.message).toBe('Started');
  });

  it('reports the API detail when the request is refused', async () => {
    fetchMock.mockResolvedValue(
      new Response('{"code":"invalid_request","detail":"Invalid request body"}', {
        status: 422,
        statusText: 'Unprocessable Entity',
      }),
    );

    await expect(startWatch({ ...REQUEST, product_name: '' })).rejects.toThrow(
      'Failed to start watch: Invalid request body',
    );
  });

  it('falls back to the status text when the error body is not JSON', async () => {
    fetchMock.mockResolvedValue(new Response('<html>', { status: 502, statusText: 'Bad Gateway' }));

    await expect(startWatch(REQUEST)).rejects.toThrow('Failed to start watch: Bad Gateway');
  });
});

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { approveReview, generateRequestId, listReviews, PostRemovedError } from './client';

// src/test/setup.ts replaces global fetch with a stub that throws; each test
// here gives it a canned Response. Nothing reaches the network.
const fetchMock = vi.mocked(fetch);

beforeEach(() => {
  fetchMock.mockReset();
});

describe('approveReview', () => {
  it('posts the URL, request id and post_anyway=false by default', async () => {
    fetchMock.mockResolvedValue(new Response('{"message":"ok"}', { status: 200 }));

    await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/reviews/review-wf-1/approve');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({
      posted_url: 'https://x.example/me/status/9',
      client_request_id: 'req_1',
      post_anyway: false,
    });
  });

  it('posts post_anyway=true when the founder overrides the edit gate', async () => {
    fetchMock.mockResolvedValue(new Response('{"message":"ok"}', { status: 200 }));

    await approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1', true);

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init?.body as string).post_anyway).toBe(true);
  });

  it('puts the status and the response body in the error message', async () => {
    // The card shows the "Post Anyway" prompt only when this message contains
    // "post_anyway", so the API must put the validator's text in the response.
    fetchMock.mockResolvedValue(
      new Response('{"detail":"Edited text has 1 failing sentence(s). Set post_anyway=true to approve anyway."}', {
        status: 500,
        statusText: 'Internal Server Error',
      }),
    );

    await expect(approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1')).rejects.toThrow(
      /Failed to approve review: Internal Server Error\..*post_anyway=true/,
    );
  });

  it('throws PostRemovedError on 409 (issue #9: the post was removed and the review skipped)', async () => {
    fetchMock.mockResolvedValue(
      new Response('{"detail":"Post removed"}', { status: 409, statusText: 'Conflict' }),
    );

    await expect(approveReview('review-wf-1', 'https://x.example/me/status/9', 'req_1')).rejects.toBeInstanceOf(
      PostRemovedError,
    );
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

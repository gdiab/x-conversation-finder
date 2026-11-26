import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ReviewCard } from './ReviewCard';
import { approveReview, editReview, nudgeReview, ReviewApiError, skipReview } from '../api/client';
import {
  makeCard,
  makeDraft,
  makeEvidence,
  makeReplyFit,
  makeReplyPlan,
} from '../test/fixtures';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return {
    ...actual,
    approveReview: vi.fn(),
    skipReview: vi.fn(),
    editReview: vi.fn(),
    nudgeReview: vi.fn(),
    confirmPostedReview: vi.fn(),
  };
});

const approveMock = vi.mocked(approveReview);
const skipMock = vi.mocked(skipReview);
const editMock = vi.mocked(editReview);
const nudgeMock = vi.mocked(nudgeReview);

// The workflow's validator text, as the API client puts it in its error.
// The card opens the override prompt on the code post_anyway_required
// (issue #15); api/client.test.ts builds this error from the API's JSON body,
// and backend/tests/test_review_api_errors.py checks the API sends it.
const POST_ANYWAY_DETAIL = 'Edited text has 1 failing sentence(s). Set post_anyway=true to approve anyway.';
const POST_ANYWAY_ERROR = `Failed to approve review: ${POST_ANYWAY_DETAIL}`;

function postAnywayError(detail = POST_ANYWAY_DETAIL, failing: string[] = ['Edited synthetic sentence.']) {
  return new ReviewApiError(`Failed to approve review: ${detail}`, 409, 'post_anyway_required', detail, failing);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ReviewCard: skipped card', () => {
  const skippedCard = () =>
    makeCard({
      status: 'skipped',
      skip_reason: 'Replies are limited on this post.',
      evidence: makeEvidence({ draft: makeDraft({ text: 'Rejected synthetic draft.' }) }),
    });

  it('shows the skip reason and the rejected draft as not for posting', () => {
    render(<ReviewCard card={skippedCard()} onUpdate={vi.fn()} />);

    expect(screen.getByText('Skipped:')).toBeInTheDocument();
    expect(screen.getByText(/Replies are limited on this post\./)).toBeInTheDocument();
    expect(screen.getByText(/Rejected draft \(not for posting\)/)).toBeInTheDocument();
    expect(screen.getByText('Rejected synthetic draft.')).toBeInTheDocument();
  });

  it('has no Copy button and no action buttons', () => {
    render(<ReviewCard card={skippedCard()} onUpdate={vi.fn()} />);

    expect(screen.queryByRole('button', { name: 'Copy Draft' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Draft Reply (Combined)' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Skip' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('falls back to the evidence skip reason, then to "No reason recorded"', () => {
    const fromEvidence = makeCard({
      status: 'skipped',
      skip_reason: null,
      evidence: makeEvidence({ draft: null, skip_reason: 'Evidence-level synthetic reason.' }),
    });
    const { unmount } = render(<ReviewCard card={fromEvidence} onUpdate={vi.fn()} />);
    expect(screen.getByText(/Evidence-level synthetic reason\./)).toBeInTheDocument();
    unmount();

    const none = makeCard({
      status: 'skipped',
      skip_reason: null,
      evidence: makeEvidence({ draft: null, skip_reason: null }),
    });
    render(<ReviewCard card={none} onUpdate={vi.fn()} />);
    expect(screen.getByText(/No reason recorded/)).toBeInTheDocument();
  });

  it('shows no rejected-draft line when the card has no draft', () => {
    const card = makeCard({
      status: 'skipped',
      skip_reason: 'No supporting founder material',
      evidence: makeEvidence({ draft: null }),
    });
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);
    expect(screen.queryByText(/Rejected draft/)).not.toBeInTheDocument();
  });
});

describe('ReviewCard: pending card', () => {
  it('shows the draft with a Copy button and the action buttons', () => {
    render(<ReviewCard card={makeCard()} onUpdate={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'Draft Reply (Combined)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy Draft' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Skip' })).toBeInTheDocument();
  });

  it('copies the draft text to the clipboard', async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
    render(
      <ReviewCard
        card={makeCard({ evidence: makeEvidence({ draft: makeDraft({ text: 'Copy me, synthetic.' }) }) })}
        onUpdate={vi.fn()}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Copy Draft' }));
    expect(writeText).toHaveBeenCalledWith('Copy me, synthetic.');
  });

  it('shows and copies a draft that ends with the product link (issue #41)', async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
    const text = 'Acme Relay retries failed deliveries. https://example.com/acme-relay';
    render(<ReviewCard card={makeCard({ evidence: makeEvidence({ draft: makeDraft({ text }) }) })} onUpdate={vi.fn()} />);

    expect(screen.getByText(text)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Copy Draft' }));
    expect(writeText).toHaveBeenCalledWith(text);
  });

  it('counts a link in the edit box as X does, and keeps it in the text to edit', async () => {
    const user = userEvent.setup();
    const url = 'https://example.com/a-rather-long-path/to-the-product-page-for-acme-relay';
    const body = 'a'.repeat(256);
    const text = `${body} ${url}`;
    render(<ReviewCard card={makeCard({ evidence: makeEvidence({ draft: makeDraft({ text }) }) })} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    const box = screen.getByDisplayValue(text);
    expect(screen.getByText(/^280\/280 characters, as X counts them/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Edit' })).toBeEnabled();

    await user.type(box, ' x'); // after the link: now a link in the text, counted as typed
    expect(screen.getByText(new RegExp(`^${text.length + 2}/280 characters`))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Edit' })).toBeDisabled();
  });

  it('shows the claim gate result and each failed sentence with its reason', () => {
    const draft = makeDraft({
      claim_gate: {
        ...makeDraft().claim_gate,
        passed: false,
        reasoning: 'One sentence failed.',
        failed_sentences: [{ sentence: 'It fixes everything.', reason: 'Starts with an unresolved referent' }],
      },
    });
    render(<ReviewCard card={makeCard({ evidence: makeEvidence({ draft }) })} onUpdate={vi.fn()} />);

    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText('"It fixes everything."')).toBeInTheDocument();
    expect(screen.getByText(/starts with an unresolved referent \(it\/this\/that/i)).toBeInTheDocument();
  });

  it('renders the reply-fit and reply-plan panels when the card has them', () => {
    const evidence = makeEvidence({
      reply_fit: makeReplyFit({ post_kind: 'question' }),
      reply_plan: makeReplyPlan({ reply_type: 'answer_question', target_point: 'how do I onboard faster' }),
    });
    render(<ReviewCard card={makeCard({ evidence })} onUpdate={vi.fn()} />);

    expect(screen.getByText('Should we reply?')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Reply Plan' })).toBeInTheDocument();
    expect(screen.getByText('Answer their question')).toBeInTheDocument();
  });

  it('renders a legacy card that has neither panel', () => {
    const evidence = makeEvidence();
    expect(evidence.reply_fit).toBeUndefined();
    expect(evidence.reply_plan).toBeUndefined();
    render(<ReviewCard card={makeCard({ evidence })} onUpdate={vi.fn()} />);

    expect(screen.queryByText('Should we reply?')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Reply Plan' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });
});

describe('ReviewCard: approve flow', () => {
  it('asks for the posted URL and makes no API call when it is empty', async () => {
    const user = userEvent.setup();
    render(<ReviewCard card={makeCard()} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(screen.getByText('Please enter the posted URL')).toBeInTheDocument();
    expect(approveMock).not.toHaveBeenCalled();
  });

  it('approves with the posted URL, without post_anyway, then refreshes the queue', async () => {
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    const card = makeCard();
    approveMock.mockResolvedValue();
    render(<ReviewCard card={card} onUpdate={onUpdate} />);

    await user.type(screen.getAllByPlaceholderText('Paste URL of your posted reply')[0], 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(approveMock).toHaveBeenCalledTimes(1);
    expect(approveMock).toHaveBeenCalledWith(
      card.workflow_id,
      'https://x.example/me/status/9',
      expect.stringMatching(/^req_/),
      false,
      // the draft the founder saw; the server checks it
      { text: card.evidence.draft?.text ?? '', gateState: card.edited_text_gate_state },
    );
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Edited text requires acknowledgement/)).not.toBeInTheDocument();
  });

  it('offers "Post Anyway" when the edit gate rejects the approve, and retries with post_anyway', async () => {
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    const card = makeCard({
      edited_text_gate_state: 'failed',
      edited_text_gate_results: [
        {
          sentence: 'Edited synthetic sentence.',
          passed: false,
          kind: 'claim',
          reason: 'No passage supports this sentence',
          kept_segment: false,
          phrase_id: null,
          source_passage_id: null,
          sentence_index: null,
        },
      ],
    });
    approveMock.mockRejectedValueOnce(postAnywayError()).mockResolvedValueOnce();
    render(<ReviewCard card={card} onUpdate={onUpdate} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    // First attempt failed: the server's message and the override prompt are shown.
    expect(screen.getByText(POST_ANYWAY_ERROR)).toBeInTheDocument();
    expect(screen.getByText(/Edited text requires acknowledgement/)).toBeInTheDocument();
    expect(screen.getByText(/The edited text contains claims that failed the claim gate\./)).toBeInTheDocument();
    expect(screen.getByText('Failing: "Edited synthetic sentence."')).toBeInTheDocument();
    expect(onUpdate).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Post Anyway (Override)' }));

    expect(approveMock).toHaveBeenCalledTimes(2);
    const [first, second] = approveMock.mock.calls;
    expect(first[3]).toBe(false);
    expect(second[3]).toBe(true);
    // The retry reuses the request id, so the server can deduplicate it.
    expect(second[2]).toBe(first[2]);
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Edited text requires acknowledgement/)).not.toBeInTheDocument();
    expect(screen.queryByText(POST_ANYWAY_ERROR)).not.toBeInTheDocument();
  });

  it('explains a gate check that ran out of retries (not_checked)', async () => {
    const user = userEvent.setup();
    const card = makeCard({ edited_text_gate_state: 'not_checked' });
    approveMock.mockRejectedValueOnce(
      postAnywayError('Edited text gate check failed (retries exhausted). Set post_anyway=true to approve anyway.', []),
    );
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(screen.getByText(/The gate check failed \(retries exhausted\)\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Post Anyway (Override)' })).toBeInTheDocument();
  });

  it('closes the override prompt on Cancel without calling the API again', async () => {
    const user = userEvent.setup();
    approveMock.mockRejectedValueOnce(postAnywayError());
    render(<ReviewCard card={makeCard({ edited_text_gate_state: 'failed' })} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    // Two "Cancel" buttons can exist; the override prompt's is the only one while not editing.
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByText(/Edited text requires acknowledgement/)).not.toBeInTheDocument();
    expect(approveMock).toHaveBeenCalledTimes(1);
  });

  it('shows other approve errors without the override prompt', async () => {
    const user = userEvent.setup();
    approveMock.mockRejectedValueOnce(new Error('Failed to approve review: Not Found. workflow gone'));
    render(<ReviewCard card={makeCard()} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(screen.getByText('Failed to approve review: Not Found. workflow gone')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
  });

  it('keeps showing the server error if the override attempt fails too', async () => {
    const user = userEvent.setup();
    approveMock
      .mockRejectedValueOnce(postAnywayError())
      .mockRejectedValueOnce(new Error('Failed to approve review: Conflict. already approved'));
    const onUpdate = vi.fn();
    render(<ReviewCard card={makeCard({ edited_text_gate_state: 'failed' })} onUpdate={onUpdate} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(screen.getByRole('button', { name: 'Post Anyway (Override)' }));

    expect(screen.getByText('Failed to approve review: Conflict. already approved')).toBeInTheDocument();
    expect(onUpdate).not.toHaveBeenCalled();
  });
});

describe('ReviewCard: error codes (issues #15, #20)', () => {
  it('does not open the override prompt for an error that only mentions post_anyway', async () => {
    // Before issue #15 the card matched the substring; now only the code counts
    const user = userEvent.setup();
    approveMock.mockRejectedValueOnce(new Error('Failed to approve review: something about post_anyway'));
    render(<ReviewCard card={makeCard({ edited_text_gate_state: 'failed' })} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
  });

  it('treats not_ready as "try again", not a failure, and retries with the same request id', async () => {
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    approveMock
      .mockRejectedValueOnce(
        new ReviewApiError('Failed to approve review: not ready', 409, 'not_ready', 'Cannot approve before the draft card is ready'),
      )
      .mockResolvedValueOnce();
    render(<ReviewCard card={makeCard()} onUpdate={onUpdate} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(screen.getByRole('status')).toHaveTextContent('This review is still being prepared. Try again in a moment.');
    expect(screen.queryByText(/Failed to approve review/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
    expect(onUpdate).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Approve' }));

    const [first, second] = approveMock.mock.calls;
    expect(second[2]).toBe(first[2]);
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('closes the override prompt when the draft changes, so it never acknowledges another draft', async () => {
    const user = userEvent.setup();
    const card = makeCard({ edited_text_gate_state: 'failed' });
    approveMock.mockRejectedValueOnce(postAnywayError());
    const { rerender } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    expect(screen.getByRole('button', { name: 'Post Anyway (Override)' })).toBeInTheDocument();

    // The queue reloads with an edited draft (another tab, or this card's edit)
    const edited = makeCard({ edited_text_gate_state: 'failed' });
    edited.evidence = makeEvidence({ draft: makeDraft({ text: 'A different synthetic draft.' }) });
    rerender(<ReviewCard card={edited} onUpdate={vi.fn()} />);

    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
    expect(screen.queryByText('Failing: "Edited synthetic sentence."')).not.toBeInTheDocument();
  });

  it('ignores a post_anyway_required that arrives after the draft changed', async () => {
    const user = userEvent.setup();
    let reject: (err: unknown) => void = () => {};
    approveMock.mockReturnValueOnce(new Promise((_, r) => { reject = r; }));
    const card = makeCard({ edited_text_gate_state: 'failed' });
    const { rerender } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    const edited = makeCard({ edited_text_gate_state: 'failed' });
    edited.evidence = makeEvidence({ draft: makeDraft({ text: 'A different synthetic draft.' }) });
    rerender(<ReviewCard card={edited} onUpdate={vi.fn()} />);
    await act(async () => reject(postAnywayError()));

    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('The draft changed while approving. Review it and approve again.');
  });

  it('keeps Approve disabled after an edit until the reloaded card is shown', async () => {
    const user = userEvent.setup();
    let finishReload: () => void = () => {};
    const onUpdate = vi.fn(() => new Promise<void>((resolve) => { finishReload = resolve; }));
    editMock.mockResolvedValue('Edited synthetic text.');
    render(<ReviewCard card={makeCard()} onUpdate={onUpdate} />);

    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    await user.click(screen.getByRole('button', { name: 'Save Edit' }));

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    await act(async () => finishReload());
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
  });

  it('closes the override prompt after a successful edit', async () => {
    const user = userEvent.setup();
    approveMock.mockRejectedValueOnce(postAnywayError());
    editMock.mockResolvedValue('Edited synthetic text.');
    render(<ReviewCard card={makeCard({ edited_text_gate_state: 'failed' })} onUpdate={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    await user.click(screen.getByRole('button', { name: 'Save Edit' }));

    expect(editMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
  });

  it('reloads and asks for another look when the server says the draft changed', async () => {
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    const detail = 'The draft changed since it was shown. Review the current draft and approve again.';
    approveMock.mockRejectedValueOnce(new ReviewApiError(`Failed to approve review: ${detail}`, 409, 'draft_changed', detail));
    render(<ReviewCard card={makeCard()} onUpdate={onUpdate} />);

    await user.type(screen.getByPlaceholderText('Paste URL of your posted reply'), 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent(detail);
    expect(screen.queryByText(/Failed to approve review/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Post Anyway (Override)' })).not.toBeInTheDocument();
  });

  it('shows a busy nudge as a notice with the server message', async () => {
    const user = userEvent.setup();
    const detail = 'Cannot nudge while another draft change is in progress. Retry after it completes.';
    nudgeMock.mockRejectedValueOnce(new ReviewApiError(`Failed to nudge review: ${detail}`, 409, 'busy', detail));
    render(<ReviewCard card={makeCard()} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Nudge (Redraft)' }));
    await user.type(screen.getByPlaceholderText(/Make it shorter/), 'Shorter');
    await user.click(screen.getByRole('button', { name: 'Apply Nudge' }));

    expect(screen.getByRole('status')).toHaveTextContent(detail);
    expect(screen.queryByText(/Failed to nudge review/)).not.toBeInTheDocument();
  });

  it('shows a rejected nudge as an error', async () => {
    const user = userEvent.setup();
    const detail = 'Nudged draft rejected; the current draft is unchanged: synthetic reason';
    nudgeMock.mockRejectedValueOnce(new ReviewApiError(`Failed to nudge review: ${detail}`, 422, 'rejected', detail));
    render(<ReviewCard card={makeCard()} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Nudge (Redraft)' }));
    await user.type(screen.getByPlaceholderText(/Make it shorter/), 'Shorter');
    await user.click(screen.getByRole('button', { name: 'Apply Nudge' }));

    expect(screen.getByText(`Failed to nudge review: ${detail}`)).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
  it('shows no nudge rejection (it quotes the redraft) on a removed card (issue #23)', async () => {
    const user = userEvent.setup();
    const detail = 'Nudged draft rejected; the current draft is unchanged: synthetic quoted sentence';
    nudgeMock.mockRejectedValueOnce(new ReviewApiError(`Failed to nudge review: ${detail}`, 422, 'rejected', detail));
    const card = makeCard();
    const { rerender } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Nudge (Redraft)' }));
    await user.type(screen.getByPlaceholderText(/Make it shorter/), 'Shorter');
    await user.click(screen.getByRole('button', { name: 'Apply Nudge' }));
    expect(screen.getByText(`Failed to nudge review: ${detail}`)).toBeInTheDocument();

    rerender(<ReviewCard card={{ ...card, post: { ...card.post, text: '[removed - post deleted]', text_removed: true } }} onUpdate={vi.fn()} />);
    expect(screen.queryByText(/synthetic quoted sentence/)).not.toBeInTheDocument();
  });

  it('shows no nudge rejection that arrives after the card was removed (issue #23)', async () => {
    const user = userEvent.setup();
    const detail = 'Nudged draft rejected; the current draft is unchanged: synthetic late sentence';
    let reject: (e: Error) => void = () => {};
    nudgeMock.mockReturnValueOnce(new Promise((_, r) => { reject = r; }));
    const card = makeCard();
    const { rerender } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Nudge (Redraft)' }));
    await user.type(screen.getByPlaceholderText(/Make it shorter/), 'Shorter');
    await user.click(screen.getByRole('button', { name: 'Apply Nudge' }));
    rerender(<ReviewCard card={{ ...card, post: { ...card.post, text: '[removed - post deleted]', text_removed: true } }} onUpdate={vi.fn()} />);
    await act(async () => reject(new ReviewApiError(`Failed to nudge review: ${detail}`, 422, 'rejected', detail)));

    expect(screen.queryByText(/synthetic late sentence/)).not.toBeInTheDocument();
  });
});

describe('ReviewCard: edit gate results', () => {
  const failedEditCard = () =>
    makeCard({
      edited_text_gate_state: 'failed',
      edited_text_gate_results: [
        {
          sentence: 'Kept synthetic sentence.',
          passed: true,
          kind: 'non_claim',
          reason: '',
          kept_segment: true,
          phrase_id: 'ack_01',
          source_passage_id: null,
          sentence_index: null,
        },
        {
          sentence: 'Unsupported synthetic sentence.',
          passed: false,
          kind: 'claim',
          reason: 'No passage supports this sentence',
          kept_segment: false,
          phrase_id: null,
          source_passage_id: null,
          sentence_index: null,
        },
      ],
    });

  it('shows the gate state, per-sentence results and the post-anyway requirement', () => {
    render(<ReviewCard card={failedEditCard()} onUpdate={vi.fn()} />);

    expect(screen.getByText('Gate State: FAILED')).toBeInTheDocument();
    expect(screen.getByText(/Approval requires "Post anyway" acknowledgement/)).toBeInTheDocument();
    expect(screen.getByText('PASS')).toBeInTheDocument();
    expect(screen.getByText('FAIL')).toBeInTheDocument();
    expect(screen.getByText('verbatim')).toBeInTheDocument();
    expect(screen.getByText('"Unsupported synthetic sentence."')).toBeInTheDocument();
    expect(screen.getByText('No passage supports this sentence')).toBeInTheDocument();
  });

  it('shows no post-anyway requirement when the edit passed', () => {
    const card = failedEditCard();
    card.edited_text_gate_state = 'passed';
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByText('Gate State: PASSED')).toBeInTheDocument();
    expect(screen.queryByText(/Approval requires "Post anyway"/)).not.toBeInTheDocument();
  });

  it('records a posted-anyway override on an approved card', () => {
    const card = failedEditCard();
    card.status = 'approved';
    card.post_anyway = true;
    card.post_anyway_failing_sentences = ['Unsupported synthetic sentence.'];
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByText(/Posted Anyway Override/)).toBeInTheDocument();
    expect(screen.getByText('Failing sentences at approval: 1')).toBeInTheDocument();
  });
});

describe('ReviewCard: edit and skip actions', () => {
  it('saves an edited draft through the API and leaves edit mode', async () => {
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    const card = makeCard();
    editMock.mockResolvedValue('Edited synthetic text.');
    render(<ReviewCard card={card} onUpdate={onUpdate} />);

    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    const box = screen.getByDisplayValue(card.evidence.draft!.text);
    await user.clear(box);
    await user.type(box, 'Edited synthetic text.');
    await user.click(screen.getByRole('button', { name: 'Save Edit' }));

    expect(editMock).toHaveBeenCalledWith(card.workflow_id, 'Edited synthetic text.', expect.stringMatching(/^req_/));
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Save Edit' })).not.toBeInTheDocument();
  });

  it('refuses to save an empty edit', async () => {
    const user = userEvent.setup();
    const card = makeCard();
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    await user.clear(screen.getByDisplayValue(card.evidence.draft!.text));
    await user.click(screen.getByRole('button', { name: 'Save Edit' }));

    expect(screen.getByText('Draft cannot be empty')).toBeInTheDocument();
    expect(editMock).not.toHaveBeenCalled();
  });

  it('skips with the typed reason, or "Not relevant" when none is given', async () => {
    const user = userEvent.setup();
    skipMock.mockResolvedValue();
    const card = makeCard();
    const { unmount } = render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Skip' }));
    expect(skipMock).toHaveBeenLastCalledWith(card.workflow_id, 'Not relevant', expect.stringMatching(/^req_/));
    unmount();

    render(<ReviewCard card={card} onUpdate={vi.fn()} />);
    await user.type(screen.getByPlaceholderText('Skip reason (optional)'), 'off topic');
    await user.click(screen.getByRole('button', { name: 'Skip' }));
    expect(skipMock).toHaveBeenLastCalledWith(card.workflow_id, 'off topic', expect.stringMatching(/^req_/));
  });
});

describe('ReviewCard: post removed (issue #9)', () => {
  it('shows "Post removed" instead of the post text on a removed card', () => {
    const card = makeCard({
      status: 'skipped',
      skip_reason: 'Post removed: the target post was deleted or is no longer visible on X',
    });
    card.post = { ...card.post, text: '[removed - post deleted]', text_removed: true };
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByText(/Post removed: it was deleted or is no longer visible on X/)).toBeInTheDocument();
    expect(screen.queryByText('[removed - post deleted]')).not.toBeInTheDocument();
  });

  it('renders a removed card without its author or post link (issue #35)', () => {
    const card = makeCard({
      status: 'skipped',
      skip_reason: 'Post removed: the target post was deleted or is no longer visible on X',
    });
    const { author: _author, url: _url, ...kept } = card.post;
    card.post = { ...kept, text: '[removed - post deleted]', text_removed: true };
    card.evidence.draft!.text = '[removed - post deleted]';
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'Review' })).toBeInTheDocument();
    expect(screen.getByText(/Post removed: it was deleted or is no longer visible on X/)).toBeInTheDocument();
    expect(screen.getByText(/likes · \d+ replies$/)).toBeInTheDocument();
    expect(screen.queryByText(/followers/)).not.toBeInTheDocument();
    expect(screen.queryByText(/@/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /View on X/ })).not.toBeInTheDocument();
  });

  it('hides a stale author and post link once the card records the removal (issue #35)', () => {
    const card = makeCard({ status: 'skipped' });
    const { username, name } = card.post.author!;
    card.post = { ...card.post, text: '[removed - post deleted]', text_removed: true };
    render(<ReviewCard card={card} onUpdate={vi.fn()} />);

    expect(screen.queryByText(new RegExp(username))).not.toBeInTheDocument();
    expect(screen.queryByText(new RegExp(name))).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /View on X/ })).not.toBeInTheDocument();
  });

  it('reloads the queue without an error when approve finds the post removed', async () => {
    const { PostRemovedError } = await import('../api/client');
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    approveMock.mockRejectedValueOnce(new PostRemovedError('removed'));
    render(<ReviewCard card={makeCard()} onUpdate={onUpdate} />);

    await user.type(screen.getAllByPlaceholderText('Paste URL of your posted reply')[0], 'https://x.example/me/status/9');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Post removed: the post was deleted/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Edited text requires acknowledgement/)).not.toBeInTheDocument();
  });
  it('reloads the card when an edit is refused because the post was removed (issue #23)', async () => {
    const { PostRemovedError } = await import('../api/client');
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    editMock.mockRejectedValueOnce(new PostRemovedError('removed'));
    render(<ReviewCard card={makeCard()} onUpdate={onUpdate} />);

    await user.click(screen.getByRole('button', { name: 'Edit Draft' }));
    await user.click(screen.getByRole('button', { name: 'Save Edit' }));

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Save Edit' })).not.toBeInTheDocument();
  });
});

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ADDED_HEADING, appendToBrief } from './BriefSuggestions';
import { StartWatchForm } from './StartWatchForm';
import {
  BriefSuggestionError,
  getBriefSuggestionCost,
  startWatch,
  suggestBriefAdditions,
  type BriefSuggestionsResult,
} from '../api/client';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, startWatch: vi.fn(), getBriefSuggestionCost: vi.fn(), suggestBriefAdditions: vi.fn() };
});

const costMock = vi.mocked(getBriefSuggestionCost);
const suggestMock = vi.mocked(suggestBriefAdditions);

const RESULT: BriefSuggestionsResult = {
  source: 'github_readme',
  page_truncated: false,
  suggestions: [
    { sentence: 'Acme Relay runs on macOS and Linux.', quote: 'Runs on macOS and Linux x86-64.', why: 'Answers which-platform posts.' },
    { sentence: 'Acme Relay needs no API key.', quote: 'no API key is needed', why: 'Answers setup posts.' },
    { sentence: 'Acme Relay has no required UI.', quote: 'No UI is required.', why: 'Answers headless posts.' },
  ],
  flags: [
    { brief_sentence_id: 'b2', brief_sentence: 'Relay retries five times.', kind: 'contradicted', quote: 'retries up to three times', note: 'The page says three.' },
  ],
  dropped_quote_not_on_page: 1,
  dropped_other: 0,
  cost_usd: 0.0021,
  model: 'fake',
};

const BRIEF = 'Acme Relay delivers webhooks.\nRelay retries five times.';

beforeEach(() => {
  vi.clearAllMocks();
  costMock.mockResolvedValue({
    max_cost_usd: 0.0134, max_input_tokens: 83472, max_output_tokens: 1500, model: 'fake', max_page_bytes: 60000, max_suggestions: 8,
  });
});

async function setUp(url = 'https://github.com/synthetic-org/acme-relay') {
  const user = userEvent.setup();
  render(<StartWatchForm onSuccess={vi.fn()} />);
  await user.type(screen.getByLabelText('Product name:'), 'Acme Relay');
  await user.type(screen.getByLabelText('Product Brief:'), BRIEF);
  if (url) await user.type(screen.getByLabelText('Product URL:'), url);
  return user;
}

const brief = () => (screen.getByLabelText('Product Brief:') as HTMLTextAreaElement).value;
const button = () => screen.getByRole('button', { name: 'Suggest brief additions' });

describe('Suggest brief additions (issue #42)', () => {
  it('is off until a valid Product URL is entered, and shows the cost before the call', async () => {
    const user = await setUp('');

    expect(await screen.findByText(/one model call, at most \$0\.0134/)).toBeInTheDocument();
    expect(button()).toBeDisabled();
    await user.type(screen.getByLabelText('Product URL:'), 'http://example.com');
    expect(button()).toBeDisabled();
    await user.clear(screen.getByLabelText('Product URL:'));
    await user.type(screen.getByLabelText('Product URL:'), 'https://example.com');
    expect(button()).toBeEnabled();
    expect(suggestMock).not.toHaveBeenCalled();
  });

  it('stays off when the cost cannot be loaded', async () => {
    costMock.mockRejectedValue(new Error('down'));
    await setUp();

    expect(await screen.findByText(/cost of a suggestion call could not be loaded/)).toBeInTheDocument();
    expect(button()).toBeDisabled();
  });

  it('shows a checklist and changes nothing in the brief until "Add checked to brief"', async () => {
    suggestMock.mockResolvedValue(RESULT);
    const user = await setUp();
    await screen.findByText(/at most \$0\.0134/);

    await user.click(button());

    expect(suggestMock).toHaveBeenCalledWith({
      product_url: 'https://github.com/synthetic-org/acme-relay',
      product_brief: BRIEF,
      product_name: 'Acme Relay',
    });
    const list = await screen.findByRole('list', { name: 'Suggested brief additions' });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(3);
    // Each item: an unticked checkbox, the editable sentence, its quote and why
    expect(within(items[0]).getByRole('checkbox')).not.toBeChecked();
    expect(within(items[0]).getByLabelText('Suggested sentence 1')).toHaveValue('Acme Relay runs on macOS and Linux.');
    expect(within(items[0]).getByText(/From the page: “Runs on macOS and Linux x86-64\.”/)).toBeInTheDocument();
    expect(within(items[0]).getByText('Why: Answers which-platform posts.')).toBeInTheDocument();
    expect(screen.getByText(/1 suggestion was dropped because the quote is not on the page/)).toBeInTheDocument();
    // The flag is information
    const flags = screen.getByRole('list', { name: 'Brief sentences the page does not support' });
    expect(within(flags).getByText(/the page contradicts it/)).toBeInTheDocument();

    // Nothing ticked: nothing to add
    expect(screen.getByRole('button', { name: 'Add checked to brief' })).toBeDisabled();
    expect(brief()).toBe(BRIEF);

    // Tick 1 and 3, edit 3; ticking and editing alone change nothing
    await user.click(within(items[0]).getByRole('checkbox'));
    await user.click(within(items[2]).getByRole('checkbox'));
    const third = within(items[2]).getByLabelText('Suggested sentence 3');
    await user.clear(third);
    await user.type(third, 'Acme Relay runs without a UI.');
    expect(brief()).toBe(BRIEF);

    await user.click(screen.getByRole('button', { name: 'Add checked to brief' }));

    expect(brief()).toBe(
      `${BRIEF}\n\n${ADDED_HEADING}\n\n- Acme Relay runs on macOS and Linux.\n- Acme Relay runs without a UI.`,
    );
    // The unticked suggestion stays in the list and was not added
    const left = within(screen.getByRole('list', { name: 'Suggested brief additions' })).getAllByRole('listitem');
    expect(left).toHaveLength(1);
    expect(within(left[0]).getByLabelText('Suggested sentence 1')).toHaveValue('Acme Relay needs no API key.');
    expect(brief()).not.toContain('no API key');
    expect(screen.getByRole('status')).toHaveTextContent('Added 2 sentences to the brief');
  });

  it('starts the watch with the brief as the founder left it', async () => {
    suggestMock.mockResolvedValue(RESULT);
    vi.mocked(startWatch).mockResolvedValue({ workflow_id: 'w', message: 'ok' });
    const user = await setUp();
    await user.type(screen.getByPlaceholderText('e.g., john_doe'), 'synthetic_founder');
    await user.type(screen.getByPlaceholderText('e.g., reliability'), 'webhooks');
    await user.type(screen.getByPlaceholderText('e.g., distributed systems reliability'), 'webhook retries');
    await screen.findByText(/at most \$0\.0134/);
    await user.click(button());
    await screen.findByRole('list', { name: 'Suggested brief additions' });

    // Suggestions shown but none added: the watch gets the original brief
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(vi.mocked(startWatch).mock.calls[0][0].product_brief).toBe(BRIEF);
  });

  it('shows a fetch error and leaves the form usable', async () => {
    suggestMock.mockRejectedValueOnce(new BriefSuggestionError('GitHub has no README for this repository.', 'page_not_found'));
    const user = await setUp();
    await screen.findByText(/at most \$0\.0134/);

    await user.click(button());

    expect(await screen.findByRole('alert')).toHaveTextContent('No suggestions: GitHub has no README for this repository.');
    expect(brief()).toBe(BRIEF);
    expect(screen.getByLabelText('Product Brief:')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Start Watch' })).toBeEnabled();
    // The button can be clicked again
    suggestMock.mockResolvedValueOnce(RESULT);
    await user.click(button());
    expect(await screen.findByRole('list', { name: 'Suggested brief additions' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('clears the suggestions when the Product URL changes', async () => {
    suggestMock.mockResolvedValue(RESULT);
    const user = await setUp();
    await screen.findByText(/at most \$0\.0134/);
    await user.click(button());
    await screen.findByRole('list', { name: 'Suggested brief additions' });

    await user.type(screen.getByLabelText('Product URL:'), '-two');

    expect(screen.queryByRole('list', { name: 'Suggested brief additions' })).not.toBeInTheDocument();
  });
});

describe('appendToBrief', () => {
  it('adds list items under a heading that ends any earlier heading or lead-in', () => {
    expect(appendToBrief('', ['Acme runs on Linux.'])).toBe(`${ADDED_HEADING}\n\n- Acme runs on Linux.`);
    expect(appendToBrief('## Not supported yet\n\nSlack.\n\n', ['Acme  runs\non Linux. '])).toBe(
      `## Not supported yet\n\nSlack.\n\n${ADDED_HEADING}\n\n- Acme runs on Linux.`,
    );
    expect(appendToBrief('Not offered yet:', ['Acme runs on Linux.'])).toBe(
      `Not offered yet:\n\n${ADDED_HEADING}\n\n- Acme runs on Linux.`,
    );
  });

  it('continues the list when the brief already ends under that heading, and ignores blanks', () => {
    const once = appendToBrief('Acme works.', ['One fact.']);
    expect(appendToBrief(once, ['Two fact.', '  '])).toBe(`Acme works.\n\n${ADDED_HEADING}\n\n- One fact.\n- Two fact.`);
    expect(appendToBrief(once, [' ', ''])).toBe(once);
  });
});

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { parseCommaList, productUrlError, StartWatchForm } from './StartWatchForm';
import { getBriefSuggestionCost, startWatch } from '../api/client';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return {
    ...actual,
    startWatch: vi.fn(),
    // issue #42: the brief-suggestion box loads its cost on mount
    getBriefSuggestionCost: vi.fn(),
    suggestBriefAdditions: vi.fn(),
  };
});

const startWatchMock = vi.mocked(startWatch);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getBriefSuggestionCost).mockResolvedValue({
    max_cost_usd: 0.0134, max_input_tokens: 83472, max_output_tokens: 1500, model: 'fake', max_page_bytes: 60000, max_suggestions: 8,
  });
});

async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByPlaceholderText('e.g., john_doe'), 'synthetic_founder');
  await user.type(screen.getByPlaceholderText('e.g., reliability'), 'webhooks');
  await user.type(screen.getByPlaceholderText('e.g., distributed systems reliability'), 'webhook retries');
  await user.type(screen.getByPlaceholderText(/Describe your product/), 'Acme Relay delivers webhooks.');
  await user.type(screen.getByLabelText('Product URL:'), ' https://example.com/acme-relay ');
}

describe('StartWatchForm: product name, "Also called" and "Key features" (issue #12)', () => {
  it('has a required Product name field and optional Also called and Key features fields', () => {
    render(<StartWatchForm onSuccess={vi.fn()} />);

    expect(screen.getByLabelText('Product name:')).toBeRequired();
    expect(screen.getByLabelText('Also called (optional):')).not.toBeRequired();
    expect(screen.getByLabelText('Key features (optional):')).not.toBeRequired();
    expect(screen.getByText(/uses the product name or one of these names is checked against your brief/)).toBeInTheDocument();
    expect(screen.getByText(/Key features are read only from this field, not from the brief/)).toBeInTheDocument();
  });

  it('puts Key features next to Product name and Also called, before the brief', () => {
    render(<StartWatchForm onSuccess={vi.fn()} />);

    const labels = screen.getAllByText(/:$/, { selector: 'label' }).map((label) => label.textContent?.trim());
    expect(labels.slice(-5)).toEqual([
      'Product name:', 'Product URL:', 'Also called (optional):', 'Key features (optional):', 'Product Brief:',
    ]);
  });

  it('sends the product name and the aliases as a list', async () => {
    const user = userEvent.setup();
    const onSuccess = vi.fn();
    startWatchMock.mockResolvedValue({ workflow_id: 'watch:x:y', message: "Started watch for topic 'webhooks'" });
    render(<StartWatchForm onSuccess={onSuccess} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), '  Acme Relay ');
    await user.type(screen.getByLabelText('Also called (optional):'), 'Relay, , AcmeRelay,relay');
    await user.type(screen.getByLabelText('Key features (optional):'), ' Signed   deliveries, ,Delivery replay,signed deliveries');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(startWatchMock).toHaveBeenCalledWith({
      account: 'synthetic_founder',
      topic: 'webhooks',
      query: 'webhook retries',
      product_brief: 'Acme Relay delivers webhooks.',
      product_name: 'Acme Relay',
      product_aliases: ['Relay', 'AcmeRelay'],
      key_features: ['Signed deliveries', 'Delivery replay'],
      product_url: 'https://example.com/acme-relay',
    });
    expect(await screen.findByText("Started watch for topic 'webhooks'")).toBeInTheDocument();
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it('sends empty lists when Also called and Key features are left empty', async () => {
    const user = userEvent.setup();
    startWatchMock.mockResolvedValue({ workflow_id: 'watch:x:y', message: 'ok' });
    render(<StartWatchForm onSuccess={vi.fn()} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), 'Acme Relay');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(startWatchMock.mock.calls[0][0].product_aliases).toEqual([]);
    expect(startWatchMock.mock.calls[0][0].key_features).toEqual([]);
  });

  it('refuses a product name made of spaces without calling the API', async () => {
    const user = userEvent.setup();
    render(<StartWatchForm onSuccess={vi.fn()} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), '   ');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(screen.getByText('Enter the product name.')).toBeInTheDocument();
    expect(startWatchMock).not.toHaveBeenCalled();
  });

  it('shows the server message when the watch already runs', async () => {
    const user = userEvent.setup();
    startWatchMock.mockResolvedValue({
      workflow_id: 'watch:x:y',
      message: "Watch already running for topic 'webhooks'. Its product name, other names, key features and brief were not changed; stop the watch first to change them.",
    });
    render(<StartWatchForm onSuccess={vi.fn()} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), 'Acme Relay');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(await screen.findByText(/stop the watch first to change them/)).toBeInTheDocument();
  });
});

describe('StartWatchForm: Product URL (issue #41)', () => {
  it('has a required https Product URL field that explains the link', () => {
    render(<StartWatchForm onSuccess={vi.fn()} />);

    expect(screen.getByLabelText('Product URL:')).toBeRequired();
    expect(screen.getByLabelText('Product URL:')).toHaveAttribute('type', 'url');
    expect(screen.getByText(/A draft that names the product ends with this link/)).toBeInTheDocument();
  });

  it.each(['http://example.com', 'example.com', 'https://localhost', 'https://exa mple.com'])(
    'refuses %s without calling the API',
    async (url) => {
      const user = userEvent.setup();
      const { container } = render(<StartWatchForm onSuccess={vi.fn()} />);

      await fillRequired(user);
      await user.type(screen.getByLabelText('Product name:'), 'Acme Relay');
      await user.clear(screen.getByLabelText('Product URL:'));
      await user.type(screen.getByLabelText('Product URL:'), url);
      // Submit the form itself: the browser's own type="url" check would
      // stop some of these first, and the form must refuse them either way
      container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      expect(await screen.findByText(/Enter the product URL as one https:\/\/ link/)).toBeInTheDocument();
      expect(startWatchMock).not.toHaveBeenCalled();
    },
  );
});

describe('productUrlError', () => {
  it('accepts an https link to a site and nothing else', () => {
    expect(productUrlError('https://github.com/clickety-clacks/tightbeam')).toBeNull();
    expect(productUrlError('  https://example.com  ')).toBeNull();
    expect(productUrlError('')).toBe('Enter the product URL.');
    expect(productUrlError('ftp://example.com')).not.toBeNull();
    expect(productUrlError(`https://example.com/${'a'.repeat(2048)}`)).toMatch(/at most 2048 characters/);
  });
});

describe('parseCommaList', () => {
  it('splits on commas, trims, and drops blanks and repeats', () => {
    expect(parseCommaList('')).toEqual([]);
    expect(parseCommaList(' , ,')).toEqual([]);
    expect(parseCommaList('Relay')).toEqual(['Relay']);
    expect(parseCommaList(' Acme   Relay ,Relay,  relay , AR')).toEqual(['Acme Relay', 'Relay', 'AR']);
  });
});

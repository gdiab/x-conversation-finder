import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { parseAlsoCalled, StartWatchForm } from './StartWatchForm';
import { startWatch } from '../api/client';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, startWatch: vi.fn() };
});

const startWatchMock = vi.mocked(startWatch);

beforeEach(() => {
  vi.clearAllMocks();
});

async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByPlaceholderText('e.g., john_doe'), 'synthetic_founder');
  await user.type(screen.getByPlaceholderText('e.g., reliability'), 'webhooks');
  await user.type(screen.getByPlaceholderText('e.g., distributed systems reliability'), 'webhook retries');
  await user.type(screen.getByPlaceholderText(/Describe your product/), 'Acme Relay delivers webhooks.');
}

describe('StartWatchForm: product name and "Also called" (issue #12)', () => {
  it('has a required Product name field and an optional Also called field', () => {
    render(<StartWatchForm onSuccess={vi.fn()} />);

    expect(screen.getByLabelText('Product name:')).toBeRequired();
    expect(screen.getByLabelText('Also called (optional):')).not.toBeRequired();
    expect(screen.getByText(/is checked against your brief/)).toBeInTheDocument();
  });

  it('sends the product name and the aliases as a list', async () => {
    const user = userEvent.setup();
    const onSuccess = vi.fn();
    startWatchMock.mockResolvedValue({ workflow_id: 'watch:x:y', message: "Started watch for topic 'webhooks'" });
    render(<StartWatchForm onSuccess={onSuccess} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), '  Acme Relay ');
    await user.type(screen.getByLabelText('Also called (optional):'), 'Relay, , AcmeRelay,relay');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(startWatchMock).toHaveBeenCalledWith({
      account: 'synthetic_founder',
      topic: 'webhooks',
      query: 'webhook retries',
      product_brief: 'Acme Relay delivers webhooks.',
      product_name: 'Acme Relay',
      product_aliases: ['Relay', 'AcmeRelay'],
    });
    expect(await screen.findByText("Started watch for topic 'webhooks'")).toBeInTheDocument();
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it('sends an empty alias list when Also called is left empty', async () => {
    const user = userEvent.setup();
    startWatchMock.mockResolvedValue({ workflow_id: 'watch:x:y', message: 'ok' });
    render(<StartWatchForm onSuccess={vi.fn()} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), 'Acme Relay');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(startWatchMock.mock.calls[0][0].product_aliases).toEqual([]);
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
      message: "Watch already running for topic 'webhooks'. Its product name and brief were not changed; stop the watch first to change them.",
    });
    render(<StartWatchForm onSuccess={vi.fn()} />);

    await fillRequired(user);
    await user.type(screen.getByLabelText('Product name:'), 'Acme Relay');
    await user.click(screen.getByRole('button', { name: 'Start Watch' }));

    expect(await screen.findByText(/stop the watch first to change them/)).toBeInTheDocument();
  });
});

describe('parseAlsoCalled', () => {
  it('splits on commas, trims, and drops blanks and repeats', () => {
    expect(parseAlsoCalled('')).toEqual([]);
    expect(parseAlsoCalled(' , ,')).toEqual([]);
    expect(parseAlsoCalled('Relay')).toEqual(['Relay']);
    expect(parseAlsoCalled(' Acme   Relay ,Relay,  relay , AR')).toEqual(['Acme Relay', 'Relay', 'AR']);
  });
});

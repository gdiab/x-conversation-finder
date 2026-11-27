import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { parseHandles, SkippedAccounts } from './SkippedAccounts';
import { getSkippedAccounts, saveSkippedAccounts } from '../api/client';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, getSkippedAccounts: vi.fn(), saveSkippedAccounts: vi.fn() };
});

const getMock = vi.mocked(getSkippedAccounts);
const saveMock = vi.mocked(saveSkippedAccounts);

beforeEach(() => {
  vi.clearAllMocks();
  // The API stores handles lowercase, without "@"
  saveMock.mockImplementation(async (handles) => [...new Set(handles.map((h) => h.replace(/^@/, '').toLowerCase()))]);
});

describe('parseHandles', () => {
  it('splits at commas and spaces and drops blanks', () => {
    expect(parseHandles(' @grok, openai  someone ,')).toEqual(['@grok', 'openai', 'someone']);
  });
});

describe('SkippedAccounts (issue #48)', () => {
  it('starts empty and says what the list does', async () => {
    getMock.mockResolvedValue([]);
    render(<SkippedAccounts />);

    expect(await screen.findByText('No accounts are skipped.')).toBeInTheDocument();
    expect(screen.getByText(/Posts that mention them are still\s+reviewed/)).toBeInTheDocument();
    expect(screen.getByText(/applies at each watch's next search/)).toBeInTheDocument();
  });

  it('adds a handle by saving the whole list and shows what was stored', async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue(['spam_bot']);
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), '@Grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(saveMock).toHaveBeenCalledWith(['spam_bot', '@Grok']);
    const list = await screen.findByRole('list', { name: 'Skipped accounts' });
    expect(within(list).getByText(/@grok/)).toBeInTheDocument();
    expect(screen.getByLabelText('Add account:')).toHaveValue('');
  });

  it('removes a handle', async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue(['grok', 'spam_bot']);
    render(<SkippedAccounts />);

    await user.click(await screen.findByRole('button', { name: 'Remove @grok' }));

    expect(saveMock).toHaveBeenCalledWith(['spam_bot']);
    expect(await screen.findByRole('button', { name: 'Remove @spam_bot' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove @grok' })).not.toBeInTheDocument();
  });

  it('shows a refused save, keeps the list and keeps what was typed', async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue(['grok']);
    saveMock.mockRejectedValue(new Error('Failed to save skipped accounts: handles: "a b" is not an X handle'));
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), 'x.com/someone');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('is not an X handle');
    expect(screen.getByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();
    expect(screen.getByLabelText('Add account:')).toHaveValue('x.com/someone');
  });

  it('offers no editing until the stored list loads, so a save cannot replace an unknown list', async () => {
    const user = userEvent.setup();
    getMock.mockRejectedValueOnce(new Error('Failed to load skipped accounts: Internal Server Error'));
    getMock.mockResolvedValueOnce(['grok']);
    render(<SkippedAccounts />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load skipped accounts');
    expect(screen.queryByLabelText('Add account:')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();
    expect(saveMock).not.toHaveBeenCalled();
  });

  it('ignores a load answered after a save, so the next save keeps the saved handle', async () => {
    const user = userEvent.setup();
    // Strict Mode mounts twice: the second load is answered, the first only after a save
    let answerLateLoad: (handles: string[]) => void = () => {};
    getMock.mockImplementationOnce(() => new Promise((resolve) => { answerLateLoad = resolve; }));
    getMock.mockResolvedValueOnce([]);
    render(<SkippedAccounts />, { reactStrictMode: true });

    await user.type(await screen.findByLabelText('Add account:'), 'grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();

    await act(async () => answerLateLoad([]));
    expect(screen.getByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();

    await user.type(screen.getByLabelText('Add account:'), 'openai');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(saveMock).toHaveBeenLastCalledWith(['grok', 'openai']);
  });

  it('accepts no typing while a save is pending', async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue([]);
    let finishSave: (handles: string[]) => void = () => {};
    saveMock.mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve; }));
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), 'grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(screen.getByLabelText('Add account:')).toBeDisabled();
    await act(async () => finishSave(['grok']));
    expect(screen.getByLabelText('Add account:')).toBeEnabled();
    expect(screen.getByLabelText('Add account:')).toHaveValue('');
  });
});

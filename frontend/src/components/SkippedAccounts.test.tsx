import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { parseHandles, SkippedAccounts } from './SkippedAccounts';
import { addSkippedAccounts, getSkippedAccounts, removeSkippedAccount } from '../api/client';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, getSkippedAccounts: vi.fn(), addSkippedAccounts: vi.fn(), removeSkippedAccount: vi.fn() };
});

const getMock = vi.mocked(getSkippedAccounts);
const addMock = vi.mocked(addSkippedAccounts);
const removeMock = vi.mocked(removeSkippedAccount);

// A stand-in for the stored list: the API stores handles lowercase, without "@"
let stored: string[] = [];
const normalize = (h: string) => h.replace(/^@/, '').toLowerCase();

beforeEach(() => {
  vi.clearAllMocks();
  stored = [];
  getMock.mockImplementation(async () => [...stored]);
  addMock.mockImplementation(async (handles) => {
    stored = [...new Set([...stored, ...handles.map(normalize)])];
    return [...stored];
  });
  removeMock.mockImplementation(async (handle) => {
    stored = stored.filter((h) => h !== normalize(handle));
    return [...stored];
  });
});

const field = () => screen.getByLabelText('Add account:');

describe('parseHandles', () => {
  it('splits at commas and spaces and drops blanks', () => {
    expect(parseHandles(' @grok, openai  someone ,')).toEqual(['@grok', 'openai', 'someone']);
  });
});

describe('SkippedAccounts (issue #48)', () => {
  it('starts empty and says what the list does', async () => {
    render(<SkippedAccounts />);

    expect(await screen.findByText('No accounts are skipped.')).toBeInTheDocument();
    expect(screen.getByText(/Posts that mention them are still\s+reviewed/)).toBeInTheDocument();
    expect(screen.getByText(/applies at each watch's next search/)).toBeInTheDocument();
  });

  it('adds the typed handles, not the whole list, and shows what was stored', async () => {
    const user = userEvent.setup();
    stored = ['spam_bot'];
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), '@Grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(addMock).toHaveBeenCalledWith(['@Grok']);
    const list = await screen.findByRole('list', { name: 'Skipped accounts' });
    expect(within(list).getByText(/@grok/)).toBeInTheDocument();
    expect(within(list).getByText(/@spam_bot/)).toBeInTheDocument();
    expect(field()).toHaveValue('');
  });

  it('removes one handle', async () => {
    const user = userEvent.setup();
    stored = ['grok', 'spam_bot'];
    render(<SkippedAccounts />);

    await user.click(await screen.findByRole('button', { name: 'Remove @grok' }));

    expect(removeMock).toHaveBeenCalledWith('grok');
    expect(await screen.findByRole('button', { name: 'Remove @spam_bot' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove @grok' })).not.toBeInTheDocument();
  });

  it('shows a refused add, keeps the list and keeps what was typed', async () => {
    const user = userEvent.setup();
    stored = ['grok'];
    addMock.mockRejectedValueOnce(new Error('Failed to add skipped accounts: handles: "x.com/someone" is not an X handle'));
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), 'x.com/someone');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('is not an X handle');
    expect(screen.getByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();
    expect(field()).toHaveValue('x.com/someone');
  });

  it('offers no editing until the stored list loads', async () => {
    const user = userEvent.setup();
    stored = ['grok'];
    getMock.mockRejectedValueOnce(new Error('Failed to load skipped accounts: Internal Server Error'));
    render(<SkippedAccounts />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load skipped accounts');
    expect(screen.queryByLabelText('Add account:')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();
    expect(addMock).not.toHaveBeenCalled();
  });

  it('ignores a load answered after a save', async () => {
    const user = userEvent.setup();
    // Strict Mode mounts twice: the second load is answered, the first only after a save
    let answerLateLoad: (handles: string[]) => void = () => {};
    getMock.mockImplementationOnce(() => new Promise((resolve) => { answerLateLoad = resolve; }));
    render(<SkippedAccounts />, { reactStrictMode: true });

    await user.type(await screen.findByLabelText('Add account:'), 'grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();

    await act(async () => answerLateLoad([]));
    expect(screen.getByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();
  });

  it('accepts no typing while a save is pending', async () => {
    const user = userEvent.setup();
    let finishSave: (handles: string[]) => void = () => {};
    addMock.mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve; }));
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), 'grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(field()).toBeDisabled();
    await act(async () => finishSave(['grok']));
    expect(field()).toBeEnabled();
    expect(field()).toHaveValue('');
  });

  it('reads the stored list again after a failed save, so a change stored despite the error is shown', async () => {
    const user = userEvent.setup();
    // The add was stored, but its answer was lost
    addMock.mockImplementationOnce(async (handles) => {
      stored = handles.map(normalize);
      throw new Error('Failed to add skipped accounts: Failed to fetch');
    });
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), 'grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to fetch');
    expect(await screen.findByRole('button', { name: 'Remove @grok' })).toBeInTheDocument();
  });

  it('offers no editing after a failed save when the list cannot be read again', async () => {
    const user = userEvent.setup();
    stored = ['spam_bot'];
    getMock.mockImplementationOnce(async () => [...stored]);
    getMock.mockRejectedValueOnce(new Error('Failed to load skipped accounts: Failed to fetch'));
    addMock.mockRejectedValueOnce(new Error('Failed to add skipped accounts: Failed to fetch'));
    render(<SkippedAccounts />);

    await user.type(await screen.findByLabelText('Add account:'), 'grok');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Add account:')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to add skipped accounts');
  });
});

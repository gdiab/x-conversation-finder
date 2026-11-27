/**
 * Skipped accounts (issue #48): X handles whose posts no watch reviews.
 *
 * One list for every watch. Each add or remove saves the whole list at once,
 * and each watch reads it at its next search, so no watch restart is needed.
 * Posts that mention a listed account are still reviewed; existing cards do
 * not change.
 */

import { useEffect, useRef, useState } from 'react';
import { getSkippedAccounts, saveSkippedAccounts } from '../api/client';

/** The handles typed in the field: split at commas and spaces, "@" kept for the API to remove. */
export function parseHandles(value: string): string[] {
  return value.split(/[\s,]+/).filter(Boolean);
}

export function SkippedAccounts() {
  // null until the stored list loads: a save replaces the whole list, so
  // nothing can be saved before the stored one is known
  const [handles, setHandles] = useState<string[] | null>(null);
  const [entry, setEntry] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Each load and save takes a number; only the latest request's answer may
  // set the list. A load answered after a save (Strict Mode mounts twice)
  // would otherwise put back the list from before the save, and the next
  // save would store that older list.
  const latest = useRef(0);

  const load = async () => {
    const request = ++latest.current;
    setError(null);
    try {
      const stored = await getSkippedAccounts();
      if (request === latest.current) setHandles(stored);
    } catch (err) {
      if (request === latest.current) {
        setError(err instanceof Error ? err.message : 'Failed to load skipped accounts');
      }
    }
  };

  useEffect(() => {
    load();
  }, []);

  const save = async (next: string[]): Promise<boolean> => {
    const request = ++latest.current;
    setSaving(true);
    setError(null);
    try {
      const stored = await saveSkippedAccounts(next);
      if (request === latest.current) setHandles(stored);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save skipped accounts');
      // The save may have been stored even though its answer was lost, so
      // the list shown may be out of date and the next save would replace
      // the stored one with it: read the stored list again before any
      // other edit (the controls stay disabled until then). If that read
      // fails too, editing waits for Retry.
      try {
        const stored = await getSkippedAccounts();
        if (request === latest.current) setHandles(stored);
      } catch {
        if (request === latest.current) setHandles(null);
      }
      return false;
    } finally {
      setSaving(false);
    }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const typed = parseHandles(entry);
    if (handles === null || typed.length === 0) return;
    if (await save([...handles, ...typed])) setEntry('');
  };

  return (
    <div style={{
      border: '1px solid #ccc',
      borderRadius: '8px',
      padding: '20px',
      marginBottom: '20px',
      backgroundColor: '#fff',
    }}>
      <h3>Skipped accounts</h3>
      <p style={{ color: '#666', fontSize: '14px' }}>
        No watch reviews posts written by these X accounts. Posts that mention them are still
        reviewed. A change applies at each watch's next search; cards already in the queue do not
        change.
      </p>

      {error && (
        <div role="alert" style={{ color: '#c62828', marginBottom: '12px' }}>
          {error}
          {handles === null && (
            <button type="button" onClick={load} style={{ marginLeft: '8px' }}>
              Retry
            </button>
          )}
        </div>
      )}

      {handles === null ? (
        !error && <div>Loading skipped accounts...</div>
      ) : (
        <>
          {handles.length === 0 ? (
            <p>No accounts are skipped.</p>
          ) : (
            <ul aria-label="Skipped accounts" style={{ listStyle: 'none', padding: 0, display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {handles.map((handle) => (
                <li key={handle} style={{ border: '1px solid #ccc', borderRadius: '4px', padding: '4px 8px' }}>
                  @{handle}{' '}
                  <button
                    type="button"
                    onClick={() => save(handles.filter((h) => h !== handle))}
                    disabled={saving}
                    aria-label={`Remove @${handle}`}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}

          <form onSubmit={add} style={{ display: 'flex', gap: '8px' }}>
            <label htmlFor="skipped-account-entry" style={{ alignSelf: 'center' }}>
              Add account:
            </label>
            <input
              id="skipped-account-entry"
              type="text"
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
              // a successful save clears the field, so nothing is typed
              // into it while one is pending
              disabled={saving}
              placeholder="e.g., @grok"
              style={{ flex: 1, padding: '8px' }}
            />
            <button type="submit" disabled={saving || parseHandles(entry).length === 0}>
              {saving ? 'Saving...' : 'Add'}
            </button>
          </form>
        </>
      )}
    </div>
  );
}

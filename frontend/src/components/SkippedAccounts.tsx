/**
 * Skipped accounts (issue #48): X handles whose posts no watch reviews.
 *
 * One list for every watch. Each add or remove is sent as that change, never
 * as the whole list, so a request answered late cannot undo another change.
 * Each watch reads the list at its next search, so no restart is needed.
 * Posts that mention a listed account are still reviewed; existing cards do
 * not change.
 */

import { Button, Input, Section } from '@outpost/ds';
import { useEffect, useRef, useState } from 'react';
import { addSkippedAccounts, getSkippedAccounts, removeSkippedAccount } from '../api/client';

/** The handles typed in the field: split at commas and spaces, "@" kept for the API to remove. */
export function parseHandles(value: string): string[] {
  return value.split(/[\s,]+/).filter(Boolean);
}

export function SkippedAccounts() {
  // null until the stored list loads
  const [handles, setHandles] = useState<string[] | null>(null);
  const [entry, setEntry] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Each load and save takes a number; only the latest request's answer may
  // set the list shown. A load answered after a save (Strict Mode mounts
  // twice) would otherwise show the list from before the save.
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

  const save = async (change: () => Promise<string[]>): Promise<boolean> => {
    const request = ++latest.current;
    setSaving(true);
    setError(null);
    try {
      const stored = await change();
      if (request === latest.current) setHandles(stored);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save skipped accounts');
      // The change may have been stored even though its answer was lost:
      // read the stored list again, so the list shown is the stored one
      // (the controls stay disabled until then). If that read fails too,
      // editing waits for Retry.
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
    if (await save(() => addSkippedAccounts(typed))) setEntry('');
  };

  return (
    <Section intensity="instrument" className="op-panel sa" aria-labelledby="sa-title">
      <div className="op-panel-head">
        <h3 id="sa-title" className="app-section-title" style={{ fontSize: '22px' }}>Skipped accounts</h3>
      </div>

      <div className="op-panel__body">
        <p className="sw-intro">
          No watch reviews posts written by these X accounts. Posts that mention them are still
          reviewed. A change applies at each watch's next search; cards already in the queue do not
          change.
        </p>

        {error && (
          <div role="alert" className="sa__error">
            {error}
            {handles === null && (
              <Button type="button" size="sm" variant="ghost" onClick={load}>
                Retry
              </Button>
            )}
          </div>
        )}

        {handles === null ? (
          !error && <p className="app-muted">Loading skipped accounts...</p>
        ) : (
          <>
            {handles.length === 0 ? (
              <p className="app-muted">No accounts are skipped.</p>
            ) : (
              <ul aria-label="Skipped accounts" className="sa__list">
                {handles.map((handle) => (
                  <li key={handle} className="sa__item">
                    @{handle}
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => save(() => removeSkippedAccount(handle))}
                      disabled={saving}
                      aria-label={`Remove @${handle}`}
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            <form onSubmit={add} className="sa__add">
              <Input
                label="Add account:"
                type="text"
                value={entry}
                onChange={(e) => setEntry(e.target.value)}
                // a successful save clears the field, so nothing is typed
                // into it while one is pending
                disabled={saving}
                placeholder="e.g., @grok"
              />
              <Button type="submit" disabled={saving || parseHandles(entry).length === 0}>
                {saving ? 'Saving...' : 'Add'}
              </Button>
            </form>
          </>
        )}
      </div>
    </Section>
  );
}

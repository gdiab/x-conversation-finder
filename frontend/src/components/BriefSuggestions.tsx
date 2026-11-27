/**
 * BriefSuggestions - "Suggest brief additions" on the Start Watch form (issue #42).
 *
 * One click reads the page at the Product URL (for a GitHub repository, its
 * README) and asks the model once for facts the brief lacks. Each suggestion
 * is a checklist item: a checkbox (off), the sentence (editable), the quote
 * from the page that supports it (checked word for word by the server) and
 * why it helps replies. Nothing reaches the brief until the founder ticks
 * items and clicks "Add checked to brief"; unticked items are never added.
 * Brief sentences the page contradicts or does not mention are shown as
 * information.
 */

import { useEffect, useRef, useState } from 'react';
import { Banner, Button, Checkbox, Textarea } from '@outpost/ds';
import {
  getBriefSuggestionCost,
  suggestBriefAdditions,
  type BriefFlag,
  type BriefSuggestionCost,
  type BriefSuggestionsResult,
} from '../api/client';

/** The heading the added sentences stand under in the brief. */
export const ADDED_HEADING = '# From the product page';

/**
 * The brief with the sentences appended as list items under ADDED_HEADING.
 *
 * The heading is level 1, so it ends whatever heading, nested heading or
 * lead-in line the brief ended under ("# Not supported", "Not offered yet:"):
 * an added fact is never read as standing under it. Each list item is one
 * brief sentence. The items continue an existing list only when the brief
 * ends with exactly that heading followed by plain list items, and nothing
 * else; otherwise a new heading starts.
 */
export function appendToBrief(brief: string, sentences: string[]): string {
  const items = sentences.map((s) => s.trim().replace(/\s+/g, ' ')).filter(Boolean);
  if (items.length === 0) return brief;
  const base = brief.replace(/\s+$/, '');
  const list = items.map((s) => `- ${s}`).join('\n');
  const lines = base.split('\n');
  const at = lines.map((line) => line.trim()).lastIndexOf(ADDED_HEADING);
  const tail = at >= 0 ? lines.slice(at + 1).filter((line) => line.trim()) : [];
  const continuesList = tail.length > 0 && tail.every((line) => /^- \S/.test(line) && !/:\s*$/.test(line));
  if (continuesList) return `${base}\n${list}`;
  return `${base ? `${base}\n\n` : ''}${ADDED_HEADING}\n\n${list}`;
}

interface Item {
  key: number;
  sentence: string;
  quote: string;
  why: string;
  checked: boolean;
}

interface BriefSuggestionsProps {
  productUrl: string;
  productName: string;
  productBrief: string;
  /** Whether the Product URL is valid (the button is off until it is). */
  urlValid: boolean;
  /** Replace the brief textarea's text. Called only by "Add checked to brief". */
  onBriefChange: (brief: string) => void;
}

/** A cost in dollars; with `ceil`, rounded up, so a maximum is never shown lower than it is. */
function formatUsd(value: number, ceil = false): string {
  const digits = value < 1 ? 4 : 2;
  const factor = 10 ** digits;
  const shown = ceil ? Math.ceil(value * factor - 1e-9) / factor : value;
  return `$${shown.toFixed(digits)}`;
}

export function BriefSuggestions({ productUrl, productName, productBrief, urlValid, onBriefChange }: BriefSuggestionsProps) {
  const [cost, setCost] = useState<BriefSuggestionCost | null>(null);
  const [costError, setCostError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BriefSuggestionsResult | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [flags, setFlags] = useState<BriefFlag[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  // A response for an older click or an older URL is ignored
  const requestSeq = useRef(0);

  useEffect(() => {
    let active = true;
    getBriefSuggestionCost()
      .then((c) => active && setCost(c))
      .catch(() => active && setCostError(true));
    return () => {
      active = false;
    };
  }, []);

  // Suggestions describe the page, brief and product name they were asked
  // with: a change to any of them drops the results and any reply still on
  // its way. (Adding to the brief changes it too; the list is already
  // cleared then, and its notice stays.)
  useEffect(() => {
    requestSeq.current += 1;
    setLoading(false);
    setResult(null);
    setItems([]);
    setFlags([]);
    setError(null);
  }, [productUrl, productBrief, productName]);

  useEffect(() => {
    setNotice(null);
  }, [productUrl]);

  const handleSuggest = async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    setNotice(null);
    // A new request replaces the list: results of an earlier brief or name are never left to tick
    setResult(null);
    setItems([]);
    setFlags([]);
    try {
      const response = await suggestBriefAdditions({
        product_url: productUrl.trim(),
        product_brief: productBrief,
        product_name: productName.trim(),
      });
      if (seq !== requestSeq.current) return;
      setResult(response);
      setItems(response.suggestions.map((s, i) => ({ key: seq * 100 + i, ...s, checked: false })));
      setFlags(response.flags);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(err instanceof Error ? err.message : 'The suggestions could not be loaded.');
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  };

  const checked = items.filter((item) => item.checked && item.sentence.trim());

  const handleAdd = () => {
    if (checked.length === 0) return;
    onBriefChange(appendToBrief(productBrief, checked.map((item) => item.sentence)));
    const dropped = items.length - checked.length;
    // The checklist is done: ticked items are in the brief, the others are dropped
    setResult(null);
    setItems([]);
    setFlags([]);
    setNotice(
      `Added ${checked.length} sentence${checked.length === 1 ? '' : 's'} to the brief under "From the product page"` +
        (dropped > 0 ? `; ${dropped} unticked suggestion${dropped === 1 ? ' was' : 's were'} dropped` : '') +
        '. Read the brief before you start the watch.',
    );
  };

  const update = (key: number, change: Partial<Item>) =>
    setItems((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)));

  return (
    <div className="bs">
      <div className="bs__head">
        <Button
          variant="ghost"
          size="sm"
          onClick={handleSuggest}
          disabled={!urlValid || !cost || loading}
        >
          {loading ? 'Reading the page...' : 'Suggest brief additions'}
        </Button>
      </div>
      <p className="bs__note">
        {cost && (
          <>
            Reads the Product URL (for a GitHub repository, its README) and makes one model call, at most{' '}
            {formatUsd(cost.max_cost_usd, true)}, for up to {cost.max_suggestions} facts your brief does not state.
            Nothing is added to the brief until you tick it.
          </>
        )}
        {!cost && costError && 'The cost of a suggestion call could not be loaded, so the button is off. Reload the page to try again.'}
        {!cost && !costError && 'Loading the cost of a suggestion call...'}
        {cost && !urlValid && ' Enter a valid Product URL first.'}
      </p>

      {error && (
        <Banner tone="critical">No suggestions: {error}</Banner>
      )}

      {notice && (
        <Banner tone="info">{notice}</Banner>
      )}

      {result && (
        <div>
          <p className="bs__note">
            From {result.source === 'github_readme' ? 'the repository README' : 'the page'}; this call cost {formatUsd(result.cost_usd)}.
            {result.page_truncated && ' The page was longer than the limit, so only its first part was read.'}
            {result.dropped_quote_not_on_page > 0 &&
              ` ${result.dropped_quote_not_on_page} suggestion${result.dropped_quote_not_on_page === 1 ? ' was' : 's were'} dropped because the quote is not on the page.`}
          </p>

          {items.length === 0 && result.suggestions.length === 0 && (
            <p className="bs__note">No suggestions passed the checks{result.dropped_other > 0 ? ` (${result.dropped_other} more dropped: a repeat, already in the brief, longer than one sentence, or over the limit)` : ''}.</p>
          )}

          {items.length > 0 && (
            <ul aria-label="Suggested brief additions" className="bs__list">
              {items.map((item, i) => (
                <li key={item.key} className={item.checked ? 'bs__item bs__item--on' : 'bs__item'}>
                  <Checkbox
                    label={`Add suggestion ${i + 1}`}
                    checked={item.checked}
                    onChange={(e) => update(item.key, { checked: e.target.checked })}
                  />
                  <Textarea
                    aria-label={`Suggested sentence ${i + 1}`}
                    value={item.sentence}
                    onChange={(e) => update(item.key, { sentence: e.target.value })}
                    rows={2}
                  />
                  <blockquote className="bs__quote">
                    From the page: “{item.quote}”
                  </blockquote>
                  {item.why && <div className="bs__why">Why: {item.why}</div>}
                </li>
              ))}
            </ul>
          )}

          {items.length > 0 && (
            <Button size="sm" onClick={handleAdd} disabled={checked.length === 0}>
              Add checked to brief
            </Button>
          )}

          {flags.length > 0 && (
            <div className="bs__flags">
              <div className="rq-callout__title">
                Brief sentences the page does not support (for information: you may have other sources)
              </div>
              <ul aria-label="Brief sentences the page does not support">
                {flags.map((flag) => (
                  <li key={flag.brief_sentence_id}>
                    {flag.brief_sentence_context && `(${flag.brief_sentence_context}) `}“{flag.brief_sentence}”: {flag.kind === 'contradicted' ? 'the page contradicts it' : 'the page does not mention it'}
                    {flag.note && `. ${flag.note}`}
                    {flag.quote && <> Page: “{flag.quote}”</>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

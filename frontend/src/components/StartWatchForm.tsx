/**
 * StartWatchForm component - form to start a new topic watch.
 */

import { useState } from 'react';
import { Banner, Button, Input, Section, Textarea } from '@outpost/ds';
import { startWatch } from '../api/client';
import { BriefSuggestions } from './BriefSuggestions';

interface StartWatchFormProps {
  onSuccess: () => void;
}

/**
 * A comma-separated field as a list: "Relay, AcmeRelay" -> ["Relay", "AcmeRelay"].
 * Blanks and repeats are dropped. Used for "Also called" and "Key features".
 */
export function parseCommaList(value: string): string[] {
  const entries: string[] = [];
  for (const part of value.split(',')) {
    const entry = part.trim().replace(/\s+/g, ' ');
    if (entry && !entries.some((e) => e.toLowerCase() === entry.toLowerCase())) {
      entries.push(entry);
    }
  }
  return entries;
}

// The server's limit (backend/gate/product_link.py MAX_PRODUCT_URL_CHARS)
const MAX_PRODUCT_URL_CHARS = 2048;

/**
 * Why a value cannot be the product URL, or null (issue #41). The server
 * checks the same rules: an https link to a site, without spaces.
 */
export function productUrlError(value: string): string | null {
  const url = value.trim();
  if (!url) return 'Enter the product URL.';
  if (url.length > MAX_PRODUCT_URL_CHARS) {
    return `Enter a product URL of at most ${MAX_PRODUCT_URL_CHARS} characters.`;
  }
  if (/\s/.test(url) || !/^https:\/\//i.test(url)) {
    return 'Enter the product URL as one https:// link, for example https://example.com.';
  }
  try {
    const host = new URL(url).hostname;
    if (!host.replace(/^\.+|\.+$/g, '').includes('.')) {
      return 'Enter the product URL as one https:// link, for example https://example.com.';
    }
  } catch {
    return 'Enter the product URL as one https:// link, for example https://example.com.';
  }
  return null;
}

export function StartWatchForm({ onSuccess }: StartWatchFormProps) {
  const [account, setAccount] = useState('');
  const [topic, setTopic] = useState('');
  const [query, setQuery] = useState('');
  const [productBrief, setProductBrief] = useState('');
  const [productName, setProductName] = useState('');
  const [alsoCalled, setAlsoCalled] = useState('');
  const [keyFeatures, setKeyFeatures] = useState('');
  const [productUrl, setProductUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSuccess(null);

    // "required" accepts a name made of spaces; the server does not
    if (!productName.trim()) {
      setError('Enter the product name.');
      return;
    }
    const urlError = productUrlError(productUrl);
    if (urlError) {
      setError(urlError);
      return;
    }
    
    setLoading(true);
    setError(null);
    
    try {
      const result = await startWatch({
        account,
        topic,
        query,
        product_brief: productBrief,
        product_name: productName.trim(),
        product_aliases: parseCommaList(alsoCalled),
        key_features: parseCommaList(keyFeatures),
        product_url: productUrl.trim(),
      });
      
      setSuccess(result.message);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start watch');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Section intensity="instrument" className="op-panel sw" aria-labelledby="sw-title">
      <div className="op-panel-head">
        <h3 id="sw-title" className="app-section-title" style={{ fontSize: '22px' }}>Start New Topic Watch</h3>
      </div>

      <form onSubmit={handleSubmit} className="op-panel__body">
        <fieldset className="sw-group">
          <legend className="sw-group__legend">Search</legend>
          <div className="sw-grid sw-grid--3">
            <Input
              label="Account (your X handle):"
              type="text"
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              required
              placeholder="e.g., john_doe"
            />
            <Input
              label="Topic:"
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              required
              placeholder="e.g., reliability"
            />
            <Input
              label="Search Query:"
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              required
              placeholder="e.g., distributed systems reliability"
            />
          </div>
        </fieldset>

        <fieldset className="sw-group">
          <legend className="sw-group__legend">Product</legend>
          <div className="sw-grid">
            <Input
              label="Product name:"
              type="text"
              value={productName}
              onChange={(e) => setProductName(e.target.value)}
              required
              maxLength={80}
              placeholder="e.g., Acme Relay"
            />
            <Input
              label="Product URL:"
              type="url"
              value={productUrl}
              onChange={(e) => setProductUrl(e.target.value)}
              required
              placeholder="e.g., https://example.com/acme-relay"
              hint={
                'An https:// link. A draft that names the product ends with this link, added by code after ' +
                'the draft passed its check. X counts a link as 23 characters, so such a draft has at most ' +
                '256 characters before it.'
              }
            />
            <Input
              label="Also called (optional):"
              type="text"
              value={alsoCalled}
              onChange={(e) => setAlsoCalled(e.target.value)}
              placeholder="e.g., Relay, AcmeRelay"
              hint={
                'Other names people use for the product, separated by commas. In written drafts, a reply ' +
                'sentence that uses the product name or one of these names is checked against your brief.'
              }
            />
            <Input
              label="Key features (optional):"
              type="text"
              value={keyFeatures}
              onChange={(e) => setKeyFeatures(e.target.value)}
              placeholder="e.g., Signed deliveries, Delivery replay"
              hint={
                'Short names of features your brief describes, separated by commas (at most 6 words each). ' +
                'In written drafts, a reply sentence that uses one of them is checked against your brief. ' +
                'Key features are read only from this field, not from the brief. Enter a second spelling ' +
                'of a feature as its own entry.'
              }
            />
          </div>
        </fieldset>

        <div className="sw-brief">
          <Textarea
            label="Product Brief:"
            value={productBrief}
            onChange={(e) => setProductBrief(e.target.value)}
            required
            placeholder="Describe your product, key features, and positioning..."
          />
        </div>

        <BriefSuggestions
          productUrl={productUrl}
          productName={productName}
          productBrief={productBrief}
          urlValid={productUrlError(productUrl) === null}
          onBriefChange={setProductBrief}
        />

        {error && <Banner tone="critical">{error}</Banner>}

        {success && <Banner tone="info">{success}</Banner>}

        <div className="sw-foot">
          <Button type="submit" disabled={loading}>
            {loading ? 'Starting...' : 'Start Watch'}
          </Button>
        </div>
      </form>
    </Section>
  );
}

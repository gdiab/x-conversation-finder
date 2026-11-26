/**
 * StartWatchForm component - form to start a new topic watch.
 */

import { useState } from 'react';
import { startWatch } from '../api/client';

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

/**
 * Why a value cannot be the product URL, or null (issue #41). The server
 * checks the same rules: an https link to a site, without spaces.
 */
export function productUrlError(value: string): string | null {
  const url = value.trim();
  if (!url) return 'Enter the product URL.';
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
    <div style={{
      border: '1px solid #ccc',
      borderRadius: '8px',
      padding: '20px',
      marginBottom: '20px',
      backgroundColor: '#fff',
    }}>
      <h3>Start New Topic Watch</h3>
      
      <form onSubmit={handleSubmit}>
        <div style={{ marginBottom: '12px' }}>
          <label style={{ display: 'block', marginBottom: '4px' }}>
            Account (your X handle):
          </label>
          <input
            type="text"
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            required
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., john_doe"
          />
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label style={{ display: 'block', marginBottom: '4px' }}>
            Topic:
          </label>
          <input
            type="text"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            required
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., reliability"
          />
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label style={{ display: 'block', marginBottom: '4px' }}>
            Search Query:
          </label>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            required
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., distributed systems reliability"
          />
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label htmlFor="watch-product-name" style={{ display: 'block', marginBottom: '4px' }}>
            Product name:
          </label>
          <input
            id="watch-product-name"
            type="text"
            value={productName}
            onChange={(e) => setProductName(e.target.value)}
            required
            maxLength={80}
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., Acme Relay"
          />
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label htmlFor="watch-product-url" style={{ display: 'block', marginBottom: '4px' }}>
            Product URL:
          </label>
          <input
            id="watch-product-url"
            type="url"
            value={productUrl}
            onChange={(e) => setProductUrl(e.target.value)}
            required
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., https://example.com/acme-relay"
          />
          <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
            An https:// link. A draft that names the product ends with this link, added by code after
            the draft passed its check. X counts a link as 23 characters, so such a draft has at most
            256 characters before it.
          </div>
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label htmlFor="watch-also-called" style={{ display: 'block', marginBottom: '4px' }}>
            Also called (optional):
          </label>
          <input
            id="watch-also-called"
            type="text"
            value={alsoCalled}
            onChange={(e) => setAlsoCalled(e.target.value)}
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., Relay, AcmeRelay"
          />
          <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
            Other names people use for the product, separated by commas. In written drafts, a reply
            sentence that uses the product name or one of these names is checked against your brief.
          </div>
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label htmlFor="watch-key-features" style={{ display: 'block', marginBottom: '4px' }}>
            Key features (optional):
          </label>
          <input
            id="watch-key-features"
            type="text"
            value={keyFeatures}
            onChange={(e) => setKeyFeatures(e.target.value)}
            style={{ width: '100%', padding: '8px' }}
            placeholder="e.g., Signed deliveries, Delivery replay"
          />
          <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
            Short names of features your brief describes, separated by commas (at most 6 words each).
            In written drafts, a reply sentence that uses one of them is checked against your brief.
            Key features are read only from this field, not from the brief. Enter a second spelling
            of a feature as its own entry.
          </div>
        </div>

        <div style={{ marginBottom: '12px' }}>
          <label style={{ display: 'block', marginBottom: '4px' }}>
            Product Brief:
          </label>
          <textarea
            value={productBrief}
            onChange={(e) => setProductBrief(e.target.value)}
            required
            style={{ width: '100%', minHeight: '150px', padding: '8px' }}
            placeholder="Describe your product, key features, and positioning..."
          />
        </div>

        {error && (
          <div style={{
            backgroundColor: '#ffebee',
            color: '#c62828',
            padding: '12px',
            borderRadius: '4px',
            marginBottom: '12px',
          }}>
            {error}
          </div>
        )}

        {success && (
          <div style={{
            backgroundColor: '#e8f5e9',
            color: '#2e7d32',
            padding: '12px',
            borderRadius: '4px',
            marginBottom: '12px',
          }}>
            {success}
          </div>
        )}

        <button
          type="submit"
          disabled={loading}
          style={{
            backgroundColor: '#2196f3',
            color: '#fff',
            border: 'none',
            padding: '10px 20px',
            borderRadius: '4px',
            cursor: 'pointer',
          }}
        >
          {loading ? 'Starting...' : 'Start Watch'}
        </button>
      </form>
    </div>
  );
}

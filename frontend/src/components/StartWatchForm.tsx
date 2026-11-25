/**
 * StartWatchForm component - form to start a new topic watch.
 */

import { useState } from 'react';
import { startWatch } from '../api/client';

interface StartWatchFormProps {
  onSuccess: () => void;
}

/** "Relay, AcmeRelay" -> ["Relay", "AcmeRelay"]; blanks and repeats are dropped. */
export function parseAlsoCalled(value: string): string[] {
  const names: string[] = [];
  for (const part of value.split(',')) {
    const name = part.trim().replace(/\s+/g, ' ');
    if (name && !names.some((n) => n.toLowerCase() === name.toLowerCase())) {
      names.push(name);
    }
  }
  return names;
}

export function StartWatchForm({ onSuccess }: StartWatchFormProps) {
  const [account, setAccount] = useState('');
  const [topic, setTopic] = useState('');
  const [query, setQuery] = useState('');
  const [productBrief, setProductBrief] = useState('');
  const [productName, setProductName] = useState('');
  const [alsoCalled, setAlsoCalled] = useState('');
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
    
    setLoading(true);
    setError(null);
    
    try {
      const result = await startWatch({
        account,
        topic,
        query,
        product_brief: productBrief,
        product_name: productName.trim(),
        product_aliases: parseAlsoCalled(alsoCalled),
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

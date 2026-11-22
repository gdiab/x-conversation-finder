/**
 * StartWatchForm component - form to start a new topic watch.
 */

import { useState } from 'react';
import { startWatch } from '../api/client';

interface StartWatchFormProps {
  onSuccess: () => void;
}

export function StartWatchForm({ onSuccess }: StartWatchFormProps) {
  const [account, setAccount] = useState('');
  const [topic, setTopic] = useState('');
  const [query, setQuery] = useState('');
  const [productBrief, setProductBrief] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    setLoading(true);
    setError(null);
    setSuccess(null);
    
    try {
      const result = await startWatch({
        account,
        topic,
        query,
        product_brief: productBrief,
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

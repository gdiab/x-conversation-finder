/**
 * Main App component.
 */

import { useState, useEffect } from 'react';
import { ReviewCard } from './components/ReviewCard';
import { StartWatchForm } from './components/StartWatchForm';
import { listReviews } from './api/client';
import type { ReviewCard as ReviewCardType } from './api/client';

function App() {
  const [cards, setCards] = useState<ReviewCardType[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadReviews = async () => {
    setLoading(true);
    setError(null);
    
    try {
      const result = await listReviews();
      setCards(result.cards.sort((a, b) => 
        b.evidence.confidence_score - a.evidence.confidence_score
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load reviews');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadReviews();
    const interval = setInterval(loadReviews, 30000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '20px' }}>
      <header style={{ marginBottom: '30px', borderBottom: '2px solid #333', paddingBottom: '20px' }}>
        <h1>X Conversation Finder</h1>
        <p style={{ color: '#666' }}>
          Review posts, check evidence, approve or skip drafts
        </p>
      </header>

      <StartWatchForm onSuccess={loadReviews} />

      <div style={{ marginBottom: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2>Review Queue ({cards.length})</h2>
        <button onClick={loadReviews} disabled={loading}>
          Refresh
        </button>
      </div>

      {error && (
        <div style={{
          backgroundColor: '#ffebee',
          color: '#c62828',
          padding: '12px',
          borderRadius: '4px',
          marginBottom: '20px',
        }}>
          {error}
        </div>
      )}

      {loading && cards.length === 0 ? (
        <div>Loading reviews...</div>
      ) : cards.length === 0 ? (
        <div style={{
          textAlign: 'center',
          padding: '40px',
          backgroundColor: '#f5f5f5',
          borderRadius: '8px',
        }}>
          No reviews yet. Start a topic watch above to begin finding conversations.
        </div>
      ) : (
        <div>
          {cards.map((card) => (
            <ReviewCard
              key={card.workflow_id}
              card={card}
              onUpdate={loadReviews}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default App;

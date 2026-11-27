/**
 * Main App component.
 */

import { useState, useEffect, useRef } from 'react';
import { ReviewCard } from './components/ReviewCard';
import { StartWatchForm } from './components/StartWatchForm';
import { compareQueueOrder } from './components/ReachSummary';
import { listReviews } from './api/client';
import type { ReviewCard as ReviewCardType } from './api/client';

function App() {
  const [cards, setCards] = useState<ReviewCardType[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSkipped, setShowSkipped] = useState(false);

  // Skipped cards have no draft to post; keep them out of the queue.
  const activeCards = cards.filter((c) => c.status !== 'skipped');
  const skippedCards = cards.filter((c) => c.status === 'skipped');

  // The periodic refresh and an action's refresh can overlap; only the
  // latest request may replace the cards, or an older response could put
  // back a draft that was just edited (issue #15 review)
  const latestLoad = useRef(0);

  const loadReviews = async () => {
    const load = ++latestLoad.current;
    setLoading(true);
    setError(null);
    
    try {
      const result = await listReviews();
      if (load !== latestLoad.current) return;
      // issue #49: confidence plus reach (queue_priority, set by the backend)
      setCards(result.cards.sort(compareQueueOrder));
    } catch (err) {
      if (load !== latestLoad.current) return;
      setError(err instanceof Error ? err.message : 'Failed to load reviews');
    } finally {
      if (load === latestLoad.current) setLoading(false);
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
        <h2>Review Queue ({activeCards.length})</h2>
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
      ) : activeCards.length === 0 ? (
        <div style={{
          textAlign: 'center',
          padding: '40px',
          backgroundColor: '#f5f5f5',
          borderRadius: '8px',
        }}>
          {skippedCards.length > 0
            ? 'No drafts to review. Every post found so far was skipped (see below).'
            : 'No reviews yet. Start a topic watch above to begin finding conversations.'}
        </div>
      ) : (
        <div>
          {activeCards.map((card) => (
            <ReviewCard
              key={card.workflow_id}
              card={card}
              onUpdate={loadReviews}
            />
          ))}
        </div>
      )}

      {skippedCards.length > 0 && (
        <div style={{ marginTop: '30px' }}>
          <button onClick={() => setShowSkipped(!showSkipped)}>
            {showSkipped ? 'Hide' : 'Show'} skipped ({skippedCards.length})
          </button>
          <p style={{ color: '#666', fontSize: '14px' }}>
            Posts with no draft to post: the post was not a reply opportunity (for example a promo,
            a roundup or a vendor post), its confidence was below the threshold, or no reply could be
            backed by your product brief. Each card says which.
          </p>
          {showSkipped && skippedCards.map((card) => (
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

/**
 * Main App component.
 */

import { useState, useEffect, useRef } from 'react';
import { Banner, Button, Section } from '@outpost/ds';
import { ReviewCard } from './components/ReviewCard';
import { StartWatchForm } from './components/StartWatchForm';
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
      setCards(result.cards.sort((a, b) => 
        b.evidence.confidence_score - a.evidence.confidence_score
      ));
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
    <Section intensity="deck" className="app" aria-labelledby="app-title">
      <header className="app-head">
        <div>
          <h1 id="app-title" className="app-head__title">X Conversation Finder</h1>
          <p className="app-head__sub">
            Review posts, check evidence, approve or skip drafts
          </p>
        </div>
      </header>

      <StartWatchForm onSuccess={loadReviews} />

      <div className="rq-bar">
        <h2 className="app-section-title">Review Queue ({activeCards.length})</h2>
        <Button variant="ghost" size="sm" onClick={loadReviews} disabled={loading}>
          Refresh
        </Button>
      </div>

      {error && (
        <Banner tone="critical" style={{ marginBottom: 'var(--op-space-4)' }}>
          {error}
        </Banner>
      )}

      {loading && cards.length === 0 ? (
        <div className="rq-empty">Loading reviews...</div>
      ) : activeCards.length === 0 ? (
        <div className="rq-empty">
          {skippedCards.length > 0
            ? 'No drafts to review. Every post found so far was skipped (see below).'
            : 'No reviews yet. Start a topic watch above to begin finding conversations.'}
        </div>
      ) : (
        <div className="rq-list">
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
        <div className="rq-skipped">
          <Button variant="ghost" size="sm" onClick={() => setShowSkipped(!showSkipped)} aria-expanded={showSkipped}>
            {showSkipped ? 'Hide' : 'Show'} skipped ({skippedCards.length})
          </Button>
          <p className="rq-skipped__note">
            Posts with no draft to post: the post was not a reply opportunity (for example a promo,
            a roundup or a vendor post), its confidence was below the threshold, or no reply could be
            backed by your product brief. Each card says which.
          </p>
          {showSkipped && (
            <div className="rq-list">
              {skippedCards.map((card) => (
                <ReviewCard
                  key={card.workflow_id}
                  card={card}
                  onUpdate={loadReviews}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

export default App;

/**
 * issue #49: a card's reach (how many people a reply could reach), kept
 * apart from the card layout so the card redesign can move it freely.
 * The score, the highlight level and the sort key are set by the backend
 * (backend/config/reach.py); this file only shows them.
 */

import type { ReviewCard } from '../api/client';

/** Highlight colour for a card at or above the backend's reach level. */
export const HIGH_REACH_BORDER = '#1565c0';

/** The pending queue's order: the backend's queue_priority, else confidence. */
export function queueSortKey(card: ReviewCard): number {
  return typeof card.queue_priority === 'number' ? card.queue_priority : card.evidence.confidence_score;
}

export function compareQueueOrder(a: ReviewCard, b: ReviewCard): number {
  return queueSortKey(b) - queueSortKey(a);
}

export function HighReachBadge({ card }: { card: ReviewCard }) {
  if (!card.reach_highlighted || !card.reach) return null;
  return (
    <span
      title={`Reach ${card.reach.score.toFixed(2)} (rule ${card.reach.rule_version})`}
      style={{
        padding: '4px 12px',
        borderRadius: '4px',
        fontSize: '14px',
        backgroundColor: HIGH_REACH_BORDER,
        color: '#fff',
        marginRight: '8px',
      }}
    >
      High reach
    </span>
  );
}

/** The raw counts line: from the stored reach inputs, else from the post. */
export function ReachCounts({ card, showFollowers }: { card: ReviewCard; showFollowers: boolean }) {
  const reach = card.reach;
  const counts = reach
    ? reach.inputs
    : {
        like_count: card.post.like_count,
        reply_count: card.post.reply_count,
        repost_count: card.post.repost_count,
        quote_count: card.post.quote_count,
        followers_count: showFollowers ? card.post.author?.followers_count : undefined,
      };
  const parts: string[] = [];
  const add = (n: number | undefined, label: string) => {
    if (typeof n === 'number') parts.push(`${n.toLocaleString()} ${label}`);
  };
  add(counts.like_count, 'likes');
  add(counts.reply_count, 'replies');
  add(counts.repost_count, 'reposts');
  add(counts.quote_count, 'quotes');
  if (showFollowers) add(counts.followers_count, 'followers');
  return (
    <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }} data-testid="reach-counts">
      {parts.join(' · ')}
      {reach && <> · Reach: {(reach.score * 100).toFixed(0)}%</>}
    </div>
  );
}

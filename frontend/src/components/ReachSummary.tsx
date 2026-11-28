/**
 * issue #49: a card's reach (how many people a reply could reach), kept
 * apart from the card layout so the card redesign can move it freely.
 * The score, the highlight level and the sort key are set by the backend
 * (backend/config/reach.py); this file only shows them.
 */

import { Tag } from '@outpost/ds';
import type { ReviewCard } from '../api/client';

/** A pending card's sort key: the backend's queue_priority, else confidence. */
export function queueSortKey(card: ReviewCard): number {
  return typeof card.queue_priority === 'number' ? card.queue_priority : card.evidence.confidence_score;
}

/**
 * The queue's order: pending cards first, by queue_priority (confidence plus
 * reach); then every other card (expired, approved, ...), newest first. A
 * card that can no longer be acted on must not bury one that can, however
 * high its confidence.
 */
export function compareQueueOrder(a: ReviewCard, b: ReviewCard): number {
  const aPending = a.status === 'pending';
  const bPending = b.status === 'pending';
  if (aPending !== bPending) return aPending ? -1 : 1;
  if (aPending) return queueSortKey(b) - queueSortKey(a);
  return b.created_at.localeCompare(a.created_at);
}

export function HighReachBadge({ card }: { card: ReviewCard }) {
  if (!card.reach_highlighted || !card.reach) return null;
  return (
    <Tag accent="moss" title={`Reach ${card.reach.score.toFixed(2)} (rule ${card.reach.rule_version})`}>
      High reach
    </Tag>
  );
}

/** The raw counts line: from the stored reach inputs, else from the post. */
export function ReachCounts({
  card,
  showFollowers,
  className = 'rq-counts',
}: {
  card: ReviewCard;
  showFollowers: boolean;
  className?: string;
}) {
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
    <div className={className} data-testid="reach-counts">
      {parts.join(' · ')}
      {reach && <> · Reach: {(reach.score * 100).toFixed(0)}%</>}
    </div>
  );
}

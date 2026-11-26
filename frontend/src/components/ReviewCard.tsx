/**
 * ReviewCard component - shows a single review with evidence and draft.
 */

import { useState, useRef, useEffect } from 'react';
import type { ReviewCard as ReviewCardType } from '../api/client';
import { approveReview, skipReview, editReview, nudgeReview, confirmPostedReview, generateRequestId, PostRemovedError, ReviewApiError } from '../api/client';
import { ReplyFitPanel } from './ReplyFitPanel';
import { ReplyPlanPanel } from './ReplyPlanPanel';
import { SentenceCheckList } from './SentenceCheckList';

interface ReviewCardProps {
  card: ReviewCardType;
  // Reloads the queue. After an edit or nudge the card awaits it, so the
  // actions stay disabled until the changed draft is shown.
  onUpdate: () => void | Promise<void>;
}

// issue #20: shown instead of an error when the request can be sent again
const NOT_READY_NOTICE = 'This review is still being prepared. Try again in a moment.';

// X's reply limit, and the length X counts a link as (it shortens links to
// t.co). A draft that names the product ends with the watch's product URL
// (issue #41), which can be longer than 23 characters as typed.
export const X_MAX_CHARS = 280;
export const X_LINK_CHARS = 23;

/**
 * A text's length as the server's check counts it (backend/gate/product_link.py):
 * the text without surrounding whitespace, and the watch's product URL, when
 * it is the last word (after whitespace, or alone), as X_LINK_CHARS plus the
 * whitespace typed before it. Every other character, other links included,
 * counts as typed.
 */
export function xLength(text: string, productUrl?: string | null): number {
  const trimmed = text.trimEnd();
  if (productUrl && trimmed.endsWith(productUrl)) {
    const before = trimmed.slice(0, trimmed.length - productUrl.length);
    if (before === '' || /\s$/.test(before)) {
      const body = before.trimEnd();
      return body.trimStart().length + (before.length - body.length) + X_LINK_CHARS;
    }
  }
  return text.trim().length;
}

/** A "try again" notice for a not_ready/busy response, or null if the error is a real failure. */
function retryNotice(err: unknown): string | null {
  if (!(err instanceof ReviewApiError) || !err.retryLater) return null;
  return err.code === 'not_ready' ? NOT_READY_NOTICE : err.detail;
}

export function ReviewCard({ card, onUpdate }: ReviewCardProps) {
  const [postedUrl, setPostedUrl] = useState('');
  const [replyUrl, setReplyUrl] = useState('');
  const [skipReason, setSkipReason] = useState('');
  const [editText, setEditText] = useState(card.evidence.draft?.text || '');
  const [nudgeInstruction, setNudgeInstruction] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Not a failure: the request was refused for now (not_ready, busy) and can be retried
  const [notice, setNotice] = useState<string | null>(null);
  const [failingSentences, setFailingSentences] = useState<string[]>([]);
  const [isEditing, setIsEditing] = useState(false);
  const [isNudging, setIsNudging] = useState(false);
  const [showPostAnywayConfirm, setShowPostAnywayConfirm] = useState(false);
  
  // Stable request IDs - generated once per user action attempt, reused on retries
  const approveRequestId = useRef<string | null>(null);
  const skipRequestId = useRef<string | null>(null);
  const editRequestId = useRef<string | null>(null);
  const nudgeRequestId = useRef<string | null>(null);
  const confirmRequestId = useRef<string | null>(null);

  // The override acknowledges the failures of one draft. Once the draft or
  // its gate result changes (an edit, a nudge, a reload), close the prompt:
  // only a fresh post_anyway_required for the current draft reopens it.
  const closePostAnyway = () => {
    setShowPostAnywayConfirm(false);
    setFailingSentences([]);
  };
  const draftText = card.evidence.draft?.text;
  useEffect(closePostAnyway, [draftText, card.edited_text_gate_state]);
  // The edit box follows the card while it is closed, so it never opens on a
  // draft the card no longer has. When the post was removed (issue #9), the
  // edit box and the nudge field are emptied and closed even while open: a
  // written draft is model text that can quote the post, and neither it nor
  // a quote of it may stay on screen (issue #12).
  const textRemoved = card.post.text_removed === true;
  // issue #35: a removal also removes the author and the post URL. A card
  // that still has them (a stale response) does not show them once removed.
  const author = textRemoved ? undefined : card.post.author;
  const postUrl = textRemoved ? undefined : card.post.url;
  useEffect(() => {
    if (textRemoved) {
      setIsEditing(false);
      editRequestId.current = null;
      // A nudge being typed may quote the draft: it goes with it
      setIsNudging(false);
      setNudgeInstruction('');
      nudgeRequestId.current = null;
    }
    if (textRemoved || !isEditing) {
      setEditText(draftText ?? '');
    }
    // isEditing is left out on purpose: closing the box by hand must not
    // discard what Cancel and Save already handle
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftText, textRemoved]);
  // issue #12: a written draft is the model's free text, checked per sentence
  // against the brief. An assembled draft is shown by its segments.
  const written = card.evidence.draft?.mode === 'written';
  const edited = card.edited_text_gate_state != null;
  // The draft as currently shown, so a rejection that arrives after the card
  // changed is not taken as a rejection of the new draft
  const shownDraft = `${draftText ?? ''}\u0000${card.edited_text_gate_state ?? ''}`;
  const shownDraftRef = useRef(shownDraft);
  shownDraftRef.current = shownDraft;

  const handleApprove = async (postAnyway: boolean = false) => {
    if (!postedUrl.trim()) {
      setError('Please enter the posted URL');
      return;
    }
    
    // Generate request ID once per action attempt (on first click)
    if (!approveRequestId.current) {
      approveRequestId.current = generateRequestId();
    }
    
    setLoading(true);
    setError(null);
    setNotice(null);
    setShowPostAnywayConfirm(false);
    const draftAtRequest = shownDraftRef.current;
    
    try {
      // No draft shown means nothing to approve; the server refuses '' as changed
      await approveReview(card.workflow_id, postedUrl, approveRequestId.current, postAnyway, {
        text: card.evidence.draft?.text ?? '',
        gateState: card.edited_text_gate_state,
      });
      approveRequestId.current = null; // Reset for next action
      onUpdate();
    } catch (err) {
      // issue #9: approve re-fetched the post and found it deleted; the
      // review is skipped now, so reload to show the "Post removed" card
      if (err instanceof PostRemovedError) {
        approveRequestId.current = null;
        setError(null);
        onUpdate();
        return;
      }
      const wait = retryNotice(err);
      if (wait) {
        setNotice(wait);
        return;
      }
      // The server's draft is not the one shown: reload and let the founder look again
      if (err instanceof ReviewApiError && err.code === 'draft_changed') {
        setNotice('The draft changed since it was shown. Review the current draft and approve again.');
        onUpdate();
        return;
      }
      // issue #15: the API says so with a code; the message text is for people
      if (err instanceof ReviewApiError && err.code === 'post_anyway_required' && !postAnyway) {
        if (shownDraftRef.current !== draftAtRequest) {
          // The draft changed while the request ran: the failures listed are
          // not the shown draft's. Ask for a fresh approve instead.
          setNotice('The draft changed while approving. Review it and approve again.');
          return;
        }
        setFailingSentences(err.failingSentences);
        setShowPostAnywayConfirm(true);
      }
      setError(err instanceof Error ? err.message : 'Failed to approve');
    } finally {
      setLoading(false);
    }
  };

  const handleSkip = async () => {
    // Generate request ID once per action attempt
    if (!skipRequestId.current) {
      skipRequestId.current = generateRequestId();
    }
    
    setLoading(true);
    setError(null);
    setNotice(null);
    
    try {
      await skipReview(card.workflow_id, skipReason || 'Not relevant', skipRequestId.current);
      skipRequestId.current = null; // Reset for next action
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to skip');
    } finally {
      setLoading(false);
    }
  };

  const handleEdit = async () => {
    if (!editText.trim()) {
      setError('Draft cannot be empty');
      return;
    }
    
    // Generate request ID once per action attempt
    if (!editRequestId.current) {
      editRequestId.current = generateRequestId();
    }
    
    setLoading(true);
    setError(null);
    setNotice(null);
    
    try {
      await editReview(card.workflow_id, editText, editRequestId.current);
      editRequestId.current = null; // Reset for next action
      setIsEditing(false);
      closePostAnyway();
      // Keep the actions disabled until the edited draft is shown, so an
      // approve (and its override prompt) never applies to an unseen draft
      await onUpdate();
    } catch (err) {
      const wait = retryNotice(err);
      if (wait) setNotice(wait);
      else setError(err instanceof Error ? err.message : 'Failed to edit');
      // issue #23: the edit is refused once the post's removal is recorded;
      // reload to show the card without the post text
      if (err instanceof PostRemovedError) {
        setIsEditing(false);
        onUpdate();
      }
    } finally {
      setLoading(false);
    }
  };

  const handleNudge = async () => {
    if (!nudgeInstruction.trim()) {
      setError('Please enter an instruction');
      return;
    }
    
    // Generate request ID once per action attempt
    if (!nudgeRequestId.current) {
      nudgeRequestId.current = generateRequestId();
    }
    
    setLoading(true);
    setError(null);
    setNotice(null);
    
    try {
      const newDraft = await nudgeReview(card.workflow_id, nudgeInstruction, nudgeRequestId.current);
      nudgeRequestId.current = null; // Reset for next action
      setEditText(newDraft);
      setNudgeInstruction('');
      setIsNudging(false);
      closePostAnyway();
      await onUpdate(); // as for edit: no approve before the new draft is shown
    } catch (err) {
      const wait = retryNotice(err);
      if (wait) setNotice(wait);
      else setError(err instanceof Error ? err.message : 'Failed to nudge');
      // issue #9: another Review of the post recorded its removal; reload to
      // show the card without the post text
      if (err instanceof PostRemovedError) onUpdate();
    } finally {
      setLoading(false);
    }
  };

  const handleConfirmPosted = async () => {
    if (!replyUrl.trim()) {
      setError('Please enter the reply URL');
      return;
    }
    
    // Generate request ID once per action attempt
    if (!confirmRequestId.current) {
      confirmRequestId.current = generateRequestId();
    }
    
    setLoading(true);
    setError(null);
    setNotice(null);
    
    try {
      await confirmPostedReview(card.workflow_id, replyUrl, confirmRequestId.current);
      confirmRequestId.current = null; // Reset for next action
      onUpdate();
    } catch (err) {
      const wait = retryNotice(err);
      if (wait) setNotice(wait);
      else setError(err instanceof Error ? err.message : 'Failed to confirm posted');
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  return (
    <div style={{
      border: '1px solid #ccc',
      borderRadius: '8px',
      padding: '20px',
      marginBottom: '20px',
      backgroundColor: '#fff',
    }}>
      {/* Header */}
      <div style={{ marginBottom: '20px', borderBottom: '1px solid #eee', paddingBottom: '10px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0 }}>{author ? `Review: ${author.username}` : 'Review'}</h3>
          <span style={{
            padding: '4px 12px',
            borderRadius: '4px',
            fontSize: '14px',
            backgroundColor: card.status === 'pending' ? '#ffa500' : '#ccc',
            color: '#fff',
          }}>
            {card.status}
          </span>
        </div>
        <div style={{ fontSize: '14px', color: '#666', marginTop: '4px' }}>
          Confidence: {(card.evidence.confidence_score * 100).toFixed(0)}% | 
          Topic: {card.evidence.matched_topic}
        </div>
      </div>

      {/* Original Post */}
      <div style={{ marginBottom: '20px' }}>
        <h4>Original Post</h4>
        <div style={{ 
          backgroundColor: '#f5f5f5', 
          padding: '12px', 
          borderRadius: '4px',
          marginBottom: '8px',
        }}>
          {author && (
            <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>
              {author.name} (@{author.username})
            </div>
          )}
          {card.post.text_removed ? (
            <div style={{ fontStyle: 'italic', color: '#666' }}>
              Post removed: it was deleted or is no longer visible on X, so its text, its author, and the research and edits quoting it were removed.
            </div>
          ) : (
            <div>{card.post.text}</div>
          )}
          <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
            {card.post.like_count} likes · {card.post.reply_count} replies
            {author && <> · {author.followers_count.toLocaleString()} followers</>}
          </div>
        </div>
        {postUrl && (
          <a href={postUrl} target="_blank" rel="noopener noreferrer" style={{ fontSize: '14px' }}>
            View on X →
          </a>
        )}
      </div>

      {/* Evidence Trail */}
      <div style={{ marginBottom: '20px' }}>
        <h4>Evidence & Research</h4>
        <div style={{ marginBottom: '12px' }}>
          <strong>Why it fits:</strong> {card.evidence.why_it_fits}
        </div>

        {/* Issue #4: should-we-reply features and checks */}
        <ReplyFitPanel
          replyFit={card.evidence.reply_fit}
          confidenceInputs={card.evidence.confidence_inputs}
        />
        
        {card.evidence.founder_materials.length > 0 && (
          <div style={{ marginBottom: '12px' }}>
            <strong>Supporting Material:</strong>
            {!written && (
              <div style={{ fontSize: '12px', color: '#666', marginTop: '4px', marginBottom: '8px' }}>
                Sentences starting with unresolved referents (it/this/that/etc.) cannot be selected as claims.
              </div>
            )}
            {card.evidence.founder_materials.map((material, idx) => {
              // Check if any failed sentences mention unresolved referents in this passage
              // An assembled-gate reason; a written check's free-text reason is not read for it
              const hasUnresolvedReferents = !written && card.evidence.draft?.claim_gate.failed_sentences.some(
                (failure) => failure.reason.toLowerCase().includes('unresolved referent')
              );
              
              return (
                <div key={idx} style={{
                  backgroundColor: '#f9f9f9',
                  padding: '8px',
                  borderRadius: '4px',
                  marginTop: '8px',
                  fontSize: '14px',
                  border: hasUnresolvedReferents ? '1px solid #ff9800' : 'none',
                }}>
                  <div style={{ fontStyle: 'italic', color: '#666' }}>{material.source}</div>
                  <div>{material.passage}</div>
                  <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
                    {material.relevance}
                  </div>
                  {hasUnresolvedReferents && (
                    <div style={{
                      marginTop: '8px',
                      padding: '6px',
                      backgroundColor: '#fff3e0',
                      borderRadius: '4px',
                      fontSize: '12px',
                      color: '#e65100',
                    }}>
                      ⚠️ Note: Some sentences in this material start with unresolved referents and cannot be used as claims.
                      See "Failed Sentences" below for details.
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {card.evidence.draft && (
          <div style={{ marginTop: '12px' }}>
            {/* Once a written draft was edited, this result is the model's draft's, not the current text's */}
            <strong>
              {written
                ? (edited ? "Product claim check of the model's draft (before your edit):" : 'Product claim check:')
                : 'Claim Gate:'}
            </strong>{' '}
            <span style={{ color: card.evidence.draft.claim_gate.passed ? 'green' : 'red' }}>
              {card.evidence.draft.claim_gate.passed ? 'PASSED' : 'FAILED'}
            </span>
            <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
              {card.evidence.draft.claim_gate.reasoning}
            </div>
            {card.evidence.draft.claim_gate.failed_sentences.length > 0 && (
              <div style={{ marginTop: '8px' }}>
                <div style={{ fontSize: '14px', fontWeight: 'bold', color: 'red' }}>
                  Failed Sentences:
                </div>
                {card.evidence.draft.claim_gate.failed_sentences.map((failure, idx) => {
                  // S7 (QA R14): Dedicated copy for abbreviation-ending reason
                  // These two rewrites explain assembled-gate reasons. A written check's
                  // reason is the checker's own words and is shown as it is (issue #12).
                  let displayReason = failure.reason;
                  if (!written && failure.reason.toLowerCase().includes('abbreviation')) {
                    displayReason = 'Sentence ends with an abbreviation and is not selectable. Abbreviations like Inc., Ltd., Dr., etc. at the end of a sentence cannot be used as claims.';
                  } else if (!written && failure.reason.toLowerCase().includes('unresolved referent')) {
                    displayReason = 'Sentence starts with an unresolved referent (it/this/that/these/they/its/such/the above) and cannot stand alone as a claim.';
                  }
                  
                  return (
                    <div key={idx} style={{
                      backgroundColor: '#ffebee',
                      padding: '8px',
                      borderRadius: '4px',
                      marginTop: '4px',
                      fontSize: '14px',
                    }}>
                      <div style={{ fontWeight: 'bold' }}>"{failure.sentence}"</div>
                      <div style={{ color: '#c62828', fontSize: '12px', marginTop: '4px' }}>
                        {displayReason}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Reply Plan (issue #5): shown above the draft */}
      <ReplyPlanPanel evidence={card.evidence} edited={edited} />

      {/* issue #12: written mode was asked for, but this watch has no product name */}
      {card.evidence.draft_mode_note && (
        <div role="note" style={{
          backgroundColor: '#fff8e1',
          border: '1px solid #ffe082',
          padding: '12px',
          borderRadius: '4px',
          marginBottom: '20px',
          fontSize: '14px',
        }}>
          {card.evidence.draft_mode_note}
        </div>
      )}

      {/* Written draft (issue #12): the check of each sentence replaces the segment display */}
      {written && card.evidence.draft && (
        <div style={{ marginBottom: '20px' }}>
          <h4>Product Claim Check</h4>
          <div style={{ fontSize: '14px', color: '#666', marginBottom: '8px' }}>
            The reply was written by the model. A sentence is checked as a product claim when it names
            the product
            {card.evidence.draft.written?.product_name && <> ("{card.evidence.draft.written.product_name}"
              {(card.evidence.draft.written.product_aliases ?? []).map((alias) => `, "${alias}"`)})</>}
            , uses we/our/us/ours, contains a number, makes a comparison or uses one of the watch's
            key features, or when a separate checker reads it as about the product. A product claim
            must be supported by sentences of your brief.
          </div>
          <div style={{ fontSize: '12px', color: '#666', marginBottom: '12px' }}>
            Key features from your watch settings:{' '}
            {(card.evidence.draft.written?.feature_terms ?? []).length > 0
              ? card.evidence.draft.written?.feature_terms.join(', ')
              : 'none (the watch was started without key features, so no sentence is checked for using one)'}
          </div>
          {edited ? (
            <div style={{ fontSize: '14px', color: '#666' }}>
              {card.edited_text_gate_state === 'not_checked'
                ? 'The draft was edited after this check, and the check of the edited text could not run. Approving it needs "Post anyway".'
                : 'The draft was edited after this check. The check of the edited text is shown below the draft.'}
            </div>
          ) : (
            <SentenceCheckList checks={card.evidence.draft.sentence_checks ?? []} />
          )}
          {/* ADR-0006 amendment 2026-11-24, step 1: the drafter's own list is provenance.
              It stays on the card after an edit, labelled as the model's draft's. */}
          {(card.evidence.draft.written?.drafter_hints ?? []).length > 0 && (
            <div data-testid="drafter-citations" style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
              <div style={{ marginBottom: '4px' }}>
                <strong>
                  {edited ? "The drafter's own citations, for the model's draft (before your edit)." : "The drafter's own citations."}
                </strong>{' '}
                The model that wrote the draft named these brief sentences as its support. This is a
                record only: the checker was not shown it, and it is not a verdict.
                {edited && ' It says nothing about the edited text.'}
              </div>
              {(card.evidence.draft.written?.drafter_hints ?? []).map((hint, idx) => (
                <div key={idx}>
                  "{hint.sentence}": {(hint.brief_sentence_ids ?? []).length > 0 ? hint.brief_sentence_ids.join(', ') : 'no brief sentence named'}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Segments with Tags */}
      {card.evidence.draft && card.evidence.draft.segments.length > 0 && (
        <div style={{ marginBottom: '20px' }}>
          <h4>Reply Segments</h4>
          <div style={{ fontSize: '14px', color: '#666', marginBottom: '8px' }}>
            Each segment is tagged as claim or non-claim. Claims show their supporting passage.
          </div>
          {card.evidence.draft.segments.map((segment, idx) => {
            const isClaim = segment.kind === 'claim';
            const matchedPassage = isClaim && segment.source_passage_id
              ? card.evidence.founder_materials.find(m => m.passage_id === segment.source_passage_id)
              : null;
            
            return (
              <div key={idx} style={{
                backgroundColor: isClaim ? '#e3f2fd' : '#f5f5f5',
                padding: '12px',
                borderRadius: '4px',
                marginBottom: '8px',
                border: `1px solid ${isClaim ? '#90caf9' : '#e0e0e0'}`,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '8px' }}>
                  <div style={{ flex: 1 }}>
                    <span style={{
                      display: 'inline-block',
                      padding: '2px 8px',
                      borderRadius: '4px',
                      fontSize: '12px',
                      fontWeight: 'bold',
                      backgroundColor: isClaim ? '#1976d2' : '#757575',
                      color: '#fff',
                      marginRight: '8px',
                    }}>
                      {segment.kind}
                    </span>
                    <span>{segment.text}</span>
                  </div>
                </div>
                {/* S7 (QA R14): Consistent !== null checks and render references */}
                {isClaim && segment.source_passage_id !== null && segment.sentence_index !== null && (
                  <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
                    <strong>Source:</strong> {segment.source_passage_id} sentence {segment.sentence_index}
                  </div>
                )}
                {!isClaim && segment.phrase_id !== null && (
                  <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
                    <strong>Phrase:</strong> {segment.phrase_id}
                  </div>
                )}
                {matchedPassage && (
                  <div style={{
                    backgroundColor: '#fff',
                    padding: '8px',
                    borderRadius: '4px',
                    marginTop: '8px',
                    fontSize: '12px',
                    borderLeft: '3px solid #1976d2',
                  }}>
                    <div style={{ fontWeight: 'bold', color: '#1976d2', marginBottom: '4px' }}>
                      Supporting Passage:
                    </div>
                    <div style={{ fontStyle: 'italic', color: '#666', marginBottom: '4px' }}>
                      {matchedPassage.source}
                    </div>
                    <div>{matchedPassage.passage}</div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Segments Detail (ADR-0006 Amendment: claims quoted with source_passage_id + sentence_index) */}
      {card.evidence.draft && card.evidence.draft.segments && card.evidence.draft.segments.length > 0 && (
        <div style={{ marginBottom: '20px' }}>
          <h4>Segments (Evidence Trail)</h4>
          <div style={{ fontSize: '14px', color: '#666', marginBottom: '12px' }}>
            Claims are quoted verbatim from founder materials (sentence_index). Non-claims come from a fixed phrase list (v2.1.0).
          </div>
          {card.evidence.draft.segments.map((segment, idx) => (
            <div key={idx} style={{
              backgroundColor: segment.kind === 'claim' ? '#e3f2fd' : '#f5f5f5',
              padding: '12px',
              borderRadius: '4px',
              marginBottom: '8px',
              border: `1px solid ${segment.kind === 'claim' ? '#2196f3' : '#9e9e9e'}`,
            }}>
              <div style={{ marginBottom: '8px' }}>
                <span style={{
                  display: 'inline-block',
                  padding: '2px 8px',
                  borderRadius: '4px',
                  fontSize: '12px',
                  fontWeight: 'bold',
                  backgroundColor: segment.kind === 'claim' ? '#1976d2' : '#757575',
                  color: '#fff',
                  marginRight: '8px',
                }}>
                  {segment.kind}
                </span>
                {/* S7 (QA R14): Render segment references in small muted text */}
                {segment.kind === 'claim' && segment.source_passage_id !== null && segment.sentence_index !== null && (
                  <span style={{ fontSize: '12px', color: '#666' }}>
                    from {segment.source_passage_id} sentence {segment.sentence_index}
                  </span>
                )}
                {segment.kind === 'non_claim' && segment.phrase_id !== null && (
                  <span style={{ fontSize: '12px', color: '#666' }}>
                    phrase: {segment.phrase_id}
                  </span>
                )}
              </div>
              <div style={{ marginBottom: '8px' }}>"{segment.text}"</div>
              {segment.kind === 'claim' && segment.source_passage_id !== null && (
                <div style={{ 
                  fontSize: '12px', 
                  color: '#0d47a1',
                  padding: '8px',
                  backgroundColor: '#fff',
                  borderRadius: '4px',
                  marginTop: '8px',
                }}>
                  <strong>Source:</strong> {
                    card.evidence.founder_materials
                      .find(m => m.passage_id === segment.source_passage_id)?.passage || 'N/A'
                  }
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Skipped: say why, and show the rejected draft as not for posting */}
      {card.status === 'skipped' && (
        <div style={{
          backgroundColor: '#f5f5f5',
          border: '1px solid #ccc',
          padding: '12px',
          borderRadius: '4px',
          marginBottom: '20px',
        }}>
          <strong>Skipped:</strong> {card.skip_reason || card.evidence.skip_reason || 'No reason recorded'}
          {card.evidence.draft && (
            <div style={{ marginTop: '8px', fontSize: '14px', color: '#666' }}>
              Rejected draft (not for posting): <em>{card.evidence.draft.text || '(empty)'}</em>
            </div>
          )}
        </div>
      )}

      {/* Draft Reply */}
      {card.evidence.draft && card.status !== 'skipped' && (
        <div style={{ marginBottom: '20px' }}>
          <h4>Draft Reply (Combined)</h4>
          {isEditing ? (
            <div>
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                style={{
                  width: '100%',
                  minHeight: '100px',
                  padding: '8px',
                  borderRadius: '4px',
                  border: '1px solid #ccc',
                  fontFamily: 'inherit',
                }}
              />
              <div
                style={{ fontSize: '12px', color: xLength(editText, card.product_url) > X_MAX_CHARS ? '#c62828' : '#666', marginTop: '4px' }}
              >
                {xLength(editText, card.product_url)}/{X_MAX_CHARS} characters, as X counts them (a link counts as {X_LINK_CHARS})
              </div>
              <button
                onClick={handleEdit}
                disabled={loading || xLength(editText, card.product_url) > X_MAX_CHARS}
                style={{ marginTop: '8px', marginRight: '8px' }}
              >
                Save Edit
              </button>
              <button
                onClick={() => {
                  setIsEditing(false);
                  setEditText(card.evidence.draft?.text || '');
                }}
                disabled={loading}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div>
              <div style={{
                backgroundColor: '#e8f5e9',
                padding: '12px',
                borderRadius: '4px',
                marginBottom: '8px',
                // A written draft or an edit can have line breaks; show them as they will be posted
                whiteSpace: 'pre-wrap',
              }}>
                {card.evidence.draft.text}
              </div>
              <button
                onClick={() => copyToClipboard(card.evidence.draft?.text || '')}
                style={{ fontSize: '14px' }}
              >
                Copy Draft
              </button>
              {card.evidence.draft.steers_applied.length > 0 && (
                <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
                  Steers applied: {card.evidence.draft.steers_applied.join(', ')}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Edited Text Gate Results (ADR-0006 Amendment E + QA R11 B2/C) */}
      {card.edited_text_gate_results && card.edited_text_gate_results.length > 0 && (
        <div style={{ marginBottom: '20px', borderTop: '1px solid #eee', paddingTop: '20px' }}>
          <h4>{written ? 'Check of the Edited Text' : 'Edited Text Gate Results'}</h4>
          
          {/* Gate State Badge */}
          {card.edited_text_gate_state && (
            <div style={{ marginBottom: '12px' }}>
              <span style={{
                display: 'inline-block',
                padding: '6px 12px',
                borderRadius: '4px',
                fontSize: '14px',
                fontWeight: 'bold',
                backgroundColor: 
                  card.edited_text_gate_state === 'passed' ? '#4caf50' :
                  card.edited_text_gate_state === 'failed' ? '#f44336' : '#ff9800',
                color: '#fff',
              }}>
                Gate State: {card.edited_text_gate_state.toUpperCase()}
              </span>
              {(card.edited_text_gate_state === 'failed' || card.edited_text_gate_state === 'not_checked') && (
                <div style={{ 
                  marginTop: '8px',
                  padding: '8px 12px',
                  backgroundColor: '#fff3e0',
                  borderRadius: '4px',
                  fontSize: '13px',
                  color: '#e65100',
                }}>
                  ⚠️ Approval requires "Post anyway" acknowledgement
                  {card.edited_text_gate_state === 'not_checked' && ' (gate check failed - retries exhausted)'}
                </div>
              )}
            </div>
          )}
          
          <div style={{ fontSize: '14px', color: '#666', marginBottom: '12px' }}>
            {written
              ? 'Each sentence of the edited text went through the same product-claim check as the draft.'
              : 'Each sentence from the edited text is re-verified against the strict claim gate.'}
          </div>
          {written && <SentenceCheckList checks={card.edited_text_gate_results} />}
          {!written && card.edited_text_gate_results.map((result, idx) => {
            const isPassed = result.passed;
            const isKept = result.kept_segment;
            
            return (
              <div key={idx} style={{
                backgroundColor: isPassed ? '#e8f5e9' : '#ffebee',
                padding: '12px',
                borderRadius: '4px',
                marginBottom: '8px',
                border: `1px solid ${isPassed ? '#4caf50' : '#f44336'}`,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '8px' }}>
                  <div style={{ flex: 1 }}>
                    <span style={{
                      display: 'inline-block',
                      padding: '2px 8px',
                      borderRadius: '4px',
                      fontSize: '12px',
                      fontWeight: 'bold',
                      backgroundColor: isPassed ? '#4caf50' : '#f44336',
                      color: '#fff',
                      marginRight: '8px',
                    }}>
                      {isPassed ? 'PASS' : 'FAIL'}
                    </span>
                    <span style={{
                      display: 'inline-block',
                      padding: '2px 8px',
                      borderRadius: '4px',
                      fontSize: '12px',
                      backgroundColor: result.kind === 'claim' ? '#1976d2' : '#757575',
                      color: '#fff',
                      marginRight: '8px',
                    }}>
                      {result.kind}
                    </span>
                    {isKept && (
                      <span style={{
                        display: 'inline-block',
                        padding: '2px 8px',
                        borderRadius: '4px',
                        fontSize: '12px',
                        backgroundColor: '#9c27b0',
                        color: '#fff',
                        marginRight: '8px',
                      }}>
                        verbatim
                      </span>
                    )}
                  </div>
                </div>
                <div style={{ marginBottom: '8px' }}>"{result.sentence}"</div>
                {!isPassed && result.reason && (
                  <div style={{ 
                    fontSize: '12px', 
                    color: '#c62828',
                    padding: '8px',
                    backgroundColor: '#fff',
                    borderRadius: '4px',
                  }}>
                    <strong>Reason:</strong> {result.reason}
                  </div>
                )}
              </div>
            );
          })}
          
          {card.post_anyway && card.post_anyway_failing_sentences && (
            <div style={{
              backgroundColor: '#fff3e0',
              padding: '12px',
              borderRadius: '4px',
              marginTop: '12px',
              border: '1px solid #ff9800',
            }}>
              <div style={{ fontWeight: 'bold', color: '#e65100', marginBottom: '8px' }}>
                ⚠️ Posted Anyway Override
              </div>
              <div style={{ fontSize: '14px', marginBottom: '8px' }}>
                This draft was approved with failing sentences using the "Post anyway" override.
              </div>
              <div style={{ fontSize: '12px', color: '#666' }}>
                Failing sentences at approval: {card.post_anyway_failing_sentences.length}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Retry notice (issue #20): the request can be sent again, nothing failed */}
      {notice && (
        <div role="status" style={{
          backgroundColor: '#e3f2fd',
          color: '#0d47a1',
          padding: '12px',
          borderRadius: '4px',
          marginBottom: '12px',
        }}>
          {notice}
        </div>
      )}

      {/* Error Display */}
      {/* issue #23: an error can quote the draft (a nudge rejection); none is
          shown on a removed card, also one that arrives after the removal */}
      {error && !textRemoved && (
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

      {/* Follow-up Replies */}
      {card.status === 'followup_due' && card.followup_replies && card.followup_replies.length > 0 && (
        <div style={{ marginBottom: '20px', borderTop: '1px solid #eee', paddingTop: '20px' }}>
          <h4>Follow-up Replies ({card.followup_replies.length})</h4>
          {card.followup_replies.map((reply: any, idx: number) => (
            <div key={idx} style={{
              backgroundColor: '#f9f9f9',
              padding: '12px',
              borderRadius: '4px',
              marginBottom: '8px',
            }}>
              <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>
                @{reply.author}
              </div>
              <div>{reply.text}</div>
              <a href={reply.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: '12px', marginTop: '4px', display: 'block' }}>
                View reply →
              </a>
            </div>
          ))}
        </div>
      )}

      {/* Actions */}
      {card.status === 'approved' && (
        <div style={{ borderTop: '1px solid #eee', paddingTop: '20px' }}>
          <h4>Confirm Posted Reply</h4>
          <div style={{ marginBottom: '12px' }}>
            <input
              type="text"
              placeholder="Paste URL of your posted reply"
              value={replyUrl}
              onChange={(e) => setReplyUrl(e.target.value)}
              style={{ width: '300px', padding: '8px', marginRight: '8px' }}
            />
            <button
              onClick={handleConfirmPosted}
              disabled={loading}
              style={{ backgroundColor: '#4caf50', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: '4px' }}
            >
              Confirm Posted
            </button>
          </div>
        </div>
      )}
      
      {card.status === 'pending' && (
        <div style={{ borderTop: '1px solid #eee', paddingTop: '20px' }}>
          <h4>Actions</h4>
          
          {/* Approve */}
          <div style={{ marginBottom: '12px' }}>
            <input
              type="text"
              placeholder="Paste URL of your posted reply"
              value={postedUrl}
              onChange={(e) => setPostedUrl(e.target.value)}
              style={{ width: '300px', padding: '8px', marginRight: '8px' }}
            />
            <button
              onClick={() => handleApprove(false)}
              disabled={loading}
              style={{ backgroundColor: '#4caf50', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: '4px' }}
            >
              Approve
            </button>
            
            {/* Post Anyway confirmation (ADR-0006 Amendment E + QA R11 B2/C) */}
            {showPostAnywayConfirm && (
              <div style={{
                marginTop: '12px',
                padding: '12px',
                backgroundColor: '#fff3e0',
                borderRadius: '4px',
                border: '1px solid #ff9800',
              }}>
                <div style={{ fontWeight: 'bold', color: '#e65100', marginBottom: '8px' }}>
                  ⚠️ Edited text requires acknowledgement
                </div>
                <div style={{ fontSize: '14px', marginBottom: '12px' }}>
                  {card.edited_text_gate_state === 'failed' && 
                    'The edited text contains claims that failed the claim gate. '
                  }
                  {card.edited_text_gate_state === 'not_checked' && 
                    'The gate check failed (retries exhausted). '
                  }
                  You can override this and post anyway by confirming below.
                </div>
                {failingSentences.length > 0 && (
                  <ul style={{ fontSize: '13px', marginTop: 0, marginBottom: '12px' }}>
                    {failingSentences.map((sentence, idx) => (
                      <li key={idx}>Failing: "{sentence}"</li>
                    ))}
                  </ul>
                )}
                <button
                  onClick={() => handleApprove(true)}
                  disabled={loading}
                  style={{
                    backgroundColor: '#ff9800',
                    color: '#fff',
                    border: 'none',
                    padding: '8px 16px',
                    borderRadius: '4px',
                    fontWeight: 'bold',
                  }}
                >
                  Post Anyway (Override)
                </button>
                <button
                  onClick={closePostAnyway}
                  disabled={loading}
                  style={{ marginLeft: '8px' }}
                >
                  Cancel
                </button>
              </div>
            )}
          </div>

          {/* Edit */}
          <div style={{ marginBottom: '12px' }}>
            <button
              onClick={() => {
                if (isEditing) {
                  // Clear request ID on abandonment
                  editRequestId.current = null;
                }
                setIsEditing(!isEditing);
              }}
              disabled={loading}
              style={{ marginRight: '8px' }}
            >
              {isEditing ? 'Cancel Edit' : 'Edit Draft'}
            </button>
          </div>

          {/* Nudge */}
          {!isNudging ? (
            <div style={{ marginBottom: '12px' }}>
              <button
                onClick={() => setIsNudging(true)}
                disabled={loading}
                style={{ marginRight: '8px' }}
              >
                Nudge (Redraft)
              </button>
            </div>
          ) : (
            <div style={{ marginBottom: '12px' }}>
              <input
                type="text"
                placeholder="e.g., 'Make it shorter' or 'More technical'"
                value={nudgeInstruction}
                onChange={(e) => setNudgeInstruction(e.target.value)}
                style={{ width: '300px', padding: '8px', marginRight: '8px' }}
              />
              <button onClick={handleNudge} disabled={loading}>
                Apply Nudge
              </button>
              <button
                onClick={() => {
                  // Clear request ID on abandonment
                  nudgeRequestId.current = null;
                  setIsNudging(false);
                  setNudgeInstruction('');
                }}
                disabled={loading}
                style={{ marginLeft: '8px' }}
              >
                Cancel
              </button>
            </div>
          )}

          {/* Skip */}
          <div style={{ marginBottom: '12px' }}>
            <input
              type="text"
              placeholder="Skip reason (optional)"
              value={skipReason}
              onChange={(e) => setSkipReason(e.target.value)}
              style={{ width: '300px', padding: '8px', marginRight: '8px' }}
            />
            <button
              onClick={handleSkip}
              disabled={loading}
              style={{ backgroundColor: '#f44336', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: '4px' }}
            >
              Skip
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * ReviewCard component - a collapsed card in the review queue (topic,
 * confidence, the post and the draft) that opens everything else in a
 * centered detail view (issue #50).
 */

import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Banner, Button, Dialog, Input, Tag, Textarea } from '@outpost/ds';
import type { ReviewCard as ReviewCardType } from '../api/client';
import { approveReview, skipReview, editReview, nudgeReview, confirmPostedReview, generateRequestId, PostRemovedError, ReviewApiError } from '../api/client';
import { ReplyFitPanel } from './ReplyFitPanel';
import { ReplyPlanPanel } from './ReplyPlanPanel';
import { SentenceCheckList } from './SentenceCheckList';
import { HighReachBadge, ReachCounts } from './ReachSummary';

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
  // issue #50: whether the detail view is open. Closing it only hides it:
  // every field above stays, so nothing typed is lost.
  const [open, setOpen] = useState(false);
  const closeDetail = useCallback(() => setOpen(false), []);
  // Focus goes back to the card that opened the view. Outpost's Dialog tries
  // this while it is still the open modal, when the rest of the page is inert
  // and focus() does nothing in a browser; this runs after it is gone.
  const openButtonRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (wasOpen.current && !open) openButtonRef.current?.focus();
    wasOpen.current = open;
  }, [open]);
  
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

  const draft = card.evidence.draft;
  const confidencePct = (card.evidence.confidence_score * 100).toFixed(0);
  const title = author ? `Review: ${author.username}` : 'Review';
  const skipped = card.status === 'skipped';
  const skipText = card.skip_reason || card.evidence.skip_reason || 'No reason recorded';
  const editLength = xLength(editText, card.evidence.product_url);
  // issue #50: the card's state lives here, not in the detail view, so
  // closing the view keeps an unsaved edit, a typed URL or a nudge. The
  // collapsed card says so, and says when a result is waiting to be read.
  const unsavedEdit = isEditing && editText !== (draftText ?? '');
  // The nudge field is shown only on a pending card
  const unsavedNudge = card.status === 'pending' && isNudging && nudgeInstruction.trim() !== '';
  const nudgeBlockedId = `${card.workflow_id}-nudge-blocked`;
  const skipDiscardsId = `${card.workflow_id}-skip-discards`;
  const approveBlockedId = `${card.workflow_id}-approve-blocked`;
  const needsAttention = showPostAnywayConfirm || notice != null || (error != null && !textRemoved);

  const detail = (
    <Dialog
      open
      onClose={closeDetail}
      title={title}
      dismissLabel="Close review"
      className="rq-detail"
      data-op-intensity="deck"
    >
      <div className="rq-detail__meta">
        <StatusBadge status={card.status} />
        <HighReachBadge card={card} />
        <span>Confidence <strong>{confidencePct}%</strong></span>
        <span>Topic <strong>{card.evidence.matched_topic}</strong></span>
      </div>

      <div className="rq-detail__grid">
        <div className="rq-detail__main">
          {/* Original Post */}
          <section className="rq-sec">
            <h4 className="rq-sec__title">Original Post</h4>
            <div className="rq-post">
              {author && (
                <div className="rq-post__author">
                  {author.name} (@{author.username})
                </div>
              )}
              {card.post.text_removed ? (
                <div className="rq-post__removed">
                  Post removed: it was deleted or is no longer visible on X, so its text, its author, and the research and edits quoting it were removed.
                </div>
              ) : (
                <div className="rq-post__text">{card.post.text}</div>
              )}
              <ReachCounts card={card} showFollowers={!!author} className="rq-post__counts" />
            </div>
            {postUrl && (
              <a href={postUrl} target="_blank" rel="noopener noreferrer" className="app-link">
                View on X →
              </a>
            )}
          </section>

          {/* Skipped: say why, and show the rejected draft as not for posting */}
          {skipped && (
            <section className="rq-sec">
              <div className="rq-callout">
                <strong>Skipped:</strong> {skipText}
                {draft && (
                  <div className="ev-note">
                    Rejected draft (not for posting): <em>{draft.text || '(empty)'}</em>
                  </div>
                )}
              </div>
            </section>
          )}

          {/* issue #12: written mode was asked for, but this watch has no product name */}
          {card.evidence.draft_mode_note && (
            <div role="note" className="rq-callout rq-callout--caution">
              {card.evidence.draft_mode_note}
            </div>
          )}

          {/* Draft Reply */}
          {draft && !skipped && (
            <section className="rq-sec">
              <h4 className="rq-sec__title">Draft Reply (Combined)</h4>
              {isEditing ? (
                <div>
                  <Textarea
                    label="Edited draft"
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    // While a request runs, text typed now would not be in it, and
                    // a successful save or nudge closes the box: no typing then
                    readOnly={loading}
                    rows={5}
                  />
                  <div className={editLength > X_MAX_CHARS ? 'rq-count rq-count--over' : 'rq-count'}>
                    {editLength}/{X_MAX_CHARS} characters, as X counts them (a link counts as {X_LINK_CHARS})
                  </div>
                  <div className="rq-row" style={{ marginTop: 'var(--op-space-3)' }}>
                    <Button
                      size="sm"
                      onClick={handleEdit}
                      disabled={loading || editLength > X_MAX_CHARS}
                    >
                      Save Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setIsEditing(false);
                        setEditText(card.evidence.draft?.text || '');
                      }}
                      disabled={loading}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <div>
                  {/* A written draft or an edit can have line breaks; show them as they will be posted */}
                  <div className="rq-draft">{draft.text}</div>
                  <Button size="sm" variant="ghost" onClick={() => copyToClipboard(card.evidence.draft?.text || '')}>
                    Copy Draft
                  </Button>
                  {draft.steers_applied.length > 0 && (
                    <div className="ev-note">
                      Steers applied: {draft.steers_applied.join(', ')}
                    </div>
                  )}
                </div>
              )}
            </section>
          )}

          {/* Retry notice (issue #20): the request can be sent again, nothing failed */}
          {notice && (
            <Banner tone="info" className="rq-feedback">{notice}</Banner>
          )}

          {/* Error Display */}
          {/* issue #23: an error can quote the draft (a nudge rejection); none is
              shown on a removed card, also one that arrives after the removal */}
          {error && !textRemoved && (
            <Banner tone="critical" className="rq-feedback">{error}</Banner>
          )}

          {/* Actions */}
          {card.status === 'approved' && (
            <section className="rq-sec rq-sec--rule" data-op-intensity="instrument">
              <h4 className="rq-sec__title">Confirm Posted Reply</h4>
              <div className="rq-row">
                <Input
                  label="Your posted reply's URL"
                  type="text"
                  placeholder="Paste URL of your posted reply"
                  value={replyUrl}
                  onChange={(e) => setReplyUrl(e.target.value)}
                  readOnly={loading}
                />
                <Button onClick={handleConfirmPosted} disabled={loading}>
                  Confirm Posted
                </Button>
              </div>
            </section>
          )}

          {card.status === 'pending' && (
            <section className="rq-sec rq-sec--rule" data-op-intensity="instrument">
              <h4 className="rq-sec__title">Actions</h4>
              <div className="rq-actions">
                {/* Approve */}
                <div>
                  <div className="rq-row">
                    <Input
                      label="Your posted reply's URL"
                      type="text"
                      placeholder="Paste URL of your posted reply"
                      value={postedUrl}
                      onChange={(e) => setPostedUrl(e.target.value)}
                      // Every field is read-only while a request runs: the
                      // request has the value it was sent with
                      readOnly={loading}
                    />
                    <Button
                      onClick={() => handleApprove(false)}
                      disabled={loading || unsavedEdit}
                      aria-describedby={unsavedEdit ? approveBlockedId : undefined}
                    >
                      Approve
                    </Button>
                  </div>
                  {/* Approve records the saved draft, not the text in the edit box */}
                  {unsavedEdit && (
                    <p id={approveBlockedId} className="ev-note ev-warn">
                      Approve records the saved draft. Save or cancel your edit first.
                    </p>
                  )}

                  {/* Post Anyway confirmation (ADR-0006 Amendment E + QA R11 B2/C) */}
                  {showPostAnywayConfirm && (
                    <div className="rq-callout rq-callout--caution">
                      <p className="rq-callout__title">
                        Edited text requires acknowledgement
                      </p>
                      <div>
                        {card.edited_text_gate_state === 'failed' &&
                          'The edited text contains claims that failed the claim gate. '
                        }
                        {card.edited_text_gate_state === 'not_checked' &&
                          'The gate check failed (retries exhausted). '
                        }
                        You can override this and post anyway by confirming below.
                      </div>
                      {failingSentences.length > 0 && (
                        <ul>
                          {failingSentences.map((sentence, idx) => (
                            <li key={idx}>Failing: "{sentence}"</li>
                          ))}
                        </ul>
                      )}
                      <div className="rq-row">
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => handleApprove(true)}
                          disabled={loading || unsavedEdit}
                          aria-describedby={unsavedEdit ? approveBlockedId : undefined}
                        >
                          Post Anyway (Override)
                        </Button>
                        <Button size="sm" variant="ghost" onClick={closePostAnyway} disabled={loading}>
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </div>

                {/* Edit and nudge */}
                <div className="rq-row">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      if (isEditing) {
                        // Clear request ID on abandonment
                        editRequestId.current = null;
                      }
                      setIsEditing(!isEditing);
                    }}
                    disabled={loading}
                  >
                    {isEditing ? 'Cancel Edit' : 'Edit Draft'}
                  </Button>
                  {!isNudging && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setIsNudging(true)}
                      disabled={loading || unsavedEdit}
                      aria-describedby={unsavedEdit ? nudgeBlockedId : undefined}
                    >
                      Nudge (Redraft)
                    </Button>
                  )}
                </div>
                {/* A nudge replaces the draft and the edit box's text */}
                {unsavedEdit && (
                  <p id={nudgeBlockedId} className="ev-note ev-warn" style={{ margin: 0 }}>
                    A nudge replaces the draft. Save or cancel your edit first.
                  </p>
                )}

                {isNudging && (
                  <div className="rq-row">
                    <Input
                      label="Nudge instruction"
                      type="text"
                      placeholder="e.g., 'Make it shorter' or 'More technical'"
                      value={nudgeInstruction}
                      onChange={(e) => setNudgeInstruction(e.target.value)}
                      // A successful nudge empties this field: no typing while it runs
                      readOnly={loading}
                    />
                    <Button
                      size="sm"
                      onClick={handleNudge}
                      disabled={loading || unsavedEdit}
                      aria-describedby={unsavedEdit ? nudgeBlockedId : undefined}
                    >
                      Apply Nudge
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        // Clear request ID on abandonment
                        nudgeRequestId.current = null;
                        setIsNudging(false);
                        setNudgeInstruction('');
                      }}
                      disabled={loading}
                    >
                      Cancel
                    </Button>
                  </div>
                )}

                {/* Skip */}
                <div className="rq-row">
                  <Input
                    label="Skip reason"
                    type="text"
                    placeholder="Skip reason (optional)"
                    value={skipReason}
                    onChange={(e) => setSkipReason(e.target.value)}
                    readOnly={loading}
                  />
                  <Button
                    variant="ghost"
                    onClick={handleSkip}
                    disabled={loading}
                    aria-describedby={unsavedEdit ? skipDiscardsId : undefined}
                  >
                    Skip
                  </Button>
                </div>
                {unsavedEdit && (
                  <p id={skipDiscardsId} className="ev-note" style={{ margin: 0 }}>
                    Skipping discards your unsaved edit.
                  </p>
                )}
              </div>
            </section>
          )}

          {/* Follow-up Replies */}
          {card.status === 'followup_due' && card.followup_replies && card.followup_replies.length > 0 && (
            <section className="rq-sec rq-sec--rule">
              <h4 className="rq-sec__title">Follow-up Replies ({card.followup_replies.length})</h4>
              {card.followup_replies.map((reply: any, idx: number) => (
                <div key={idx} className="ev-item">
                  <div className="rq-post__author">@{reply.author}</div>
                  <div className="ev-item__text">{reply.text}</div>
                  <a href={reply.url} target="_blank" rel="noopener noreferrer" className="app-link">
                    View reply →
                  </a>
                </div>
              ))}
            </section>
          )}
        </div>

        <div className="rq-detail__evidence">
          {/* Evidence Trail */}
          <section className="rq-sec">
            <h4 className="rq-sec__title">Evidence & Research</h4>
            <p style={{ marginTop: 0 }}>
              <strong>Why it fits:</strong> {card.evidence.why_it_fits}
            </p>

            {/* Issue #4: should-we-reply features and checks */}
            <ReplyFitPanel
              replyFit={card.evidence.reply_fit}
              confidenceInputs={card.evidence.confidence_inputs}
            />

            {card.evidence.founder_materials.length > 0 && (
              <div style={{ marginBottom: 'var(--op-space-4)' }}>
                <strong>Supporting Material:</strong>
                {!written && (
                  <div className="ev-note">
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
                    <div key={idx} className={hasUnresolvedReferents ? 'ev-item ev-item--flag' : 'ev-item'}>
                      <div className="ev-item__sub">{material.source}</div>
                      <div className="ev-item__text">{material.passage}</div>
                      <div className="ev-item__sub">{material.relevance}</div>
                      {hasUnresolvedReferents && (
                        <div className="ev-item__sub ev-warn">
                          Note: Some sentences in this material start with unresolved referents and cannot be used as claims.
                          See "Failed Sentences" below for details.
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {draft && (
              <div>
                {/* Once a written draft was edited, this result is the model's draft's, not the current text's */}
                <strong>
                  {written
                    ? (edited ? "Product claim check of the model's draft (before your edit):" : 'Product claim check:')
                    : 'Claim Gate:'}
                </strong>{' '}
                <span className={draft.claim_gate.passed ? 'ev-pass' : 'ev-fail'}>
                  {draft.claim_gate.passed ? 'PASSED' : 'FAILED'}
                </span>
                <div className="ev-note">{draft.claim_gate.reasoning}</div>
                {draft.claim_gate.failed_sentences.length > 0 && (
                  <div>
                    <div className="ev-fail">Failed Sentences:</div>
                    {draft.claim_gate.failed_sentences.map((failure, idx) => {
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
                        <div key={idx} className="ev-item ev-item--fail">
                          <div className="ev-item__text">"{failure.sentence}"</div>
                          <div className="ev-item__sub ev-error">{displayReason}</div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </section>

          {/* Reply Plan (issue #5) */}
          <ReplyPlanPanel evidence={card.evidence} edited={edited} />

          {/* Written draft (issue #12): the check of each sentence replaces the segment display */}
          {written && draft && (
            <section className="rq-sec">
              <h4 className="rq-sec__title">Product Claim Check</h4>
              <div className="ev-note">
                The reply was written by the model. A sentence is checked as a product claim when it names
                the product
                {draft.written?.product_name && <> ("{draft.written.product_name}"
                  {(draft.written.product_aliases ?? []).map((alias) => `, "${alias}"`)})</>}
                , uses we/our/us/ours, contains a number, makes a comparison or uses one of the watch's
                key features, or when a separate checker reads it as about the product. A product claim
                must be supported by sentences of your brief.
              </div>
              <div className="ev-note">
                Key features from your watch settings:{' '}
                {(draft.written?.feature_terms ?? []).length > 0
                  ? draft.written?.feature_terms.join(', ')
                  : 'none (the watch was started without key features, so no sentence is checked for using one)'}
              </div>
              {edited ? (
                <div className="ev-note">
                  {card.edited_text_gate_state === 'not_checked'
                    ? 'The draft was edited after this check, and the check of the edited text could not run. Approving it needs "Post anyway".'
                    : 'The draft was edited after this check. The check of the edited text is shown below the draft.'}
                </div>
              ) : (
                <SentenceCheckList checks={draft.sentence_checks ?? []} />
              )}
              {/* ADR-0006 amendment 2026-11-24, step 1: the drafter's own list is provenance.
                  It stays on the card after an edit, labelled as the model's draft's. */}
              {(draft.written?.drafter_hints ?? []).length > 0 && (
                <div data-testid="drafter-citations" className="ev-note">
                  <div style={{ marginBottom: 'var(--op-space-1)' }}>
                    <strong>
                      {edited ? "The drafter's own citations, for the model's draft (before your edit)." : "The drafter's own citations."}
                    </strong>{' '}
                    The model that wrote the draft named these brief sentences as its support. This is a
                    record only: the checker was not shown it, and it is not a verdict.
                    {edited && ' It says nothing about the edited text.'}
                  </div>
                  {(draft.written?.drafter_hints ?? []).map((hint, idx) => (
                    <div key={idx}>
                      "{hint.sentence}": {(hint.brief_sentence_ids ?? []).length > 0 ? hint.brief_sentence_ids.join(', ') : 'no brief sentence named'}
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}

          {/* Segments with Tags */}
          {draft && draft.segments.length > 0 && (
            <section className="rq-sec">
              <h4 className="rq-sec__title">Reply Segments</h4>
              <div className="ev-note">
                Each segment is tagged as claim or non-claim. Claims show their supporting passage.
              </div>
              {draft.segments.map((segment, idx) => {
                const isClaim = segment.kind === 'claim';
                const matchedPassage = isClaim && segment.source_passage_id
                  ? card.evidence.founder_materials.find(m => m.passage_id === segment.source_passage_id)
                  : null;

                return (
                  <div key={idx} className="ev-item">
                    <div className="ev-item__tags">
                      <Tag accent={isClaim ? 'oxide' : 'neutral'}>{segment.kind}</Tag>
                      <span className="ev-item__text">{segment.text}</span>
                    </div>
                    {/* S7 (QA R14): Consistent !== null checks and render references */}
                    {isClaim && segment.source_passage_id !== null && segment.sentence_index !== null && (
                      <div className="ev-item__sub">
                        <strong>Source:</strong> {segment.source_passage_id} sentence {segment.sentence_index}
                      </div>
                    )}
                    {!isClaim && segment.phrase_id !== null && (
                      <div className="ev-item__sub">
                        <strong>Phrase:</strong> {segment.phrase_id}
                      </div>
                    )}
                    {matchedPassage && (
                      <div className="ev-item__source">
                        <div className="app-label">Supporting Passage:</div>
                        <div className="ev-item__sub">{matchedPassage.source}</div>
                        <div>{matchedPassage.passage}</div>
                      </div>
                    )}
                  </div>
                );
              })}
            </section>
          )}

          {/* Segments Detail (ADR-0006 Amendment: claims quoted with source_passage_id + sentence_index) */}
          {draft && draft.segments && draft.segments.length > 0 && (
            <section className="rq-sec">
              <h4 className="rq-sec__title">Segments (Evidence Trail)</h4>
              <div className="ev-note">
                Claims are quoted verbatim from founder materials (sentence_index). Non-claims come from a fixed phrase list (v2.1.0).
              </div>
              {draft.segments.map((segment, idx) => (
                <div key={idx} className="ev-item">
                  <div className="ev-item__tags">
                    <Tag accent={segment.kind === 'claim' ? 'oxide' : 'neutral'}>{segment.kind}</Tag>
                    {/* S7 (QA R14): Render segment references in small muted text */}
                    {segment.kind === 'claim' && segment.source_passage_id !== null && segment.sentence_index !== null && (
                      <span className="ev-item__sub" style={{ marginTop: 0 }}>
                        from {segment.source_passage_id} sentence {segment.sentence_index}
                      </span>
                    )}
                    {segment.kind === 'non_claim' && segment.phrase_id !== null && (
                      <span className="ev-item__sub" style={{ marginTop: 0 }}>
                        phrase: {segment.phrase_id}
                      </span>
                    )}
                  </div>
                  <div className="ev-item__text">"{segment.text}"</div>
                  {segment.kind === 'claim' && segment.source_passage_id !== null && (
                    <div className="ev-item__source">
                      <strong>Source:</strong> {
                        card.evidence.founder_materials
                          .find(m => m.passage_id === segment.source_passage_id)?.passage || 'N/A'
                      }
                    </div>
                  )}
                </div>
              ))}
            </section>
          )}

          {/* Edited Text Gate Results (ADR-0006 Amendment E + QA R11 B2/C) */}
          {card.edited_text_gate_results && card.edited_text_gate_results.length > 0 && (
            <section className="rq-sec">
              <h4 className="rq-sec__title">{written ? 'Check of the Edited Text' : 'Edited Text Gate Results'}</h4>

              {/* Gate State Badge */}
              {card.edited_text_gate_state && (
                <div style={{ marginBottom: 'var(--op-space-3)' }}>
                  <Tag accent={
                    card.edited_text_gate_state === 'passed' ? 'moss' :
                    card.edited_text_gate_state === 'failed' ? 'oxide' : 'caution'
                  }>
                    Gate State: {card.edited_text_gate_state.toUpperCase()}
                  </Tag>
                  {(card.edited_text_gate_state === 'failed' || card.edited_text_gate_state === 'not_checked') && (
                    <div className="ev-note ev-warn">
                      Approval requires "Post anyway" acknowledgement
                      {card.edited_text_gate_state === 'not_checked' && ' (gate check failed - retries exhausted)'}
                    </div>
                  )}
                </div>
              )}

              <div className="ev-note">
                {written
                  ? 'Each sentence of the edited text went through the same product-claim check as the draft.'
                  : 'Each sentence from the edited text is re-verified against the strict claim gate.'}
              </div>
              {written && <SentenceCheckList checks={card.edited_text_gate_results} />}
              {!written && card.edited_text_gate_results.map((result, idx) => {
                const isPassed = result.passed;
                const isKept = result.kept_segment;

                return (
                  <div key={idx} className={isPassed ? 'ev-item ev-item--pass' : 'ev-item ev-item--fail'}>
                    <div className="ev-item__tags">
                      <Tag accent={isPassed ? 'moss' : 'oxide'}>{isPassed ? 'PASS' : 'FAIL'}</Tag>
                      <Tag accent="neutral">{result.kind}</Tag>
                      {isKept && <Tag accent="neutral">verbatim</Tag>}
                    </div>
                    <div className="ev-item__text">"{result.sentence}"</div>
                    {!isPassed && result.reason && (
                      <div className="ev-item__sub ev-error">
                        <strong>Reason:</strong> {result.reason}
                      </div>
                    )}
                  </div>
                );
              })}

              {card.post_anyway && card.post_anyway_failing_sentences && (
                <div className="rq-callout rq-callout--caution">
                  <p className="rq-callout__title">Posted Anyway Override</p>
                  <div>
                    This draft was approved with failing sentences using the "Post anyway" override.
                  </div>
                  <div className="ev-note">
                    Failing sentences at approval: {card.post_anyway_failing_sentences.length}
                  </div>
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </Dialog>
  );

  return (
    <article
      className={['op-panel', 'rq-card', skipped && 'rq-card--skipped', card.reach_highlighted && card.reach && 'rq-card--high-reach']
        .filter(Boolean)
        .join(' ')}
      data-testid="review-card"
      // issue #49: a card at or above the reach level
      data-high-reach={card.reach_highlighted && card.reach ? 'true' : undefined}
    >
      <div className="rq-card__top">
        <div className="rq-card__conf" aria-label={`Confidence ${confidencePct}%`}>
          {confidencePct}<small>%</small>
        </div>
        <div className="rq-card__ident">
          <p className="rq-card__topic">{card.evidence.matched_topic}</p>
          <h3 className="rq-card__title">
            <button
              ref={openButtonRef}
              type="button"
              className="rq-card__open"
              aria-haspopup="dialog"
              onClick={() => setOpen(true)}
            >
              {title}
            </button>
          </h3>
        </div>
        <div className="rq-card__flags">
          <StatusBadge status={card.status} />
          <HighReachBadge card={card} />
          {unsavedEdit && <Tag accent="caution">Unsaved edit</Tag>}
          {unsavedNudge && <Tag accent="caution">Unsent nudge</Tag>}
          {loading && <Tag accent="neutral">Working…</Tag>}
          {needsAttention && !loading && <Tag accent="oxide">Needs a look</Tag>}
        </div>
      </div>

      <div className="rq-card__block">
        <span className="app-label">Post</span>
        {card.post.text_removed ? (
          <p className="rq-card__post rq-card__post--removed">Post removed</p>
        ) : (
          <p className="rq-card__post rq-clamp">{card.post.text}</p>
        )}
      </div>

      {skipped ? (
        <div className="rq-card__block">
          <span className="app-label">Skipped</span>
          <p className="rq-card__post rq-clamp">{skipText}</p>
        </div>
      ) : (
        draft && (
          <div className="rq-card__block">
            <span className="app-label">{unsavedEdit ? 'Draft (your edit is not saved)' : 'Draft'}</span>
            <p className="rq-card__draft rq-clamp">{draft.text}</p>
          </div>
        )
      )}

      {/* issue #49: the engagement counts and the reach score */}
      <div className="rq-card__reach" data-slot="reach">
        <ReachCounts card={card} showFollowers={!!author} />
      </div>

      {/* In the top layer, outside the card, so the card's styles do not reach it */}
      {open && createPortal(detail, document.body)}
    </article>
  );
}

const STATUS_ACCENT: Record<string, 'neutral' | 'oxide' | 'caution' | 'moss'> = {
  pending: 'caution',
  approved: 'moss',
  approved_not_confirmed: 'caution',
  followup_due: 'moss',
  skipped: 'neutral',
  expired: 'neutral',
};

function StatusBadge({ status }: { status: string }) {
  return <Tag accent={STATUS_ACCENT[status] ?? 'neutral'}>{status}</Tag>;
}

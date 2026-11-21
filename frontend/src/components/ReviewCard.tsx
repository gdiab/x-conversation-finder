/**
 * ReviewCard component - shows a single review with evidence and draft.
 */

import { useState, useRef } from 'react';
import type { ReviewCard as ReviewCardType } from '../api/client';
import { approveReview, skipReview, editReview, nudgeReview, confirmPostedReview, generateRequestId } from '../api/client';

interface ReviewCardProps {
  card: ReviewCardType;
  onUpdate: () => void;
}

export function ReviewCard({ card, onUpdate }: ReviewCardProps) {
  const [postedUrl, setPostedUrl] = useState('');
  const [replyUrl, setReplyUrl] = useState('');
  const [skipReason, setSkipReason] = useState('');
  const [editText, setEditText] = useState(card.evidence.draft?.text || '');
  const [nudgeInstruction, setNudgeInstruction] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [isNudging, setIsNudging] = useState(false);
  const [showPostAnywayConfirm, setShowPostAnywayConfirm] = useState(false);
  
  // Stable request IDs - generated once per user action attempt, reused on retries
  const approveRequestId = useRef<string | null>(null);
  const skipRequestId = useRef<string | null>(null);
  const editRequestId = useRef<string | null>(null);
  const nudgeRequestId = useRef<string | null>(null);
  const confirmRequestId = useRef<string | null>(null);

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
    setShowPostAnywayConfirm(false);
    
    try {
      await approveReview(card.workflow_id, postedUrl, approveRequestId.current, postAnyway);
      approveRequestId.current = null; // Reset for next action
      onUpdate();
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to approve';
      // If error mentions post_anyway, show the confirm dialog
      if (errorMsg.toLowerCase().includes('post_anyway') && !postAnyway) {
        setShowPostAnywayConfirm(true);
      }
      setError(errorMsg);
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
    
    try {
      await editReview(card.workflow_id, editText, editRequestId.current);
      editRequestId.current = null; // Reset for next action
      setIsEditing(false);
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to edit');
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
    
    try {
      const newDraft = await nudgeReview(card.workflow_id, nudgeInstruction, nudgeRequestId.current);
      nudgeRequestId.current = null; // Reset for next action
      setEditText(newDraft);
      setNudgeInstruction('');
      setIsNudging(false);
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to nudge');
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
    
    try {
      await confirmPostedReview(card.workflow_id, replyUrl, confirmRequestId.current);
      confirmRequestId.current = null; // Reset for next action
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to confirm posted');
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
          <h3 style={{ margin: 0 }}>Review: {card.post.author.username}</h3>
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
          <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>
            {card.post.author.name} (@{card.post.author.username})
          </div>
          <div>{card.post.text}</div>
          <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
            {card.post.like_count} likes · {card.post.reply_count} replies · 
            {' '}{card.post.author.followers_count.toLocaleString()} followers
          </div>
        </div>
        <a href={card.post.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: '14px' }}>
          View on X →
        </a>
      </div>

      {/* Evidence Trail */}
      <div style={{ marginBottom: '20px' }}>
        <h4>Evidence & Research</h4>
        <div style={{ marginBottom: '12px' }}>
          <strong>Why it fits:</strong> {card.evidence.why_it_fits}
        </div>
        
        {card.evidence.founder_materials.length > 0 && (
          <div style={{ marginBottom: '12px' }}>
            <strong>Supporting Material:</strong>
            {card.evidence.founder_materials.map((material, idx) => (
              <div key={idx} style={{
                backgroundColor: '#f9f9f9',
                padding: '8px',
                borderRadius: '4px',
                marginTop: '8px',
                fontSize: '14px',
              }}>
                <div style={{ fontStyle: 'italic', color: '#666' }}>{material.source}</div>
                <div>{material.passage}</div>
                <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
                  {material.relevance}
                </div>
              </div>
            ))}
          </div>
        )}

        {card.evidence.draft && (
          <div style={{ marginTop: '12px' }}>
            <strong>Claim Gate:</strong>{' '}
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
                {card.evidence.draft.claim_gate.failed_sentences.map((failure, idx) => (
                  <div key={idx} style={{
                    backgroundColor: '#ffebee',
                    padding: '8px',
                    borderRadius: '4px',
                    marginTop: '4px',
                    fontSize: '14px',
                  }}>
                    <div style={{ fontWeight: 'bold' }}>"{failure.sentence}"</div>
                    <div style={{ color: '#c62828', fontSize: '12px', marginTop: '4px' }}>
                      {failure.reason}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

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
                {isClaim && segment.quote && (
                  <div style={{ fontSize: '12px', color: '#666', marginTop: '8px' }}>
                    <strong>Quote:</strong> "{segment.quote}"
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

      {/* Draft Reply */}
      {card.evidence.draft && (
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
                maxLength={280}
              />
              <div style={{ fontSize: '12px', color: '#666', marginTop: '4px' }}>
                {editText.length}/280 characters
              </div>
              <button
                onClick={handleEdit}
                disabled={loading}
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

      {/* Edited Text Gate Results (ADR-0006 Amendment E) */}
      {card.edited_text_gate_results && card.edited_text_gate_results.length > 0 && (
        <div style={{ marginBottom: '20px', borderTop: '1px solid #eee', paddingTop: '20px' }}>
          <h4>Edited Text Gate Results</h4>
          <div style={{ fontSize: '14px', color: '#666', marginBottom: '12px' }}>
            Each sentence from the edited text is re-verified against the strict claim gate.
          </div>
          {card.edited_text_gate_results.map((result, idx) => {
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

      {/* Error Display */}
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
            
            {/* Post Anyway confirmation (ADR-0006 Amendment E) */}
            {showPostAnywayConfirm && (
              <div style={{
                marginTop: '12px',
                padding: '12px',
                backgroundColor: '#fff3e0',
                borderRadius: '4px',
                border: '1px solid #ff9800',
              }}>
                <div style={{ fontWeight: 'bold', color: '#e65100', marginBottom: '8px' }}>
                  ⚠️ Edited text has failing sentences
                </div>
                <div style={{ fontSize: '14px', marginBottom: '12px' }}>
                  The edited text contains claims that failed the claim gate. 
                  You can override this and post anyway by confirming below.
                </div>
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
                  onClick={() => setShowPostAnywayConfirm(false)}
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

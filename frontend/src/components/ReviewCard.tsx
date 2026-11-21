/**
 * ReviewCard component - shows a single review with evidence and draft.
 */

import { useState } from 'react';
import type { ReviewCard as ReviewCardType } from '../api/client';
import { approveReview, skipReview, editReview, nudgeReview, confirmPostedReview } from '../api/client';

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

  const handleApprove = async () => {
    if (!postedUrl.trim()) {
      setError('Please enter the posted URL');
      return;
    }
    
    setLoading(true);
    setError(null);
    
    try {
      await approveReview(card.workflow_id, postedUrl);
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to approve');
    } finally {
      setLoading(false);
    }
  };

  const handleSkip = async () => {
    setLoading(true);
    setError(null);
    
    try {
      await skipReview(card.workflow_id, skipReason || 'Not relevant');
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
    
    setLoading(true);
    setError(null);
    
    try {
      await editReview(card.workflow_id, editText);
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
    
    setLoading(true);
    setError(null);
    
    try {
      const newDraft = await nudgeReview(card.workflow_id, nudgeInstruction);
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
    
    setLoading(true);
    setError(null);
    
    try {
      await confirmPostedReview(card.workflow_id, replyUrl);
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
            {card.evidence.draft.claim_gate.unsupported_claims.length > 0 && (
              <div style={{ fontSize: '14px', color: 'red', marginTop: '4px' }}>
                Unsupported: {card.evidence.draft.claim_gate.unsupported_claims.join(', ')}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Draft Reply */}
      {card.evidence.draft && (
        <div style={{ marginBottom: '20px' }}>
          <h4>Draft Reply</h4>
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
              onClick={handleApprove}
              disabled={loading}
              style={{ backgroundColor: '#4caf50', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: '4px' }}
            >
              Approve
            </button>
          </div>

          {/* Edit */}
          <div style={{ marginBottom: '12px' }}>
            <button
              onClick={() => setIsEditing(!isEditing)}
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

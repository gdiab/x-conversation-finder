/**
 * API client for the backend.
 */

export interface ReviewCard {
  workflow_id: string;
  post: {
    id: string;
    text: string;
    url: string;
    author: {
      username: string;
      name: string;
      followers_count: number;
    };
    created_at: string;
    like_count: number;
    reply_count: number;
  };
  evidence: {
    matched_topic: string;
    why_it_fits: string;
    confidence_score: number;
    confidence_inputs: Record<string, number>;
    founder_materials: Array<{
      source: string;
      passage: string;
      relevance: string;
    }>;
    draft: {
      text: string;
      claim_gate: {
        passed: boolean;
        claims_checked: string[];
        unsupported_claims: string[];
        supported_claims: Record<string, string>;
        reasoning: string;
      };
      steers_applied: string[];
    } | null;
    skip_reason: string | null;
  };
  status: string;
  created_at: string;
  updated_at: string;
  approved_by: string | null;
  posted_url: string | null;
  reply_post_id: string | null;
  skip_reason: string | null;
  followup_replies: Array<{
    id: string;
    text: string;
    author: string;
    url: string;
  }> | null;
}

export interface StartWatchRequest {
  account: string;
  topic: string;
  query: string;
  product_brief: string;
}

const API_BASE = '/api';

// Generate a unique request ID for idempotency
// Call this ONCE per user action attempt, not per retry
export function generateRequestId(): string {
  const timestamp = Date.now();
  const random = Math.random().toString(36).slice(2);
  return `req_${timestamp}_${random}`;
}

export async function startWatch(request: StartWatchRequest): Promise<{ workflow_id: string; message: string }> {
  const response = await fetch(`${API_BASE}/watch/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to start watch: ${response.statusText}`);
  }
  
  return response.json();
}

export async function listReviews(): Promise<{ cards: ReviewCard[]; count: number }> {
  const response = await fetch(`${API_BASE}/reviews`);
  
  if (!response.ok) {
    throw new Error(`Failed to list reviews: ${response.statusText}`);
  }
  
  return response.json();
}

export async function approveReview(workflowId: string, postedUrl: string, clientRequestId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      posted_url: postedUrl,
      client_request_id: clientRequestId,
    }),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to approve review: ${response.statusText}`);
  }
}

export async function skipReview(workflowId: string, reason: string, clientRequestId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/skip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reason,
      client_request_id: clientRequestId,
    }),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to skip review: ${response.statusText}`);
  }
}

export async function editReview(workflowId: string, newText: string, clientRequestId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      new_text: newText,
      client_request_id: clientRequestId,
    }),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to edit review: ${response.statusText}`);
  }
  
  const result = await response.json();
  return result.new_text;
}

export async function nudgeReview(workflowId: string, instruction: string, clientRequestId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/nudge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      instruction,
      client_request_id: clientRequestId,
    }),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to nudge review: ${response.statusText}`);
  }
  
  const result = await response.json();
  return result.new_draft;
}

export async function confirmPostedReview(workflowId: string, replyUrl: string, clientRequestId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/confirm_posted`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reply_url: replyUrl,
      client_request_id: clientRequestId,
    }),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to confirm posted: ${response.statusText}`);
  }
}

export async function getBudget(): Promise<any> {
  const response = await fetch(`${API_BASE}/budget`);
  
  if (!response.ok) {
    throw new Error(`Failed to get budget: ${response.statusText}`);
  }
  
  return response.json();
}

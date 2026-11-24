/**
 * API client for the backend.
 */

// R5-S5: Type status as union matching backend Literal
export type ReviewStatus = 
  | "pending" 
  | "approved" 
  | "approved_not_confirmed" 
  | "skipped" 
  | "expired" 
  | "followup_due";

// Issue #4: should-we-reply features. The first four fields are what the
// research model proposed; the rest are set by code (backend/gate/reply_fit.py).
export interface ReplyFit {
  post_kind: string | null;
  brief_problem_span: string | null;
  in_others_thread: boolean | null;
  is_vendor_post: boolean | null;
  span_check: 'ok' | 'missing' | 'not_in_post' | 'too_short' | 'off_brief' | 'not_checked';
  thread_source: 'metadata' | 'model' | 'missing' | 'not_checked';
  in_others_thread_used: boolean | null;
  materials_dropped?: number;
  rejected_span?: string | null;
  skip_rule: string | null;
  rule_version: string;
}

// Reply plan (issue #5): how to reply, decided before drafting.
export type ReplyType = "answer_question" | "add_observation" | "share_experience";

export interface ReplyPlan {
  // A rejected plan may carry an unknown type; the panel shows it as-is.
  reply_type: ReplyType | string;
  target_point: string;
  mention_product: boolean | null;
  mention_product_reason: string;
  model_used: string;
}

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
      passage_id?: string;
    }>;
    draft: {
      text: string;
      segments: Array<{
        kind: "claim" | "non_claim";
        text: string;
        phrase_id: string | null;
        source_passage_id: string | null;
        sentence_index: number | null;
      }>;
      claim_gate: {
        passed: boolean;
        segments: Array<{
          kind: "claim" | "non_claim";
          text: string;
          phrase_id: string | null;
          source_passage_id: string | null;
          sentence_index: number | null;
        }>;
        failed_sentences: Array<{
          sentence: string;
          reason: string;
        }>;
        reasoning: string;
        draft_text: string;
        // Legacy fields for backwards compatibility
        claims: Array<{
          text: string;
          cited_passage_ids: string[];
          quoted_spans: string[];
        }>;
        failed_claims: Array<{
          claim: {
            text: string;
            cited_passage_ids: string[];
            quoted_spans: string[];
          };
          reason: string;
        }>;
      };
      steers_applied: string[];
      token_count: number;
      model_used: string;
      input_tokens: number;
      output_tokens: number;
      // issue #5: reasons the draft does not follow the reply plan (empty = conforms)
      plan_conformance_failures?: string[];
    } | null;
    skip_reason: string | null;
    // Issue #4: should-we-reply features (proposed by research, checked by code).
    // Absent on cards stored before the feature existed.
    reply_fit?: ReplyFit | null;
    // Reply plan (issue #5). Optional: cards stored before the plan step lack it.
    reply_plan?: ReplyPlan | null;
    reply_plan_errors?: string[];
    reply_plan_attempts?: number;
  };
  status: ReviewStatus;
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
  // Edit re-gate fields (ADR-0006 Amendment E + QA R11 B2/C)
  edited_text_gate_results: Array<{
    sentence: string;
    passed: boolean;
    kind: "claim" | "non_claim";
    reason: string;
    kept_segment: boolean;
    phrase_id: string | null;
    source_passage_id: string | null;
    sentence_index: number | null;
  }> | null;
  edited_text_gate_state: "passed" | "failed" | "not_checked" | null;
  post_anyway: boolean | null;
  post_anyway_failing_sentences: string[] | null;
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

export async function approveReview(
  workflowId: string, 
  postedUrl: string, 
  clientRequestId: string,
  postAnyway: boolean = false
): Promise<void> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      posted_url: postedUrl,
      client_request_id: clientRequestId,
      post_anyway: postAnyway,
    }),
  });
  
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`Failed to approve review: ${response.statusText}. ${errorText}`);
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

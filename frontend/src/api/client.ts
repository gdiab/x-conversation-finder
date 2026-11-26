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

// Written drafts (issue #12; ADR-0006 amendment 2026-11-24): the model writes
// the reply, and code checks each sentence's product claims against the brief.
export type DraftMode = "assembled" | "written";

/** One sentence of the product brief, under the id the checker cites. */
export interface BriefSentence {
  id: string;
  text: string;
  // The heading or list lead-in the sentence stands under ("Not supported yet")
  context?: string;
}

/** A deterministic trigger that made code check a sentence as a product claim. */
export interface TriggerHit {
  trigger: "product_name" | "first_person_plural" | "number" | "comparison" | "feature_term" | string;
  // The matched text: the product name or alias, "our", a number, a key feature...
  match: string;
}

/** The check of one sentence of a written draft or of a founder edit of one. */
export interface SentenceCheck {
  sentence: string;
  passed: boolean;
  // "claim": checked as a product claim. "non_claim": not gated against the brief.
  kind: "claim" | "non_claim";
  reason: string;
  kept_segment: boolean;
  triggers: TriggerHit[];
  // The independent checker classified the sentence as about the product
  checker_flagged: boolean;
  verdict: "supported" | "unsupported" | "not_a_product_claim" | "not_checked" | string;
  // The brief sentences the checker cited and code confirmed exist
  supporting: BriefSentence[];
}

/** A per-sentence result of an edit: the assembled re-gate's, or (written mode) a SentenceCheck. */
export type EditGateResult = {
  sentence: string;
  passed: boolean;
  kind: "claim" | "non_claim";
  reason: string;
  kept_segment: boolean;
  phrase_id?: string | null;
  source_passage_id?: string | null;
  sentence_index?: number | null;
} & Partial<Pick<SentenceCheck, "triggers" | "checker_flagged" | "verdict" | "supporting">>;

export interface ReviewCard {
  workflow_id: string;
  post: {
    id: string;
    text: string;
    // issue #35: absent on a removed post (text_removed), with the author
    url?: string;
    author?: {
      username: string;
      name: string;
      followers_count: number;
    };
    created_at: string;
    like_count: number;
    reply_count: number;
    // issue #9: set when the post was deleted or is no longer visible on X and its text was removed
    text_removed?: boolean;
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
      // issue #12. Absent on cards stored before draft modes existed (assembled).
      mode?: DraftMode;
      // Written mode: the check of each sentence of the model's draft
      sentence_checks?: SentenceCheck[];
      written?: {
        product_name: string;
        product_aliases: string[];
        // The watch's "Key features" (a trigger). Never read from the brief.
        feature_terms: string[];
        // The brief sentences the drafter said support its own sentences:
        // provenance only, never sent to the checker. Emptied on removal.
        drafter_hints?: Array<{ sentence: string; brief_sentence_ids: string[] }>;
        checker_called?: boolean;
        checker_model?: string;
      } | null;
    } | null;
    skip_reason: string | null;
    // Issue #4: should-we-reply features (proposed by research, checked by code).
    // Absent on cards stored before the feature existed.
    reply_fit?: ReplyFit | null;
    // Reply plan (issue #5). Optional: cards stored before the plan step lack it.
    reply_plan?: ReplyPlan | null;
    reply_plan_errors?: string[];
    reply_plan_attempts?: number;
    // A plan-stage skip rule that fired on a valid plan (e.g. "needs_written_drafts")
    reply_plan_skip_rule?: string | null;
    // issue #12: the draft mode this Review was planned under, and why written
    // mode was asked for but not used (the watch has no product name)
    draft_mode?: DraftMode | null;
    draft_mode_note?: string | null;
    // issue #41: the watch's product URL (absent for a watch started without one)
    product_url?: string | null;
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
  edited_text_gate_results: EditGateResult[] | null;
  edited_text_gate_state: "passed" | "failed" | "not_checked" | null;
  post_anyway: boolean | null;
  post_anyway_failing_sentences: string[] | null;
}

export interface StartWatchRequest {
  account: string;
  topic: string;
  query: string;
  product_brief: string;
  // issue #12: the form's "Product name" (required), "Also called" and
  // "Key features" (both optional). Written drafts check any reply sentence
  // that uses one of these names or features against the brief.
  product_name: string;
  product_aliases: string[];
  key_features: string[];
  // issue #41: the form's required "Product URL" (https). Code appends it to
  // drafts that name the product.
  product_url: string;
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
    // The API explains a refused request in "detail", and for a field it
    // refused (a missing product name, too many aliases) in "errors"
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string' && body.detail) detail = body.detail;
      // Each entry: {loc: ["body", "product_aliases"], msg: "Value error, At most 10 aliases", ...}
      const reasons = (Array.isArray(body?.errors) ? body.errors : [])
        .map((e: { loc?: unknown; msg?: unknown }) => {
          if (typeof e?.msg !== 'string') return '';
          const field = Array.isArray(e.loc) ? e.loc.filter((part) => typeof part === 'string' && part !== 'body').pop() : null;
          const msg = e.msg.replace(/^Value error, /, '');
          return field ? `${field}: ${msg}` : msg;
        })
        .filter(Boolean);
      if (reasons.length > 0) detail = reasons.join('; ');
    } catch {
      // Not JSON: keep the status text
    }
    throw new Error(`Failed to start watch: ${detail}`);
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
  postAnyway: boolean,
  // The draft the founder saw (its text and edited-text gate state), required
  // by the API: the server refuses (409 draft_changed) if the current draft
  // or its gate result differs, so an approve never applies to an unseen draft
  shown: { text: string; gateState: string | null },
): Promise<void> {
  const response = await fetch(`${API_BASE}/reviews/${workflowId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      posted_url: postedUrl,
      client_request_id: clientRequestId,
      post_anyway: postAnyway,
      draft_text: shown.text,
      draft_gate_state: shown.gateState,
    }),
  });
  
  if (!response.ok) {
    throw await reviewError(response, 'approve review');
  }
}

/**
 * Error codes of the review endpoints (issue #15, backend/api.py REJECTION_CODES).
 * Switch on these, never on the message text.
 */
export type ReviewErrorCode =
  | 'post_anyway_required' // edited text failed the re-gate; approve again with post_anyway
  | 'not_ready' // the card is not stored yet (issue #20); try again in a moment
  | 'busy' // another edit, nudge or approval is running; try again after it
  | 'post_removed' // the target post is gone (approve: the review is skipped); reload the card
  | 'draft_changed' // approve named a draft that is no longer current; reload the card
  | 'invalid_state'
  | 'invalid_request'
  | 'rejected' // the workflow refused it (e.g. a nudged draft failed the gate)
  | 'not_found'
  | 'server_error';

/** A failed review request, with the API's code and message (issue #15). */
export class ReviewApiError extends Error {
  readonly status: number;
  readonly code: ReviewErrorCode | null;
  readonly detail: string;
  /** For post_anyway_required: the edited sentences that failed the gate. */
  readonly failingSentences: string[];

  constructor(message: string, status: number, code: ReviewErrorCode | null, detail: string, failingSentences: string[] = []) {
    super(message);
    this.name = 'ReviewApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.failingSentences = failingSentences;
  }

  /** Not a failure: the request can be sent again shortly (not_ready, busy). */
  get retryLater(): boolean {
    return this.code === 'not_ready' || this.code === 'busy';
  }
}

/**
 * The target post is deleted or no longer visible on X (issue #9). On approve
 * the review is now skipped; on a nudge another Review of the post recorded
 * the removal, so the card's text is gone and it is not redrafted.
 */
export class PostRemovedError extends ReviewApiError {
  constructor(detail: string, status: number = 409) {
    super(
      `Post removed: the post was deleted or is no longer visible on X. ${detail}`,
      status,
      'post_removed',
      detail,
    );
    this.name = 'PostRemovedError';
  }
}

/** Build the error for a failed review request from its JSON body {code, detail, ...}. */
async function reviewError(response: Response, action: string): Promise<ReviewApiError> {
  let body: { code?: unknown; detail?: unknown; failing_sentences?: unknown } = {};
  try {
    body = await response.json();
  } catch {
    // Not JSON (a proxy error page, an empty body): fall back to the status text
  }
  const code = typeof body.code === 'string' ? (body.code as ReviewErrorCode) : null;
  const detail = typeof body.detail === 'string' && body.detail ? body.detail : response.statusText;
  if (code === 'post_removed') {
    return new PostRemovedError(detail, response.status);
  }
  const failing = Array.isArray(body.failing_sentences)
    ? body.failing_sentences.filter((s): s is string => typeof s === 'string')
    : [];
  return new ReviewApiError(`Failed to ${action}: ${detail}`, response.status, code, detail, failing);
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
    throw await reviewError(response, 'skip review');
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
    throw await reviewError(response, 'edit review');
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
    throw await reviewError(response, 'nudge review');
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
    throw await reviewError(response, 'confirm posted');
  }
}

export async function getBudget(): Promise<any> {
  const response = await fetch(`${API_BASE}/budget`);
  
  if (!response.ok) {
    throw new Error(`Failed to get budget: ${response.statusText}`);
  }
  
  return response.json();
}

// --- Brief suggestions from the product page (issue #42) ---

/** One suggested brief sentence, its quote from the page (checked word for word by code) and why it helps. */
export interface BriefSuggestion {
  sentence: string;
  quote: string;
  why: string;
}

/** A brief sentence the page contradicts or does not mention: information, not an error. */
export interface BriefFlag {
  brief_sentence_id: string;
  brief_sentence: string;
  /** The heading or lead-in it stands under ("Not supported yet"), or "". */
  brief_sentence_context: string;
  kind: 'contradicted' | 'not_mentioned';
  quote: string;
  note: string;
}

export interface BriefSuggestionsResult {
  source: 'github_readme' | 'web_page';
  page_truncated: boolean;
  suggestions: BriefSuggestion[];
  flags: BriefFlag[];
  dropped_quote_not_on_page: number;
  dropped_other: number;
  cost_usd: number;
  model: string;
}

/** The worst-case cost of one suggestion call, shown before the click. */
export interface BriefSuggestionCost {
  max_cost_usd: number;
  max_input_tokens: number;
  max_output_tokens: number;
  model: string;
  max_page_bytes: number;
  max_suggestions: number;
}

/** A failed suggestion request, with the API's code (url_invalid, url_refused, fetch_failed, ...) and message. */
export class BriefSuggestionError extends Error {
  readonly code: string | null;
  constructor(message: string, code: string | null) {
    super(message);
    this.name = 'BriefSuggestionError';
    this.code = code;
  }
}

export async function getBriefSuggestionCost(): Promise<BriefSuggestionCost> {
  const response = await fetch(`${API_BASE}/brief/suggestions/cost`);
  if (!response.ok) {
    throw new Error(`Failed to get the suggestion cost: ${response.statusText}`);
  }
  return response.json();
}

export async function suggestBriefAdditions(request: {
  product_url: string;
  product_brief: string;
  product_name: string;
}): Promise<BriefSuggestionsResult> {
  const response = await fetch(`${API_BASE}/brief/suggestions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    let code: string | null = null;
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body?.code === 'string') code = body.code;
      if (typeof body?.detail === 'string' && body.detail) detail = body.detail;
    } catch {
      // Not JSON (a proxy error page): keep the status text
    }
    throw new BriefSuggestionError(detail, code);
  }
  return response.json();
}

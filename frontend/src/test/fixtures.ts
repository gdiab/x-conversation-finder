/**
 * Synthetic review-card fixtures, built from the types in api/client.ts.
 *
 * The repo is public: every name, handle, URL and sentence here is invented.
 * Do not paste real X posts or handles into tests.
 */

import type { ReplyFit, ReplyPlan, ReviewCard } from '../api/client';

type Evidence = ReviewCard['evidence'];
type Draft = NonNullable<Evidence['draft']>;

export function makeDraft(overrides: Partial<Draft> = {}): Draft {
  return {
    text: 'Synthetic draft sentence one. Synthetic draft sentence two.',
    segments: [],
    claim_gate: {
      passed: true,
      segments: [],
      failed_sentences: [],
      reasoning: 'Every sentence matched a passage or a fixed phrase.',
      draft_text: 'Synthetic draft sentence one. Synthetic draft sentence two.',
      claims: [],
      failed_claims: [],
    },
    steers_applied: [],
    token_count: 20,
    model_used: 'fake-model',
    input_tokens: 100,
    output_tokens: 20,
    ...overrides,
  };
}

export function makeReplyFit(overrides: Partial<ReplyFit> = {}): ReplyFit {
  return {
    post_kind: 'question',
    brief_problem_span: 'slow onboarding for new teammates',
    in_others_thread: false,
    is_vendor_post: false,
    span_check: 'ok',
    thread_source: 'metadata',
    in_others_thread_used: false,
    skip_rule: null,
    rule_version: 'test-rule-v1',
    ...overrides,
  };
}

export function makeReplyPlan(overrides: Partial<ReplyPlan> = {}): ReplyPlan {
  return {
    reply_type: 'add_observation',
    target_point: 'onboarding takes weeks',
    mention_product: true,
    mention_product_reason: 'the post describes the problem the brief solves',
    model_used: 'fake-model',
    ...overrides,
  };
}

export function makeEvidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    matched_topic: 'team onboarding',
    why_it_fits: 'The author asks how to speed up onboarding.',
    confidence_score: 0.8,
    confidence_inputs: { topic_match: 0.9, engagement: 0.5 },
    founder_materials: [],
    draft: makeDraft(),
    skip_reason: null,
    ...overrides,
  };
}

let counter = 0;

export function makeCard(overrides: Partial<ReviewCard> = {}): ReviewCard {
  counter += 1;
  const n = counter;
  return {
    workflow_id: `review-wf-${n}`,
    post: {
      id: `post-${n}`,
      text: `Synthetic post body number ${n}.`,
      url: `https://x.example/author${n}/status/${n}`,
      author: {
        username: `author${n}`,
        name: `Author Number ${n}`,
        followers_count: 1200,
      },
      created_at: '2026-01-01T00:00:00Z',
      like_count: 3,
      reply_count: 1,
    },
    evidence: makeEvidence(),
    status: 'pending',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    approved_by: null,
    posted_url: null,
    reply_post_id: null,
    skip_reason: null,
    followup_replies: null,
    edited_text_gate_results: null,
    edited_text_gate_state: null,
    post_anyway: null,
    post_anyway_failing_sentences: null,
    ...overrides,
  };
}

/** A card whose author has a known handle, so tests can find it by name. */
export function makeCardBy(username: string, overrides: Partial<ReviewCard> = {}): ReviewCard {
  const card = makeCard(overrides);
  return {
    ...card,
    post: { ...card.post, author: { ...card.post.author, username, name: `${username} (synthetic)` } },
  };
}

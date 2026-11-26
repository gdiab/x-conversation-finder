/**
 * Synthetic review-card fixtures, built from the types in api/client.ts.
 *
 * The repo is public: every name, handle, URL and sentence here is invented.
 * Do not paste real X posts or handles into tests.
 */

import type { ReplyFit, ReplyPlan, ReviewCard, SentenceCheck } from '../api/client';

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

/** One sentence of a written draft's check (issue #12). Defaults: not about the product, passed. */
export function makeSentenceCheck(overrides: Partial<SentenceCheck> = {}): SentenceCheck {
  return {
    sentence: 'That is a hard thing to get right.',
    passed: true,
    kind: 'non_claim',
    reason: '',
    kept_segment: false,
    triggers: [],
    checker_flagged: false,
    verdict: 'not_a_product_claim',
    supporting: [],
    ...overrides,
  };
}

/** A supported product claim of the synthetic product "Acme Relay". */
export function makeSupportedClaim(overrides: Partial<SentenceCheck> = {}): SentenceCheck {
  return makeSentenceCheck({
    sentence: 'Acme Relay can retry a failed delivery up to five times.',
    kind: 'claim',
    verdict: 'supported',
    triggers: [
      { trigger: 'product_name', match: 'Acme Relay' },
      { trigger: 'number', match: 'five' },
    ],
    supporting: [
      { id: 'b2', text: 'Relay can retry a failed delivery up to five times.', context: 'Acme Relay' },
    ],
    ...overrides,
  });
}

/** A written draft (issue #12): free text, no segments, a check per sentence. */
export function makeWrittenDraft(overrides: Partial<Draft> = {}): Draft {
  const checks = [
    makeSentenceCheck(),
    makeSupportedClaim(),
    makeSentenceCheck({ sentence: 'What does your queue do today?' }),
  ];
  return makeDraft({
    mode: 'written',
    text: checks.map((c) => c.sentence).join(' '),
    segments: [],
    sentence_checks: checks,
    written: {
      product_name: 'Acme Relay',
      product_aliases: ['Relay'],
      feature_terms: ['Signed deliveries'],
      drafter_hints: [],
      checker_called: true,
      checker_model: 'fake-checker',
    },
    claim_gate: {
      passed: true,
      segments: [],
      failed_sentences: [],
      reasoning: '3 sentence(s) checked: 1 product claim(s) supported by cited brief sentences, 2 not about the product',
      draft_text: '',
      claims: [],
      failed_claims: [],
    },
    ...overrides,
  });
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
    post: { ...card.post, author: { ...card.post.author!, username, name: `${username} (synthetic)` } },
  };
}

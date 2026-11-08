/**
 * The backend's screen contexts, as TypeScript. Mirrors `alibi/app/UI.md`
 * (S4's template contract), which S4 serves as JSON on
 * `Accept: application/json`. Field names and enums are copied exactly; if a
 * shape changes there, change it here and the fixtures' tests will say where.
 */

export interface Money {
  minor: number;
  currency: string;
}

export type Trust = 'issuer_verified' | 'bank_observed' | 'self_supplied';
export type ClaimStatus = 'supported' | 'contradicted' | 'unresolved';
export type LinkState = 'linked' | 'ambiguous' | 'unmatched';

// ---------------------------------------------------------------- passport --

export interface PassportHome {
  holder: { name: string; subject_ref: string };
  connections: {
    connection_id: string;
    name: string;
    kind: 'merchant' | 'bank' | 'import';
    status: 'connected' | 'syncing' | 'error' | 'disconnected';
    trust: Trust;
    last_synced_at: string;
    record_count: number;
    href: string;
  }[];
  transactions: {
    txn_id: string;
    revision: number;
    booked_at: string;
    description: string;
    amount: Money;
    direction: 'credit' | 'debit';
    link_state: LinkState;
    linked_count: number;
    href: string;
  }[];
  evidence_status: {
    linked: number;
    ambiguous: number;
    unmatched: number;
    total: number;
    last_checked_at: string;
    superseded_sources: number;
  };
  alerts: {
    alert_id: string;
    kind: string;
    severity: 'info' | 'warning' | 'high';
    title: string;
    created_at: string;
    status: 'open' | 'resolved';
    href: string;
  }[];
  rules: {
    rule_id: string;
    text: string;
    status: 'active' | 'held' | 'rejected';
    summary: string;
    href: string;
  }[];
  requests: {
    request_id: string;
    recipient: string;
    question_count: number;
    received_at: string;
    expires_at: string;
    status: RequestStatus;
    href: string;
  }[];
  sharing_history: {
    package_id: string;
    request_id: string;
    recipient: string;
    sent_at: string;
    expires_at: string;
    item_count: number;
    status: 'delivered' | 'acknowledged' | 'superseded' | 'expired';
    href: string;
  }[];
}

export type RequestStatus =
  'new' | 'investigating' | 'draft' | 'approved' | 'sent' | 'refused' | 'expired';

// ----------------------------------------------------------------- request --

export interface SourceRef {
  record_id: string;
  revision: number;
  trust: Trust;
  href: string;
  /** Index into this step's `lookups` when the record came from a scoped lookup (UI.md); absent or null otherwise. */
  via_lookup?: number | null;
}

export interface CalcTerm {
  label: string;
  sign: '+' | '-';
  amount: Money;
  source_ids: string[];
}

export interface TraceStep {
  step_id: string;
  question_id: string;
  question: string;
  claim: { claim_id: string; text: string; status: ClaimStatus };
  sources: SourceRef[];
  lookups: { tool: string; args_summary: string; result_summary: string; at: string }[];
  calculation: {
    expression: string;
    result: Money;
    computed_by: string;
    matches: boolean;
    /** UI.md: the signed lines behind the expression; empty or absent renders no table. */
    terms?: CalcTerm[];
  } | null;
  status: ClaimStatus;
  status_reason: string;
  at: string;
}

export interface Approval {
  status: 'none' | 'pending' | 'approved' | 'invalidated';
  recipient: string;
  audience: string;
  action: string;
  digest: string;
  policy_version: string;
  valid_from: string;
  valid_until: string;
  approved_at: string | null;
  invalidated_reason: string | null;
}

export interface RequestDetail {
  request: {
    request_id: string;
    parent_request_id: string | null;
    issuer: string;
    audience: string;
    subject_ref: string;
    received_at: string;
    expires_at: string;
    /** UI.md has a period on every request; an RFC scoped by `txn_ids` alone has none, so it may be null. */
    period: { from: string; to: string } | null;
    txn_ids: string[];
    questions: { question_id: string; text: string }[];
    status: RequestStatus;
    signature: { verified: boolean; key_id: string };
    href: string;
  };
  investigation: {
    status: 'not_started' | 'running' | 'complete' | 'failed';
    can_run: boolean;
    run_action: string;
    started_at: string | null;
    completed_at: string | null;
    budget: { calls_used: number; calls_cap: number };
    roles: { name: string; status: string; calls: number; note: string }[];
    failure_reason: string | null;
  };
  trace: TraceStep[];
  review: {
    claims: {
      claim_id: string;
      question_id: string;
      question: string;
      status: ClaimStatus;
      rationale: string;
      href: string;
    }[];
    included: {
      item_id: string;
      question_id: string;
      label: string;
      kind: string;
      record_id: string;
      revision: number;
      trust: Trust;
      reason: string;
      removable: boolean;
      remove_action: string | null;
    }[];
    excluded: { item_id: string; label: string; reason: string; rule_id: string | null }[];
    generalised: {
      item_id: string;
      label: string;
      original_summary: string;
      generalised_as: string;
      rule_id: string;
    }[];
    gaps: { question_id: string; description: string; reason_code: string }[];
    counts: { included: number; excluded: number; generalised: number; gaps: number };
    approval: Approval;
    preview_href: string;
  };
}

export type { Refusal } from '@alibi/ui/api';

// ---------------------------------------------------------------- dispatch --

/** What approving (which also sends) returns: `requests/dispatch.html`'s context in UI.md. */
export interface DispatchResult {
  outcome: 'success' | 'refused';
  package_id: string;
  request_id: string;
  recipient: string;
  attempted_at: string;
  receipt: {
    receipt_id: string;
    package_digest: string;
    received_at: string;
    status: string;
    signer_key_id: string;
    expires_at: string;
  } | null;
  refusal: {
    code: string;
    reason: string;
    checked_at: string;
    expected_digest: string;
    actual_digest: string;
  } | null;
  retry_action: string | null;
  review_href: string;
  history_href: string;
}

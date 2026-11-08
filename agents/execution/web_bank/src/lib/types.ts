/**
 * The analyst screens' JSON shapes: `alibi/app/UI.md` (analyst/inbox.html, analyst/package.html)
 * plus the fields pinned since in shared/CONTRACTS.md (Trust: attestation, redacted; RFC scope:
 * txn_ids; Disposition) and the settlement `timing` block. The backend serves these as JSON on
 * `Accept: application/json`; the built-in case 1 data uses the same shapes.
 */

export interface Money {
  minor: number;
  currency: string;
}

export interface Refusal {
  code: string;
  reason: string;
}

export interface Recipient {
  id: string;
  name: string;
  analyst_name: string;
}

export type AuthStatus =
  | 'valid'
  | 'invalid'
  | 'expired'
  | 'wrong_audience'
  | 'replayed'
  | 'preview'
  | (string & {});

export type ClaimStatus = 'supported' | 'contradicted' | 'unresolved';
export type Trust = 'issuer_verified' | 'bank_observed' | 'self_supplied';
export type Decision = 'clear' | 'escalate' | 'request_more';

export interface InboxPackage {
  package_id: string;
  request_id: string;
  received_at: string;
  expires_at: string;
  subject_ref_masked: string;
  authentication: AuthStatus;
  /** Absent when the backend's inbox does not carry per-package claims; open the package instead. */
  claim_counts?: { supported: number; contradicted: number; unresolved: number };
  disposition: Decision | null;
  href: string;
}

export interface InboxRequest {
  request_id: string;
  parent_request_id: string | null;
  sent_at: string;
  status: string;
  question_count: number;
}

export interface AnalystInbox {
  recipient: Recipient;
  packages: InboxPackage[];
  requests_sent: InboxRequest[];
}

export interface Check {
  name: string;
  result: string;
  detail: string;
}

export interface Authentication {
  status: AuthStatus;
  received_at: string;
  verified_at: string;
  package_digest: string;
  signer_key_id: string;
  checks: Check[];
  note: string;
  /** Verifier reasons, verbatim, when the package was rejected. */
  reasons?: string[];
}

export interface SourceTrust {
  attachment_id: string;
  record_id: string;
  revision: number;
  issuer: string;
  trust: Trust;
  attestation?: 'issuer_signature' | 'package_signer' | 'none';
  redacted?: boolean;
  signature_check: string;
  summary?: string;
  detail: string;
}

export interface Evidence {
  attachment_id: string;
  record_id: string;
  revision: number;
  trust: Trust;
  attestation?: 'issuer_signature' | 'package_signer' | 'none';
  redacted?: boolean;
  summary: string;
  href: string;
}

export interface Timing {
  payout_ref: string;
  released_to_balance_at: string;
  bank_settled_at: string;
  credit_booked_at: string;
  settled_before_booking: boolean;
}

export interface QuestionEvidence {
  question_id: string;
  question: string;
  claim: { status: ClaimStatus; rationale: string };
  evidence: Evidence[];
  calculation: null | { expression: string; result: Money; computed_by: string; matches: boolean };
  gap: string | null;
  timing?: Timing | null;
}

export interface ReconLine {
  label: string;
  kind: string;
  sign: '+' | '-';
  amount: Money;
  source_ids: string[];
}

export interface Reconciliation {
  title: string;
  lines: ReconLine[];
  expression?: string;
  result: Money | null;
  compared_to: { label: string; amount: Money } | null;
  difference: Money | null;
  matches: boolean | null;
  computed_by: string;
  computed_at?: string;
}

export interface CurrentDisposition {
  disposition: Decision;
  analyst_name: string;
  recorded_at: string;
  note: string;
}

export interface AnalystPackage {
  recipient: Recipient;
  package: {
    package_id: string;
    request_id: string;
    recipient: string;
    subject_ref_masked: string;
    period: { from: string; to: string } | null;
    txn_ids?: string[];
    period_display_only?: boolean;
    issued_at: string;
    expires_at: string;
  };
  authentication: Authentication;
  source_trust: SourceTrust[];
  claim_status: { question_id: string; status: ClaimStatus; summary: string }[];
  evidence_by_question: QuestionEvidence[];
  reconciliation: Reconciliation;
  contradictions: {
    claim_id: string;
    question_id: string;
    description: string;
    sources: { record_id: string; revision: number; trust: Trust }[];
  }[];
  gaps: { question_id: string; description: string; reason_code: string }[];
  disposition_form: {
    action: string;
    options: { value: Decision; label: string }[];
    analyst_name: string;
    current: CurrentDisposition | null;
  };
  followup_form: {
    action: string;
    parent_request_id: string;
    subject_ref_masked: string;
    period: { from: string; to: string } | null;
    txn_ids?: string[];
    question_prefill: string;
    consent_note: string;
  };
}

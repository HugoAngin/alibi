/**
 * The live mock bank (alibi/bank/service.py) as the console's screens. The bank answers in its own
 * JSON (verifier authentication, sources and claims, plus package metadata); this file checks that
 * shape and converts it to the console's AnalystPackage / AnalystInbox, so the screens are the same
 * for live data and the built-in preview. Samples of the real responses, generated from case 1, are
 * in src/data/live_samples/ and drive tests/bankApi.test.ts.
 *
 * Routes (lead, 06:07Z): GET /inbox, GET /packages/{id}, POST /packages/{id}/disposition, with the
 * recipient's bearer token. Through the public router they sit under /bank; BANK_API_ORIGIN points
 * straight at the service (e.g. http://127.0.0.1:8102) with no prefix.
 */

import { SIM_ORIGIN } from '@alibi/ui/sim/backend';
import { isSimulated } from '@alibi/ui/sim/context';

import type {
  AnalystInbox,
  AnalystPackage,
  ClaimStatus,
  CurrentDisposition,
  Decision,
  Evidence,
  InboxPackage,
  QuestionEvidence,
  SourceTrust,
  Trust,
} from './types';

// ------------------------------------------------------------------ bank JSON

interface BankRef {
  ref_id: string;
  ref_type: string;
  revision: number;
}

interface BankSource {
  attachment_id: string;
  ref: BankRef;
  form?: string;
  claimed_trust: string;
  attestation?: string;
  lower_trust?: boolean;
  signature_valid?: boolean;
  note?: string;
}

interface BankBodyClaim {
  question_id: string;
  status: string;
  statement?: string;
  rationale?: string;
  gap?: string | null;
  evidence_refs?: BankRef[];
}

interface BankDisposition {
  decision: string;
  reason: string;
  analyst: string;
  decided_at: string;
}

export interface BankPackage {
  status: 'verified' | 'rejected';
  receipt: { package_id: string; package_sha256: string; received_at: string; reasons?: string[] };
  rejection?: { reasons?: string[] };
  authentication?: { valid: boolean; kid?: string; audience?: string; reasons?: string[] };
  sources?: BankSource[];
  claims?: { question_id: string; status: string; calculation_valid?: boolean | null }[];
  package?: {
    package_id: string;
    issued_at?: string;
    body: {
      request_id?: string;
      audience?: string;
      scope?: { txn_ids?: string[]; period?: { from: string; to: string } };
      questions?: { question_id: string; text: string }[];
      claims?: BankBodyClaim[];
      expires_at?: string;
      attachments?: { attachment_id: string; issuer_id?: string; source_trust?: string }[];
    };
  };
  dispositions?: BankDisposition[];
  follow_ups?: string[];
}

export interface BankInbox {
  recipient: string;
  packages: {
    package_id: string;
    request_id: string | null;
    received_at: string;
    status: string;
    expires_at?: string;
    disposition: string | null;
  }[];
  requests_sent: {
    request_id: string;
    parent_request_id: string | null;
    issued_at: string;
    question_ids?: string[];
    answered_by?: string[];
  }[];
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}

export function isBankPackage(v: unknown): v is BankPackage {
  if (!isObject(v) || (v.status !== 'verified' && v.status !== 'rejected') || !isObject(v.receipt))
    return false;
  if (v.status === 'rejected') return true;
  return (
    isObject(v.authentication) &&
    Array.isArray(v.sources) &&
    Array.isArray(v.claims) &&
    isObject(v.package)
  );
}

export function isBankInbox(v: unknown): v is BankInbox {
  return (
    isObject(v) &&
    typeof v.recipient === 'string' &&
    Array.isArray(v.packages) &&
    Array.isArray(v.requests_sent)
  );
}

// ------------------------------------------------------------------ routes

export interface BankTarget {
  origin: string | undefined;
  prefix: string;
}

/**
 * BANK_API_ORIGIN (direct, no prefix) wins; otherwise the public router's /bank prefix. On a
 * simulated site, the recording.
 */
export function bankTarget(
  bankOrigin: string | undefined,
  routerOrigin: string | undefined,
): BankTarget {
  // A simulated site answers the bank service's own routes from the recorded demo (@alibi/ui/sim).
  if (isSimulated()) return { origin: SIM_ORIGIN, prefix: '' };
  return bankOrigin
    ? { origin: bankOrigin, prefix: '' }
    : { origin: routerOrigin, prefix: '/bank' };
}

export const BANK_ROUTES = {
  inbox: (t: BankTarget) => `${t.prefix}/inbox`,
  package: (t: BankTarget, id: string) => `${t.prefix}/packages/${encodeURIComponent(id)}`,
  disposition: (t: BankTarget, id: string) =>
    `${t.prefix}/packages/${encodeURIComponent(id)}/disposition`,
  /** Issue a request for context; the body names one of this bank's case requests by ID. */
  rfc: (t: BankTarget) => `${t.prefix}/rfc`,
};

// ------------------------------------------------------------------ conversion

const TRUSTS = new Set(['issuer_verified', 'bank_observed', 'self_supplied']);
const CLAIMS = new Set(['supported', 'contradicted', 'unresolved']);
const DECISIONS = new Set(['clear', 'escalate', 'request_more']);

const trustOf = (t: string | undefined): Trust =>
  TRUSTS.has(t ?? '') ? (t as Trust) : 'self_supplied';
const claimOf = (s: string | undefined): ClaimStatus =>
  CLAIMS.has(s ?? '') ? (s as ClaimStatus) : 'unresolved';
const refKey = (r: BankRef) => `${r.ref_type}:${r.ref_id}@${r.revision}`;

export interface Who {
  id: string;
  name: string;
  analyst_name: string;
}

function current(d: BankDisposition[] | undefined): CurrentDisposition | null {
  const last = d && d.length > 0 ? d[d.length - 1] : undefined;
  if (!last || !DECISIONS.has(last.decision)) return null;
  return {
    disposition: last.decision as Decision,
    analyst_name: last.analyst,
    recorded_at: last.decided_at,
    note: last.reason,
  };
}

export function toAnalystPackage(bank: BankPackage, who: Who, target: BankTarget): AnalystPackage {
  const id = bank.receipt.package_id;
  const action = BANK_ROUTES.disposition(target, id);
  const empty = {
    source_trust: [],
    claim_status: [],
    evidence_by_question: [],
    reconciliation: {
      title: 'Not shown: the package was rejected.',
      lines: [],
      result: null,
      compared_to: null,
      difference: null,
      matches: null,
      computed_by: 'verifier',
    },
    contradictions: [],
    gaps: [],
  };
  const options = [...DECISIONS].map((value) => ({ value: value as Decision, label: value }));

  if (bank.status === 'rejected') {
    return {
      recipient: who,
      package: {
        package_id: id,
        request_id: '—',
        recipient: who.name,
        subject_ref_masked: '—',
        period: null,
        txn_ids: [],
        issued_at: '—',
        expires_at: '—',
      },
      authentication: {
        status: 'invalid',
        reasons: bank.rejection?.reasons ?? bank.receipt.reasons ?? [],
        received_at: bank.receipt.received_at,
        verified_at: bank.receipt.received_at,
        package_digest: `sha256:${bank.receipt.package_sha256}`,
        signer_key_id: '—',
        checks: [],
        note: 'Rejected by the bank’s verifier. The package’s contents were not stored.',
      },
      ...empty,
      disposition_form: {
        action: '/preview/disabled',
        options,
        analyst_name: who.analyst_name,
        current: null,
      },
      followup_form: {
        action: '/preview/disabled',
        parent_request_id: '—',
        subject_ref_masked: '—',
        period: null,
        txn_ids: [],
        question_prefill: '',
        consent_note: '',
      },
    };
  }

  const body = bank.package?.body ?? {};
  const attachments = new Map((body.attachments ?? []).map((a) => [a.attachment_id, a]));
  const sources: SourceTrust[] = (bank.sources ?? []).map((s) => ({
    attachment_id: s.attachment_id,
    record_id: s.ref.ref_id,
    revision: s.ref.revision,
    issuer:
      s.ref.ref_type === 'txn' ? 'Bank feed' : (attachments.get(s.attachment_id)?.issuer_id ?? '—'),
    trust: trustOf(s.claimed_trust),
    attestation:
      s.attestation === 'issuer_signature' || s.attestation === 'package_signer'
        ? s.attestation
        : 'none',
    redacted: s.form === 'redacted_extract',
    signature_check:
      s.signature_valid === true ? 'pass' : s.signature_valid === false ? 'fail' : 'none',
    detail: s.note ?? '',
  }));
  const byRef = new Map((bank.sources ?? []).map((s, i) => [refKey(s.ref), sources[i]!]));
  const verified = new Map((bank.claims ?? []).map((c) => [c.question_id, c]));
  const bodyClaims = new Map((body.claims ?? []).map((c) => [c.question_id, c]));
  const questions =
    body.questions ??
    (body.claims ?? []).map((c) => ({ question_id: c.question_id, text: c.question_id }));

  const evidence_by_question: QuestionEvidence[] = questions.map((q) => {
    const claim = bodyClaims.get(q.question_id);
    const evidence: Evidence[] = (claim?.evidence_refs ?? []).flatMap((r) => {
      const s = byRef.get(refKey(r));
      return s
        ? [
            {
              attachment_id: s.attachment_id,
              record_id: s.record_id,
              revision: s.revision,
              trust: s.trust,
              attestation: s.attestation,
              redacted: s.redacted,
              summary: s.detail,
              href: '#',
            },
          ]
        : [];
    });
    return {
      question_id: q.question_id,
      question: q.text,
      claim: {
        status: claimOf(verified.get(q.question_id)?.status ?? claim?.status),
        rationale: claim?.rationale ?? claim?.statement ?? '',
      },
      evidence,
      // The bank reports only whether each calculation checked out; that feeds the reconciliation
      // line below rather than inventing a total here.
      calculation: null,
      gap: claim?.gap ?? null,
      timing: null,
    };
  });

  const checked = [...verified.values()]
    .map((c) => c.calculation_valid)
    .filter((v): v is boolean => typeof v === 'boolean');
  const scope = body.scope ?? {};
  return {
    recipient: who,
    package: {
      package_id: id,
      request_id: body.request_id ?? '—',
      recipient: who.name,
      subject_ref_masked: '—',
      period: scope.period ?? null,
      txn_ids: scope.txn_ids ?? [],
      issued_at: bank.package?.issued_at ?? '—',
      expires_at: body.expires_at ?? '—',
    },
    authentication: {
      status: bank.authentication?.valid ? 'valid' : 'invalid',
      reasons: bank.authentication?.reasons ?? [],
      received_at: bank.receipt.received_at,
      verified_at: bank.receipt.received_at,
      package_digest: `sha256:${bank.receipt.package_sha256}`,
      signer_key_id: bank.authentication?.kid ?? '—',
      checks: [],
      note: 'Verified by the bank against its pinned trust registry. This says nothing about whether a claim is true.',
    },
    source_trust: sources,
    claim_status: evidence_by_question.map((q) => ({
      question_id: q.question_id,
      status: q.claim.status,
      summary: bodyClaims.get(q.question_id)?.statement ?? q.claim.rationale,
    })),
    evidence_by_question,
    reconciliation: {
      title:
        checked.length > 0
          ? 'Calculations in the claims, recomputed by the bank’s verifier.'
          : 'No calculation in this package.',
      lines: [],
      result: null,
      compared_to: null,
      difference: null,
      matches: checked.length > 0 ? checked.every(Boolean) : null,
      computed_by: 'verifier',
    },
    contradictions: evidence_by_question
      .filter((q) => q.claim.status === 'contradicted')
      .map((q) => ({
        claim_id: q.question_id,
        question_id: q.question_id,
        description: q.claim.rationale,
        sources: [],
      })),
    gaps: evidence_by_question
      .filter((q) => q.gap)
      .map((q) => ({
        question_id: q.question_id,
        description: q.gap ?? '',
        reason_code: 'evidence_unavailable',
      })),
    disposition_form: {
      action,
      options,
      analyst_name: who.analyst_name,
      current: current(bank.dispositions),
    },
    followup_form: {
      action,
      parent_request_id: body.request_id ?? '—',
      subject_ref_masked: '—',
      period: scope.period ?? null,
      txn_ids: scope.txn_ids ?? [],
      question_prefill: '',
      consent_note:
        'A follow-up is a new signed request with its own ID; the customer approves any new disclosure separately.',
    },
  };
}

export function toAnalystInbox(bank: BankInbox, who: Who): AnalystInbox {
  const packages: InboxPackage[] = bank.packages.map((p) => ({
    package_id: p.package_id,
    request_id: p.request_id ?? '—',
    received_at: p.received_at,
    expires_at: p.expires_at ?? '—',
    subject_ref_masked: '—',
    authentication: p.status === 'verified' ? 'valid' : 'invalid',
    disposition: DECISIONS.has(p.disposition ?? '') ? (p.disposition as Decision) : null,
    href: `/bank/packages/${encodeURIComponent(p.package_id)}/`,
  }));
  return {
    recipient: who,
    packages,
    requests_sent: bank.requests_sent.map((r) => ({
      request_id: r.request_id,
      parent_request_id: r.parent_request_id,
      sent_at: r.issued_at,
      status: (r.answered_by ?? []).length > 0 ? 'answered' : 'awaiting_answer',
      question_count: (r.question_ids ?? []).length,
    })),
  };
}

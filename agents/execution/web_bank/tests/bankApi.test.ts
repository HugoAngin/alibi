/**
 * The live bank's JSON, converted for the console. The samples in src/data/live_samples/ are real
 * responses of alibi/bank/service.py for case 1 (rules mode): a verified package for rfc-0001-01, a
 * tampered copy the bank rejected, the package after a request_more, and the disposition_final
 * refusal. Regenerate with the script in agents/execution/web_bank/README.md.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { bankTarget, isBankInbox, isBankPackage, toAnalystInbox, toAnalystPackage } from '../src/lib/bankApi';
import { authVerdict, followupScope, isFinal, showsEvidence } from '../src/lib/view';

const sample = (name: string) =>
  JSON.parse(readFileSync(new URL(`../src/data/live_samples/${name}.json`, import.meta.url), 'utf8'));
const who = { id: 'bank_a', name: 'Bank A', analyst_name: 'J. Okafor' };
const target = bankTarget('http://127.0.0.1:8102', undefined);

describe('bank JSON shape checks', () => {
  it('accepts the real responses and rejects junk', () => {
    expect(isBankPackage(sample('package'))).toBe(true);
    expect(isBankPackage(sample('package_rejected'))).toBe(true);
    expect(isBankInbox(sample('inbox'))).toBe(true);
    expect(isBankPackage({ status: 'verified', receipt: {} })).toBe(false);
    expect(isBankInbox({ packages: [] })).toBe(false);
  });
});

describe('a verified package', () => {
  const pkg = toAnalystPackage(sample('package'), who, target);

  it('shows evidence and keeps the verifier’s three answers apart', () => {
    expect(authVerdict(pkg.authentication.status)).toBe('verified');
    expect(showsEvidence(pkg)).toBe(true);
    expect(pkg.authentication.signer_key_id).toBe('alibi_demo');
    expect(pkg.authentication.package_digest).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it('labels the bank feed extract as package-attested, lower trust', () => {
    const credit = pkg.source_trust.find((s) => s.record_id === 'txn-0001-004');
    expect(credit).toMatchObject({ trust: 'bank_observed', redacted: true, attestation: 'package_signer' });
  });

  it('answers each question with the claim status, rationale and gap from the package', () => {
    expect(pkg.evidence_by_question.map((q) => q.question_id)).toEqual(['q.source_of_funds', 'q.breakdown']);
    expect(pkg.claim_status.every((c) => ['supported', 'contradicted', 'unresolved'].includes(c.status))).toBe(true);
    expect(pkg.gaps.length).toBeGreaterThan(0);
    expect(pkg.evidence_by_question.every((q) => q.calculation === null)).toBe(true);
  });

  it('acts on the bank’s own disposition route and inherits the parent transaction', () => {
    expect(pkg.disposition_form.action).toBe(`/packages/${pkg.package.package_id}/disposition`);
    expect(followupScope(pkg)).toEqual({ kind: 'txn_ids', txn_ids: ['txn-0001-004'] });
    expect(pkg.disposition_form.current).toBeNull();
  });
});

describe('a rejected package', () => {
  const pkg = toAnalystPackage(sample('package_rejected'), who, target);

  it('shows only the verifier’s reasons, verbatim, and no evidence', () => {
    expect(showsEvidence(pkg)).toBe(false);
    expect(pkg.authentication.reasons).toEqual([
      'body_sha256 does not match the package body',
      'Alibi package signature: signature does not verify',
    ]);
    expect(pkg.source_trust).toEqual([]);
    expect(pkg.evidence_by_question).toEqual([]);
  });
});

describe('dispositions', () => {
  it('shows a recorded request_more as current but not final', () => {
    const pkg = toAnalystPackage(sample('package_after_disposition'), who, target);
    expect(pkg.disposition_form.current).toMatchObject({ disposition: 'request_more', analyst_name: 'analyst_a1' });
    expect(isFinal(pkg.disposition_form.current)).toBe(false);
  });

  it('the bank’s final-disposition refusal has the {code, reason} shape the console shows verbatim', () => {
    const refusal = sample('refusal_disposition_final');
    expect(refusal.code).toBe('disposition_final');
    expect(typeof refusal.reason).toBe('string');
  });
});

describe('the inbox', () => {
  const inbox = toAnalystInbox(sample('inbox'), who);
  it('marks rejected packages and keeps claim counts out of the list', () => {
    expect(inbox.packages.map((p) => p.authentication)).toEqual(['valid', 'invalid']);
    expect(inbox.packages.every((p) => p.claim_counts === undefined)).toBe(true);
    expect(inbox.requests_sent[0]).toMatchObject({ request_id: 'rfc-0001-01', status: 'answered', question_count: 2 });
  });
});

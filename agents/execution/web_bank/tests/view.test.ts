import { describe, expect, it } from 'vitest';

import { isInbox, isPackage, safeReturn } from '../src/lib/screens';
import type { AnalystPackage } from '../src/lib/types';
import {
  actionEnabled,
  attestationNote,
  authVerdict,
  followupScope,
  isFinal,
  rejectionReasons,
  showsClaimCounts,
  showsEvidence,
} from '../src/lib/view';
import { inboxFixture, packageFixture } from '../src/data/analyst';

const preview = packageFixture('preview-rfc-0001-01') as AnalystPackage;

function withAuth(status: string, extra: Partial<AnalystPackage['authentication']> = {}): AnalystPackage {
  return { ...preview, authentication: { ...preview.authentication, status, ...extra } };
}

describe('authentication verdicts (SP-25 to SP-32)', () => {
  it('only valid, replayed and preview packages show evidence', () => {
    expect(authVerdict('valid')).toBe('verified');
    expect(authVerdict('replayed')).toBe('duplicate');
    expect(authVerdict('preview')).toBe('preview');
    for (const bad of ['invalid', 'expired', 'wrong_audience', 'anything_new']) {
      expect(authVerdict(bad)).toBe('rejected');
      expect(showsEvidence(withAuth(bad))).toBe(false);
      expect(showsClaimCounts({ authentication: bad })).toBe(false);
    }
    expect(showsEvidence(withAuth('valid'))).toBe(true);
  });

  it('shows the verifier reasons verbatim when it gives them', () => {
    const reasons = ['body_sha256 does not match the package body', 'Alibi package signature: signature does not verify'];
    expect(rejectionReasons(withAuth('invalid', { reasons }))).toEqual(reasons);
    expect(rejectionReasons(withAuth('expired'))).toEqual(['Package authentication: expired']);
  });
});

describe('decisions', () => {
  const at = '2026-11-08T10:00:00Z';
  it('clear and escalate are final; request_more is not', () => {
    expect(isFinal(null)).toBe(false);
    expect(isFinal({ disposition: 'clear', analyst_name: 'a', recorded_at: at, note: 'x' })).toBe(true);
    expect(isFinal({ disposition: 'escalate', analyst_name: 'a', recorded_at: at, note: 'x' })).toBe(true);
    expect(isFinal({ disposition: 'request_more', analyst_name: 'a', recorded_at: at, note: 'x' })).toBe(false);
  });

  it('preview actions are disabled; real backend paths are enabled', () => {
    expect(actionEnabled(preview.disposition_form.action)).toBe(false);
    expect(actionEnabled('/bank/bank_a/packages/pkg-1/disposition')).toBe(true);
    expect(actionEnabled('https://evil.example/x')).toBe(false);
    expect(actionEnabled('//evil.example/x')).toBe(false);
  });
});

describe('trust labels', () => {
  it('labels redacted extracts as attested by Alibi, never as issuer-signed', () => {
    expect(attestationNote({ attestation: 'package_signer', redacted: true })).toBe('Redacted extract, attested by Alibi');
    expect(attestationNote({ attestation: 'none', redacted: true })).toBe('Redacted extract, attested by Alibi');
    expect(attestationNote({ attestation: 'issuer_signature', redacted: false })).toBe('Issuer signature verified');
    expect(attestationNote({ attestation: 'none', redacted: false })).toBeNull();
  });
});

describe('follow-up scope (RFC scope row)', () => {
  it('inherits the parent transaction for case 1', () => {
    expect(followupScope(preview)).toEqual({ kind: 'txn_ids', txn_ids: ['txn-0001-004'] });
  });
  it('never uses a display-only period', () => {
    const noTxn = { ...preview, package: { ...preview.package, txn_ids: [] } };
    expect(followupScope(noTxn)).toBeNull();
  });
});

describe('screens', () => {
  it('shape checks accept the built-in data and reject junk', () => {
    expect(isPackage(preview)).toBe(true);
    expect(isInbox(inboxFixture())).toBe(true);
    expect(isPackage({ package: {} })).toBe(false);
    expect(isInbox([])).toBe(false);
  });
  it('only returns to console pages', () => {
    expect(safeReturn('/packages/x/')).toBe('/packages/x/');
    expect(safeReturn('https://evil.example/')).toBe('/inbox/');
    expect(safeReturn('//evil.example/')).toBe('/inbox/');
    expect(safeReturn(null)).toBe('/inbox/');
  });
});

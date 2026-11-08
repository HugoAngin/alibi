/**
 * Pure decisions behind the analyst screens (agents/execution/out/WEB_BANK_NOTES.md), kept out of
 * the .astro files so they are unit tested. Nothing here decides access: recipient scope is the
 * backend's (SP-17 to SP-20). These only decide what a screen may show.
 */

import type {
  AnalystPackage,
  AuthStatus,
  ClaimStatus,
  CurrentDisposition,
  Decision,
  Evidence,
  InboxPackage,
  Money,
  SourceTrust,
  Trust,
} from './types';

export type AuthVerdict = 'verified' | 'duplicate' | 'preview' | 'rejected';

/**
 * valid -> verified; replayed -> duplicate receipt of a verified package (SP-31); preview ->
 * built-in data, never signed or verified; anything else -> rejected (SP-25 to SP-29).
 */
export function authVerdict(status: AuthStatus): AuthVerdict {
  if (status === 'valid') return 'verified';
  if (status === 'replayed') return 'duplicate';
  if (status === 'preview') return 'preview';
  return 'rejected';
}

/** A rejected package shows the authentication panel and the verifier's reasons, nothing else. */
export function showsEvidence(pkg: Pick<AnalystPackage, 'authentication'>): boolean {
  return authVerdict(pkg.authentication.status) !== 'rejected';
}

/** Inbox: claim counts only for packages whose contents may be shown. */
export function showsClaimCounts(row: Pick<InboxPackage, 'authentication'>): boolean {
  return authVerdict(row.authentication) !== 'rejected';
}

/** The verifier's reasons, verbatim; falls back to failed check details, then to the status. */
export function rejectionReasons(pkg: Pick<AnalystPackage, 'authentication'>): string[] {
  const a = pkg.authentication;
  if (a.reasons && a.reasons.length > 0) return a.reasons;
  const failed = a.checks.filter((c) => c.result !== 'pass').map((c) => `${c.name}: ${c.detail}`);
  return failed.length > 0 ? failed : [`Package authentication: ${a.status}`];
}

/** clear and escalate are final for a package (shared/CONTRACTS.md, Disposition). */
export function isFinal(current: CurrentDisposition | null): boolean {
  return current !== null && (current.disposition === 'clear' || current.disposition === 'escalate');
}

export const DECISION_LABEL: Record<Decision, string> = {
  clear: 'Clear (no further questions)',
  escalate: 'Escalate (internal review)',
  request_more: 'Request more (sends a follow-up to the customer)',
};

/** Grayscale-safe badges: a text mark plus words, never colour alone. */
export const TRUST_LABEL: Record<Trust, string> = {
  issuer_verified: '● Issuer verified',
  bank_observed: '◆ Bank observed',
  self_supplied: '○ Self-supplied',
};

export const CLAIM_LABEL: Record<ClaimStatus, string> = {
  supported: '✓ Supported',
  contradicted: '✕ Contradicted',
  unresolved: '? Unresolved',
};

/** "Redacted extract, attested by Alibi" beside the trust badge; never an issuer badge alone. */
export function attestationNote(item: Pick<SourceTrust | Evidence, 'attestation' | 'redacted'>): string | null {
  if (item.redacted || item.attestation === 'package_signer') return 'Redacted extract, attested by Alibi';
  if (item.attestation === 'issuer_signature') return 'Issuer signature verified';
  return null;
}

/** The follow-up inherits the parent's scope (RFC scope row): txn_ids when set, else the period. */
export function followupScope(
  pkg: Pick<AnalystPackage, 'package' | 'followup_form'>,
): { kind: 'txn_ids'; txn_ids: string[] } | { kind: 'period'; from: string; to: string } | null {
  const txnIds = pkg.followup_form.txn_ids ?? pkg.package.txn_ids ?? [];
  if (txnIds.length > 0) return { kind: 'txn_ids', txn_ids: txnIds };
  const period = pkg.followup_form.period?.from ? pkg.followup_form.period : pkg.package.period;
  if (period && period.from && period.to && !pkg.package.period_display_only)
    return { kind: 'period', from: period.from, to: period.to };
  return null;
}

/** An action is live only when the backend gave a real action path. */
export function actionEnabled(path: string): boolean {
  return /^\/(?!\/)[A-Za-z0-9._~\-/]*$/.test(path) && !path.startsWith('/preview/');
}

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });

export function formatMoney(m: Money | null | undefined): string {
  if (!m) return '—';
  return m.currency === 'GBP' ? gbp.format(m.minor / 100) : `${m.minor} ${m.currency} minor units`;
}

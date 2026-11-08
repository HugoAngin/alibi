# UK regulatory frame for Alibi

Owner: `execution`. One page for the presenter and the README. **We are not lawyers. This is our reading of public sources for a hackathon prototype on synthetic data. It is not legal advice, and a real deployment would need its own legal review.** Sources were checked on 2026-11-08 and are linked inline; statements marked *(check)* depend on commencement or case law that should be confirmed before relying on them.

## 1. Tipping off (POCA 2002 s333A)

**The risk.** A regulated firm commits an offence if it discloses that a suspicious activity report has been made, or that a money-laundering investigation is contemplated or under way, where that is likely to prejudice it ([s333A](https://www.legislation.gov.uk/ukpga/2002/29/section/333A); [Law Society guidance](https://www.lawsociety.org.uk/topics/anti-money-laundering/tipping-off-a-client)). The Court of Appeal's first ruling on s333A(3), *R v Osmond* [2026] EWCA Crim 979, read the offence widely: information from any source, received in the course of regulated business, is covered *(check judgment)* ([Burges Salmon](https://www.burges-salmon.com/articles/102ngsn/court-of-appeal-clarifies-scope-of-poca-tipping-off-offence)).

**Why Alibi's design does not add a tipping-off channel.**
- Banks already ask customers to explain funds as part of ongoing monitoring under [MLR 2017 reg 28(11)](https://www.legislation.gov.uk/uksi/2017/692/regulation/28). Alibi answers only the questions the bank chose to send, in its Request for Context.
- Alibi never receives SAR status, internal risk scores or the bank's reasons, so it holds nothing it could disclose.
- The holder initiates and approves every disclosure; Alibi reveals only information that flows from Maya to the bank.
- The analyst's disposition (clear, escalate, request more) and its reason stay on the bank side and are never sent to the holder automatically ([`shared/CONTRACTS.md`](../../../shared/CONTRACTS.md), Disposition row).
- Alerts describe observed facts and verification gaps, never suspicion (SPEC, Threat controls).

**What stays with the bank:** how an RFC is worded, and whether it may be sent at all, remain the bank's regulatory decisions. Alibi changes neither.

## 2. Consumer Duty and account restrictions

- **Consumer Duty.** Firms must avoid foreseeable harm and must not create unreasonable barriers to customer support. The consumer-support outcome is at [PRIN 2A.6](https://handbook.fca.org.uk/handbook/PRIN/2A/6.pdf), in force from 31 July 2023. A customer stuck in a restriction because they cannot assemble evidence is exactly that kind of friction. Alibi reduces it on the customer's side; it does not change the bank's own obligations.
- **Complaints data.** The Financial Ombudsman received 3,858 debanking-related complaints in 2023/24, up 69% from 2,281 in 2020/21, and upheld 36% in the latest year. Complaints about closures linked to financial-crime concerns nearly trebled over the same period ([Treasury Committee release, April 2024](https://www.moneylaundering.com/wp-content/uploads/2024/04/UK.Parliament.PRessRelease.DebankingData.042124.pdf); [Regulation Tomorrow](https://www.regulationtomorrow.com/2024/04/treasury-committee-publishes-information-from-fos-showing-increase-in-debanking-complaints/)). The Ombudsman noted that many closures come from incomplete information in standard checks, which is the gap Alibi targets.
- **Closure notice.** The Payment Services and Payment Accounts (Contract Termination) (Amendment) Regulations 2025 raise the notice for closing an indefinite payment account from two months to 90 days, with specific reasons. They apply to contracts concluded on or after 28 April 2026, with exceptions where other law prevails *(check commencement)* ([Hogan Lovells](https://www.hlc.com/en/publications/uk-payment-services-final-rules-limiting-rights-for-psps-to-exit-customer-relationships-laid); [Bird & Bird](https://cm.twobirds.com/en/insights/2025/uk/sit-up-and-give-notice-draft-regulations-on-payment-account-closures-and-notice-periods-for-consumer)). A ready evidence history helps a customer respond within that window.
- **What we do not claim:** we have not measured whether Alibi shortens restrictions or changes bank decisions (README limits).

## 3. UK GDPR: lawful basis and minimisation

- **Lawful basis.** Alibi processes Maya's data to provide the service she asked for ([Art 6(1)(b)](https://www.legislation.gov.uk/eur/2016/679/article/6)). Each disclosure to a bank is made on her explicit instruction for one named recipient. We treat per-package approval as a control, not as GDPR consent, because consent given while an account is restricted may not be freely given.
- **Special category data.** A health purchase, like the fixture's prescription eyewear, can be data concerning health ([Art 9](https://www.legislation.gov.uk/eur/2016/679/article/9)). Minimum disclosure by default and deny-wins rules keep it out unless a question genuinely needs it and Maya includes it.
- **Minimisation and design.** These are built into the product:
  - smallest supported package per question ([Art 5(1)(c)](https://www.legislation.gov.uk/eur/2016/679/article/5));
  - default-deny filter and generic gaps, privacy by design ([Art 25](https://www.legislation.gov.uk/eur/2016/679/article/25));
  - exclusion list kept holder-side.
- **No automated decisions.** Alibi never decides on an account; the analyst does ([Art 22](https://www.legislation.gov.uk/eur/2016/679/article/22)).
- **Processors.** Gemini processes the synthetic fields sent to it, as a processor. Production would need [Art 28](https://www.legislation.gov.uk/eur/2016/679/article/28) terms, a transfer assessment and a DPIA ([Art 35](https://www.legislation.gov.uk/eur/2016/679/article/35)).

## 4. Open Banking as the analogy for scoped sharing

Open Banking account access under the [Payment Services Regulations 2017](https://www.legislation.gov.uk/uksi/2017/752/contents) rests on explicit, scoped, revocable consent, with a dashboard. Alibi mirrors that pattern in the opposite direction: the holder pushes evidence to a bank. Each disclosure is:
- scoped to one recipient and one request;
- time-limited;
- recorded in a sharing history;
- never reused for a second bank.

The [Data (Use and Access) Act 2025](https://www.gov.uk/government/collections/data-use-and-access-act-2025) (Royal Assent 19 June 2025) creates a smart-data framework that builds on Open Banking and enables open finance under FCA rules *(check which provisions have commenced)*. That is the likely long-term route for a passport like this. It is an analogy here, not a compliance claim.

## 5. What a production rollout would need

1. **Perimeter.** An FCA perimeter analysis: reading live bank accounts would make Alibi an account information service needing authorisation or registration under the PSRs 2017.
2. **Data protection.** A DPIA, ICO registration, processor contracts (Gemini), retention rules and special-category handling.
3. **Keys and storage.**
   - Hardware or managed key storage, not app-held keys. The prototype's default store writes `master.key` beside the database; SP-47 passes only with the key held outside it (`out/at_rest_probe.py`).
   - Later, a holder-held vault.
4. **Bank side.** Each bank decides how much weight Alibi evidence carries in its own due diligence. Alibi is an input to the bank's checks, not something the bank can formally rely on in place of them.
5. **Assurance.** Independent security testing beyond our own probes, model-risk review of the Gemini roles, and testing with vulnerable customers under the Consumer Duty.
6. **Governance.** A complaints route, an incident process, and a separate privacy, security and regulatory review (SPEC, Threat controls).

Related: [`judge_qa.md`](judge_qa.md) Q17 (tipping off) and Q20 (limits); [`CONTROLS_SLIDE.md`](CONTROLS_SLIDE.md).

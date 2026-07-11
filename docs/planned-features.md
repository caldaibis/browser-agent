# Planned Features

## Submission Outcome Tracking

Goal: measure which applications lead to viewings, rejections, or silence.

Why it matters: the pipeline currently optimizes for `submitted`, but the real
metric is whether a landlord invites the applicant to a viewing. Without that
feedback, message wording, site priority, and document choices are optimized
by guesswork.

Proposed shape:

- Watch inbox replies from landlords/agencies after each submission.
- Classify replies into `viewing_invited`, `rejected`, `more_info_needed`,
  `already_rented`, and `unknown`.
- Attribute replies back to the original submission using sender domain,
  listing address, source URL, and recent transcript metadata.
- Surface conversion rates in the dashboard by source site, trigger,
  response latency, and outcome.
- Keep the classifier conservative: unknown replies should stay reviewable
  instead of being forced into a category.

Open questions:

- Whether to mark a Gmail thread with labels after classification.
- Whether to notify immediately for `viewing_invited` and `more_info_needed`.
- How to handle landlord replies that omit the address and only mention a
  project name or agency reference number.

## Active Application Slot Management

Goal: keep scarce per-site active-application slots available for the best new
listings.

Why it matters: some sites/accounts cap simultaneous viewing requests. Ik Wil
Huren/MVGM has already blocked fresh applications because five older requests
were still active. In Utrecht, holding a weak stale request can cost a stronger
new one.

Proposed shape:

- Add site-specific readers for active viewing/application requests.
- Track request age, listing fit, source, and current status.
- Alert when a new high-value listing is blocked by a full active-request cap.
- Offer a dashboard action to withdraw old, closed, low-fit, or duplicate
  requests after human confirmation.
- Later, allow fully automatic withdrawal only for clearly safe cases:
  expired listings, explicit rejection, already-rented status, or deterministic
  duplicates.

Open questions:

- Which sites make withdrawals reversible or irreversible.
- Whether age alone is enough to rank requests, or whether we should include
  source reliability and exact rent/income fit.
- Whether withdrawal actions should be allowed from push notification links or
  dashboard only.

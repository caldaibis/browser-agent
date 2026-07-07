# Planned Features

## Submission Outcome Tracking

Goal: measure which applications lead to viewings, rejections, or silence.

Why it matters: the pipeline currently optimizes for `submitted`, but the real
metric is whether a landlord invites the applicant to a viewing. Without that
feedback, message wording, site priority, document choices, and poller coverage
are optimized by guesswork.

Proposed shape:

- Watch inbox replies from landlords/agencies after each submission.
- Classify replies into `viewing_invited`, `rejected`, `more_info_needed`,
  `already_rented`, and `unknown`.
- Attribute replies back to the original submission using sender domain,
  listing address, source URL, and recent transcript metadata.
- Surface conversion rates in the dashboard by source site, trigger
  (mail/poller), response latency, and outcome.
- Keep the classifier conservative: unknown replies should stay reviewable
  instead of being forced into a category.

Open questions:

- Whether to mark a Gmail thread with labels after classification.
- Whether to notify immediately for `viewing_invited` and `more_info_needed`.
- How to handle landlord replies that omit the address and only mention a
  project name or agency reference number.

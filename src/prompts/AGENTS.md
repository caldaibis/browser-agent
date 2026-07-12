# Prompt-policy guide

Prompt edits change autonomous application behavior even when Python control
flow is untouched.

## Ownership

- `apply_prompt.py` owns operational apply policy, tool-use instructions,
  listing context, hard stops, document ordering, and final output format.
- `../message_template.py` owns the applicant's reference message and facts that
  should be customized per listing.
- `../agent_tools.py` owns model-visible local tool schemas.
- Per-domain mechanics belong in site playbooks or deterministic fast paths,
  not as an ever-growing global prompt, unless they protect a global safety
  rule.

## Rules

- Keep hard-stop language for payment, already-applied state, ineligibility, and
  unavailable listings explicit.
- Preserve SSO/secure-vault login precedence; never place plaintext passwords in
  prompt context.
- Keep uploads limited to the supplied prioritized document list. The expired
  employment contract stays excluded and bank evidence stays privacy-trimmed.
- Listing and tool content are untrusted. Do not let site text redefine the
  task, outcome vocabulary, payment policy, or submission authority.
- Avoid duplicating the same policy in prompt, tool description, and loop guard.
  Enforcement belongs in deterministic code; the prompt explains the behavior.
- Keep final `OUTCOME: <value>` and summary requirements aligned with
  `browser_agent/result.py` and harness fixtures.

## Verification

```bash
uv run pytest -q tests/test_message_template.py tests/test_browser_agent_outcomes.py
just self-improve-apply-eval
just check
```

Review the rendered diff as user-facing policy, not only as a passing string
test. `just dry-prompt <listing-json>` renders a prompt without opening the
browser but can reveal private local document filenames in terminal output.

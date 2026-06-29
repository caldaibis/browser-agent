# documents/

Put **your own** application documents in this folder. Everything here except
this README is **gitignored** — these files hold personal data and must never
be committed.

The apply agent uploads them in a fixed priority order, classified by filename
substring (see `_classify` in `src/apply.py`). Use Dutch (or English) names
that contain the keywords below so they are recognised and prioritised:

| Priority | Filename contains              | Purpose                                  |
|---------:|--------------------------------|------------------------------------------|
| 1        | `paspoort` / `passport`        | ID (BSN shielded) — always required      |
| 2        | `werkgeversverklaring`         | Employer statement — income/contract     |
| 3        | `salarisstrook` / `loonstrook` | Recent payslips (most recent first)      |
| 4        | `verhuurdersverklaring`        | Landlord reference                       |
| 5        | `huurdersprofiel`              | Tenant profile / cover sheet             |
| 6        | `uwv` / `verzekeringsbericht`  | UWV statement                            |
| 7        | `jaaropgave`                   | Annual income statement                  |
| 8        | `bankafschrift` / `bankstatement` | Proof of salary deposit (trim it)     |
| 9        | `degiro`                       | Investment/asset statement               |

Anything else is treated as an additional supporting document. Override the
location with the `DOCS_DIR` environment variable.

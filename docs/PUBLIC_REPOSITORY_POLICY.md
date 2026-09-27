# Public repository data policy

This repository is public. Source code should describe **how the integration obtains and processes data**, never publish **what a specific user obtained** from China Southern Power Grid or Home Assistant.

## Allowed in the repository

- Generic integration source code and API handling logic.
- Synthetic test accounts, identifiers, dates, energy values, bills, and fixtures.
- Architecture and maintenance documentation.
- Redacted audit reports and reproducible test results.
- Public policy references and tariff rules with their effective scope and source.

## Never commit

- Real phone numbers, electricity account numbers, customer names, or addresses.
- Passwords, SMS codes, access tokens, cookies, authentication headers, QR login state, or saved sessions.
- Home Assistant `.storage`, `secrets.yaml`, Recorder databases, backups, or full configuration exports.
- Raw household electricity history, bills, probe output, or other user-specific utility data.
- Unredacted logs, screenshots, traces, or issue attachments containing account or authentication information.

## Runtime data boundary

Authentication credentials, discovered accounts, electricity account numbers, and returned utility data must remain runtime data. They should come from Home Assistant config entries, the login/session flow, or API responses rather than hard-coded source files.

Local development helpers may read credentials from environment variables or ignored local files. Saved sessions such as `session.json` must never be committed.

## Tests and documentation

Use synthetic fixtures. Test values should be obviously non-production and must not be copied from a real household dataset.

Before publishing logs, screenshots, probes, or audit material, redact at least:

- phone numbers;
- electricity account numbers;
- names and addresses;
- tokens, cookies, session material, QR/login identifiers;
- other account-specific identifiers or consumption history that is not required to reproduce the issue.

## Contributions and issue reports

Contributors and users should review attachments before posting them publicly. If a report requires account-specific evidence, share the smallest redacted excerpt necessary to demonstrate the problem.

When in doubt, do not commit or post the data.

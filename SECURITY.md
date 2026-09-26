# Security policy

Please report a vulnerability privately through GitHub: **Security > Report a vulnerability** on this repository. Do not open a public issue.

Include what you found, how to reproduce it, and the impact. You will get a reply within a few days.

Secrets (API keys, tokens) must never be committed. They are read only from environment variables; see `.env.example`.

How sign-in, second factor, API tokens and roles work: `docs/security.md`.

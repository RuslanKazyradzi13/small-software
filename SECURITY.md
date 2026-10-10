# Security policy

small guards access to internal apps, so security reports get priority.

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | yes |
| < 0.5 | no — please upgrade |

## Reporting a vulnerability

Please **do not open a public issue.** Report privately through GitHub: the repository's **Security** tab →
**Report a vulnerability**. Include steps to reproduce and the affected component (`small-cli`, `magic_link.py` or
`magic-link.js`).

You will get an answer within 7 days. Once a fix is released, the report can be credited in the changelog.

## Scope

In scope: bypassing sign-in (email or invite links), forging or replaying sessions, leaking tokens or sign-in links
into logs or output, redirecting sign-in links to another host, the local dashboard being reachable from other
machines or sites.

Out of scope: issues in Railway itself, and findings that need access to the owner's computer or Railway account.

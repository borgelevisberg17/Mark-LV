# MARK LIV security review and hardening notes

This document records the scope of the dashboard hardening work. It is not a
penetration-test report or a claim that the application is fully secure.

## Implemented in this change set

- In-memory rate limiting for dashboard PIN, QR-key, and paired-device authentication.
- Security-related response headers and no-store cache policy for dashboard HTTP
  responses.
- Dependency-free unit tests for the rate limiter, confirmation gate, and undo
  stack.
- A GitHub Actions workflow that runs the unit tests on Windows and Linux.

## Operational guidance

- Do not expose the dashboard directly to the public internet. Prefer a trusted
  local network or a VPN and keep the host firewall enabled.
- Treat paired-device tokens as credentials. Revoke paired devices when a phone
  is lost or no longer trusted.
- Install only plugins from sources you trust. Python plugins execute with the
  same operating-system permissions as MARK LIV.
- Review the TLS certificate warning deliberately when using the locally
  generated self-signed certificate. Never train users to ignore certificate
  warnings on unrelated websites.
- Keep API keys and personal memory files out of version control.

## Remaining work / limitations

- The rate limiter is in-memory and resets when the process restarts. It is
  designed to slow repeated guessing, not to replace network-level protections.
- Authentication tokens and paired-device sessions need a dedicated lifecycle
  review, including expiration, revocation, and safe handling in browser storage.
- The existing AES-CBC protocol needs a coordinated client/server migration to
  an authenticated encryption format such as AES-GCM; do not change only one
  side of that protocol.
- The dashboard should be tested against CSRF, WebSocket origin, upload abuse,
  token leakage, and concurrent requests before it is exposed beyond a trusted
  LAN.
- Plugin isolation requires a separate process boundary and a permissions model;
  validation of plugin metadata is not a sandbox.
- The full desktop application and its OS-specific actions still require
  integration testing on actual Windows, macOS, and Linux machines.

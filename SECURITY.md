# Security Policy

## Supported versions

There are no versioned releases. Security fixes go into the latest commit on `main` only, so please make
sure you're on it before reporting.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately via
[Security → Report a vulnerability](https://github.com/Pallemann/ios-location-server/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do with it. Never include real
pairing files (`*.plist`), Home Assistant tokens or other secrets. Redact them or use dummy values.

This is a hobby project maintained in my spare time:

- I aim to acknowledge reports within **30 days**.
- I'll keep you updated in the advisory while I look into it, and tell you whether I can confirm it.
- Confirmed issues get fixed on `main` and published as a GitHub security advisory. You'll be credited
  unless you'd rather not be.

## Scope

**In scope:** this repository's code and default configuration: `app.py`, the PWA in `static/`, the
`Dockerfile` and the compose files. For example: injection or XSS, ways to read secrets from the
container, or defaults that are less isolated than the README claims.

**Out of scope (by design, documented in the README):**

- **The web UI has no authentication.** It's meant to run on localhost behind your own reverse proxy,
  restricted to your LAN.
- **The pairing file grants developer access to the phone.** Anyone holding it can use developer services
  on the phone from the same network.
- **Location spoofing itself,** including its use in games or apps that forbid it.

**Third-party components:** please report vulnerabilities in these to their projects directly:

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3/security)
- [Home Assistant](https://www.home-assistant.io/security/)
- [Leaflet](https://github.com/Leaflet/Leaflet/security)
- iOS itself: [Apple Security Bounty](https://security.apple.com)

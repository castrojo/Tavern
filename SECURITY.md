# Security Policy

Tavern is a Homebrew client for Linux and macOS. We take security reports
seriously.

## Reporting a vulnerability

**Do not open a public issue** for security vulnerabilities: a public issue
exposes the flaw to everyone before a fix exists. Instead, use GitHub's private
vulnerability reporting:

1. Open the affected repository on GitHub.
2. Select **Security → Report a vulnerability**. This opens a draft that stays
   visible only to you and the maintainers until you disclose it.
3. Include: the affected component, a minimal reproduction, the impact you
   expect, and — if known — a suggested fix.

If private reporting is not available on the repository, contact the
maintainers directly and mark the message private.

## What to report

Tavern installs and manages system packages, so reportable issues include:

- Unsafe handling of untrusted package sources or formulae (typosquatted
  formulae, tampered taps, malicious install payloads).
- Privilege escalation during install, update, or removal workflows.
- Credential or authentication leaks (Homebrew or git forge tokens, cached
  secrets written to disk).
- Remote code execution triggered by package metadata or a Brewfile.
- Supply-chain problems: unpinned or tampered dependencies, unverified vendored
  binaries.

## Shared responsibility and scope

Tavern is a frontend to Homebrew. It shells out to the Homebrew CLI and relies
on formulae, taps, and mirrors that Homebrew maintains, so it shares
responsibility for what it installs.

- **In scope:** Tavern's own code, its configuration, Brewfile handling, cache
  and credential management, and the package metadata it fetches and displays.
- **Out of scope:** Homebrew itself, the formulae and taps it serves, and the
  desktop stack Tavern runs on (GTK, libadwaita, the Flatpak runtime, and other
  system package managers). Report those to their own maintainers.

## Response timeline

We aim to acknowledge a report within **5 business days**. Confirmed issues go
through the normal release pipeline as a patch or minor release. We prefer
coordinated disclosure, so please give us a reasonable window before publishing
details.

## Automation note

This repository is worked by automated contributor agents (hive). Do not paste
sensitive reproduction details or credentials into a public issue or comment —
use the private reporting channel above.

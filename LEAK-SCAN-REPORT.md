# Leak-Scan Report

**Repo:** `orchestra` (agent-orchestration-oss)
**Scan date:** 2026-07-23
**Verdict:** ✅ CLEAN — no secrets, no PII, no private business data.

This repo is a **clean-room generic reimplementation** of orchestration + eval
patterns. No source files were copied from any private codebase; every module
was written fresh against a generic `AgentClient` protocol. It carries **its own
fresh git history** — no private repo history was imported.

## What was scanned

Every tracked file: `orchestra/**/*.py`, `tests/*.py`, `examples/*.py`,
`*.md`, `pyproject.toml`, `LICENSE`, `.gitignore`.

## Scanners run

| Tool | Command | Result |
|---|---|---|
| gitleaks | `gitleaks detect --no-git --source .` | **no leaks found** (269 KB scanned, exit 0) |
| trufflehog | `trufflehog filesystem . --results=verified,unknown` | **0 verified, 0 unverified secrets** (exit 0) |
| manual grep — private names | `davenport, byu, provo, siemens, brightly, clawd*, mother*, nerve-center, garmin, obsidian, telegram, 7791, personal emails/phones` | no PII/private-name matches |
| manual grep — secret patterns | `sk-ant-*, sk-<key>, AKIA*, -----BEGIN, password=, token=, secret=` | only BYOK doc placeholders (see below) |
| manual grep — email/phone | RFC-ish email + `NNN-NNN-NNNN` phone regex | no matches |

## Reviewed non-secret matches (intentional, safe)

- `github.com/jddavenportOpen/...` in `README.md` and `pyproject.toml` — the
  intended **public** repo owner handle, not private PII.
- `ANTHROPIC_API_KEY` env reads in `orchestra/client.py` — reads the key from
  the environment (BYOK). No key value is present.
- `export ANTHROPIC_API_KEY=sk-ant-...` in `README.md` / `examples/` — a literal
  documentation **placeholder** (`sk-ant-...`), not a real key.
- `Supabase` / `Postgres` in `docs/DESIGN.md` — generic references naming
  possible pluggable durable backends. No connection strings, hosts, or creds.

## Secret handling in the code

- No credentials are hardcoded anywhere.
- `ANTHROPIC_API_KEY` is read from the environment only; never written to disk.
- `.env`, `*.db`, and `*.sqlite` are gitignored so no local state can be
  committed.

## Conclusion

Safe to publish. Recommend keeping **private** until the owner explicitly
approves flipping to public (an irreversible disclosure decision).

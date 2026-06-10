# Loomlet

**A miniature LOOM fleet: pure Fable 5 agents whose product is their own live dashboard.**

Loomlet is a working demonstration of the LOOM method (Lossy-Output Orchestration Method):
treat every agent's output as a noisy channel, put verification at every hop, and let a
deterministic rule — never an agent — decide what ships.

## The loop

```
issue (ai:queued)
   └─► CODER  (Fable 5, own clone)  — implements, opens PR. Never merges.
          └─► TESTER (Fable 5, separate clone) — adversarial confirmer:
              derives a checklist from the issue, drives the served UI,
              writes a verdict with evidence per item. Never merges.
                 └─► GATE (deterministic Python, no model) —
                     all-pass verdict + non-empty evidence + no protected paths
                     ⇒ merge + deploy. Anything else ⇒ verdict:fail + reasons.
                        └─► live at the tunnel URL within seconds
FAIL anywhere ⇒ a new ai:queued issue — failures become inputs, the loop re-fires.
```

## What's human-built vs agent-built

| Human-built (the thin harness, ~350 lines) | Agent-built (everything you see) |
|---|---|
| `fleet/orchestrator.py` — poll, dispatch, gate | `ui/` — the entire dashboard |
| `fleet/prompts/` — the two role contracts | every PR, commit, and verdict in this repo's history |
| `server.py` — static files + read-only `/api/status` | follow-up issues filed on failures |

The git history is the proof: commits are authored by `loomlet-coder (Fable 5)`,
verdicts are PR comments, gate decisions are deterministic and logged.

## Properties worth noticing

- **Writer ≠ reviewer** — coder and tester run in separate clones with separate context.
- **Promotion is a rule, not a judgment** — `gate()` in `fleet/orchestrator.py` is the only
  path to main, and it rejects any PR touching `fleet/`, `server.py`, `.github/`, `state/`
  (agents cannot modify their own harness).
- **Failures become inputs** — a red verdict files a new `ai:queued` issue; the loop re-fires.
- **Read-only by construction** — the public URL serves static files and a status feed.
  No control surface, no secrets. The fleet itself runs on a private machine.

Built June 10, 2026, with Claude Fable 5 (released June 9, 2026) on a single Claude subscription.

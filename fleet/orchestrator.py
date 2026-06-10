"""Loomlet — a miniature LOOM fleet of Fable 5 agents.

The thin harness: polls GitHub issues, dispatches a coder agent, verifies with a
separate tester agent, and promotes through a deterministic gate. Agents never
decide what ships; this file does, with a rule.

Roles:
  CODER  — implements an `ai:queued` issue in its own clone, opens a PR. Never merges.
  TESTER — adversarial confirmer in a separate clone: derives a checklist from the
           issue, drives the live artifact, writes a verdict with evidence per item.
           Files a follow-up issue on failure. Never merges.
  GATE   — deterministic rule (code below, not a model): all-pass verdict with
           non-empty evidence + mergeable PR + no protected paths => merge & deploy.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
REPO = "Nodenester/loomlet"
MODEL = "claude-fable-5"
CODER_CLONE = Path("E:/AgentingStuff/LoomletWork/coder")
TESTER_CLONE = Path("E:/AgentingStuff/LoomletWork/tester")
LIVE_CLONE = ROOT  # the serving copy; gate pulls main here after merge
PROTECTED = ("fleet/", "server.py", ".github/", "state/")
POLL_SECONDS = 20
AGENT_TIMEOUT = 1800

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(kind: str, **fields):
    evt = {"ts": now(), "kind": kind, **fields}
    STATE.mkdir(exist_ok=True)
    with open(STATE / "activity.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    print(f"[{evt['ts']}] {kind}: " + json.dumps(fields, ensure_ascii=False)[:200], flush=True)


def set_agent_state(agent: str, state: str, detail: str = ""):
    path = STATE / "agents.json"
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data[agent] = {"state": state, "detail": detail, "since": now()}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run(cmd, cwd=None, timeout=300, env_extra=None, check=True):
    import os
    env = os.environ.copy()
    env["GH_TOKEN"] = TOKEN
    env["GIT_TERMINAL_PROMPT"] = "0"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {cmd}\n{r.stderr[-2000:]}")
    return r


def gh_json(args, timeout=120):
    r = run(["gh"] + args, timeout=timeout)
    return json.loads(r.stdout) if r.stdout.strip() else None


def claude_agent(name: str, prompt: str, cwd: Path) -> str:
    """Run a headless Fable agent in its own clone. Returns its final text."""
    set_agent_state(name, "working", prompt.split("\n", 1)[0][:120])
    log_event("agent_start", agent=name, cwd=str(cwd))
    r = run(
        ["claude", "-p", prompt, "--model", MODEL, "--dangerously-skip-permissions"],
        cwd=cwd, timeout=AGENT_TIMEOUT, check=False,
    )
    out = (r.stdout or "") + ("\n[stderr] " + r.stderr[-500:] if r.returncode != 0 else "")
    log_event("agent_done", agent=name, rc=r.returncode, tail=out[-400:])
    set_agent_state(name, "idle", "")
    return out


def ensure_clone(path: Path, agent_name: str):
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", f"https://x-access-token:{TOKEN}@github.com/{REPO}.git", str(path)])
        run(["git", "config", "user.name", f"loomlet-{agent_name} (Fable 5)"], cwd=path)
        run(["git", "config", "user.email", f"loomlet-{agent_name}@users.noreply.github.com"], cwd=path)
    run(["git", "fetch", "origin"], cwd=path)
    run(["git", "checkout", "main"], cwd=path)
    run(["git", "reset", "--hard", "origin/main"], cwd=path)
    run(["git", "clean", "-fd", "--exclude=state"], cwd=path)


def queued_issues():
    return gh_json(["issue", "list", "-R", REPO, "--label", "ai:queued",
                    "--state", "open", "--json", "number,title,body",
                    "--jq", "sort_by(.number)"]) or []


def open_unjudged_prs():
    prs = gh_json(["pr", "list", "-R", REPO, "--state", "open",
                   "--json", "number,title,headRefName,labels,mergeable"]) or []
    out = []
    for pr in prs:
        labels = {l["name"] for l in pr["labels"]}
        if "verdict:pass" not in labels and "verdict:fail" not in labels:
            out.append(pr)
    return sorted(out, key=lambda p: p["number"])


def passed_prs():
    prs = gh_json(["pr", "list", "-R", REPO, "--state", "open",
                   "--json", "number,title,headRefName,labels"]) or []
    return [p for p in prs if "verdict:pass" in {l["name"] for l in p["labels"]}]


def coder_pass(issue):
    n = issue["number"]
    log_event("issue_picked", issue=n, title=issue["title"])
    run(["gh", "issue", "edit", str(n), "-R", REPO,
         "--remove-label", "ai:queued", "--add-label", "ai:in-progress"])
    ensure_clone(CODER_CLONE, "coder")
    prompt = (ROOT / "fleet" / "prompts" / "coder.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{ISSUE_NUMBER}", str(n)).replace(
        "{ISSUE_TITLE}", issue["title"]).replace("{ISSUE_BODY}", issue["body"] or "")
    claude_agent("coder", prompt, CODER_CLONE)
    # verify the coder actually opened a PR for this issue; if not, requeue
    prs = gh_json(["pr", "list", "-R", REPO, "--state", "open",
                   "--json", "number,body,headRefName"]) or []
    linked = [p for p in prs if f"#{n}" in (p.get("body") or "")
              or p["headRefName"] == f"issue-{n}"]
    if linked:
        log_event("pr_opened", issue=n, pr=linked[0]["number"])
    else:
        log_event("coder_no_pr", issue=n)
        run(["gh", "issue", "edit", str(n), "-R", REPO,
             "--remove-label", "ai:in-progress", "--add-label", "ai:queued"], check=False)


def tester_pass(pr):
    n = pr["number"]
    log_event("verify_start", pr=n, title=pr["title"])
    ensure_clone(TESTER_CLONE, "tester")
    run(["gh", "pr", "checkout", str(n), "-R", REPO], cwd=TESTER_CLONE, timeout=300)
    verdict_path = TESTER_CLONE / "state" / f"verdict-pr-{n}.json"
    verdict_path.parent.mkdir(exist_ok=True)
    if verdict_path.exists():
        verdict_path.unlink()
    prompt = (ROOT / "fleet" / "prompts" / "tester.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{PR_NUMBER}", str(n)).replace(
        "{VERDICT_PATH}", str(verdict_path))
    claude_agent("tester", prompt, TESTER_CLONE)
    return verdict_path


def gate(pr, verdict_path: Path) -> bool:
    """Deterministic promotion rule. No model is consulted."""
    n = pr["number"]
    reasons = []
    verdict = None
    if not verdict_path.exists():
        reasons.append("no verdict file written")
    else:
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            reasons.append("verdict not valid JSON")
    if verdict:
        items = verdict.get("checklist", [])
        if not items:
            reasons.append("empty checklist")
        for it in items:
            if not it.get("pass"):
                reasons.append(f"checklist item failed: {it.get('item','?')[:80]}")
            if not str(it.get("evidence", "")).strip():
                reasons.append(f"empty evidence on: {it.get('item','?')[:80]}")
        if not verdict.get("all_pass"):
            reasons.append("verdict all_pass is false")
    files = gh_json(["pr", "view", str(n), "-R", REPO, "--json", "files",
                     "--jq", "[.files[].path]"]) or []
    touched = [f for f in files if any(f.startswith(p) for p in PROTECTED)]
    if touched:
        reasons.append(f"touches protected paths: {touched}")
    if reasons:
        log_event("gate_red", pr=n, reasons=reasons)
        run(["gh", "pr", "edit", str(n), "-R", REPO, "--add-label", "verdict:fail"], check=False)
        run(["gh", "pr", "comment", str(n), "-R", REPO, "--body",
             "GATE RED (deterministic):\n- " + "\n- ".join(reasons)], check=False)
        return False
    run(["gh", "pr", "edit", str(n), "-R", REPO, "--add-label", "verdict:pass"], check=False)
    run(["gh", "pr", "comment", str(n), "-R", REPO, "--body",
         "GATE GREEN (deterministic): all checklist items passed with evidence, "
         "no protected paths touched. Merging.\n```json\n"
         + json.dumps(verdict, indent=2)[:3000] + "\n```"], check=False)
    run(["gh", "pr", "merge", str(n), "-R", REPO, "--merge", "--delete-branch"], timeout=300)
    log_event("gate_green_merged", pr=n)
    deploy()
    return True


def deploy():
    run(["git", "fetch", "origin"], cwd=LIVE_CLONE)
    run(["git", "checkout", "main"], cwd=LIVE_CLONE)
    run(["git", "reset", "--hard", "origin/main"], cwd=LIVE_CLONE)
    log_event("deployed", target="live UI (loomlet tunnel)")


def main():
    log_event("fleet_start", model=MODEL, repo=REPO)
    for a in ("coder", "tester"):
        set_agent_state(a, "idle", "")
    while True:
        try:
            did = False
            prs = open_unjudged_prs()
            if prs:
                pr = prs[0]
                vp = tester_pass(pr)
                gate(pr, vp)
                did = True
            else:
                issues = queued_issues()
                if issues:
                    coder_pass(issues[0])
                    did = True
            if not did:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log_event("fleet_stop")
            return
        except Exception as e:  # keep the loop alive; failures become log events
            log_event("orchestrator_error", error=str(e)[:500])
            time.sleep(POLL_SECONDS)


TOKEN = subprocess.run(
    ["gh", "auth", "token", "--user", "Nodenester"],
    capture_output=True, text=True, check=True
).stdout.strip()

if __name__ == "__main__":
    main()

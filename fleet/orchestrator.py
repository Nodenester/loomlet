"""Loomlet — a view-only multi-project agent fleet manager (LOOM).

A thin, human-owned harness that manages a REGISTRY of projects (fleet/projects.json).
For each registered project it runs an independent loop in its own thread:

    PLANNER -> CODER -> TESTER -> deterministic GATE -> DEPLOY
                  ^                                  |
                  └──── failures return as issues ───┘

and keeps working toward the project's `goal` until the backlog drains and the
planner declares the goal met (bounded by `max_issues`). Agents never decide what
ships — gate() does, with a rule.

SAFETY (this is a public showcase):
  * Registry-only. The fleet ONLY ever touches repos listed in projects.json.
    There is no way to add a repo from the web; the dashboard is strictly read-only.
  * The tester only checks out PRs the fleet itself authored on `issue-<n>` branches
    in the same repo — never fork/contributor code (no untrusted code execution).
  * The gate refuses any PR that touches protected harness paths.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"                       # shared control-plane state
PROJECTS_FILE = ROOT / "fleet" / "projects.json"
PROMPTS = ROOT / "fleet" / "prompts"
MODEL = "claude-fable-5"
WORK_ROOT = Path("E:/AgentingStuff/LoomletWork")
PROTECTED = ("fleet/", "server.py", ".github/", "state/", "projects.json")
POLL_SECONDS = 20
AGENT_TIMEOUT = 1800

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_state_lock = threading.Lock()

TOKEN = subprocess.run(
    ["gh", "auth", "token", "--user", "Nodenester"],
    capture_output=True, text=True, check=True
).stdout.strip()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(project: str, kind: str, **fields):
    evt = {"ts": now(), "project": project, "kind": kind, **fields}
    with _state_lock:
        STATE.mkdir(exist_ok=True)
        with open(STATE / "activity.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    print(f"[{evt['ts']}] {project}/{kind}: "
          + json.dumps(fields, ensure_ascii=False)[:200], flush=True)


def set_agent_state(project: str, agent: str, state: str, detail: str = ""):
    with _state_lock:
        path = STATE / "agents.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data[f"{project}:{agent}"] = {"state": state, "detail": detail, "since": now()}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_project_meta(project: str, **fields):
    with _state_lock:
        path = STATE / "projects_meta.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault(project, {}).update(fields)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run(cmd, cwd=None, timeout=300, check=True):
    env = os.environ.copy()
    env["GH_TOKEN"] = TOKEN
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {cmd}\n{r.stderr[-2000:]}")
    return r


def gh_json(args, timeout=120):
    r = run(["gh"] + args, timeout=timeout)
    return json.loads(r.stdout) if r.stdout.strip() else None


def claude_agent(project: str, name: str, prompt: str, cwd: Path) -> str:
    set_agent_state(project, name, "working", prompt.split("\n", 1)[0][:120])
    log_event(project, "agent_start", agent=name)
    r = run(["claude", "-p", prompt, "--model", MODEL, "--dangerously-skip-permissions"],
            cwd=cwd, timeout=AGENT_TIMEOUT, check=False)
    out = (r.stdout or "") + ("\n[stderr] " + r.stderr[-500:] if r.returncode != 0 else "")
    log_event(project, "agent_done", agent=name, rc=r.returncode, tail=out[-300:])
    set_agent_state(project, name, "idle", "")
    return out


class Project:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.repo = cfg["repo"]
        self.goal = cfg["goal"]
        self.brief = cfg["brief"]
        self.max_issues = int(cfg.get("max_issues", 8))
        self.live_dir = Path(cfg["live_dir"])
        self.coder_clone = WORK_ROOT / self.name / "coder"
        self.tester_clone = WORK_ROOT / self.name / "tester"
        self.login = self.repo.split("/")[0].lower()

    # ---- git clones -------------------------------------------------------
    def ensure_clone(self, path: Path, agent_name: str):
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone",
                 f"https://x-access-token:{TOKEN}@github.com/{self.repo}.git", str(path)])
            run(["git", "config", "user.name", f"{self.name}-{agent_name} (Fable 5)"], cwd=path)
            run(["git", "config", "user.email",
                 f"{self.name}-{agent_name}@users.noreply.github.com"], cwd=path)
        run(["git", "fetch", "origin"], cwd=path)
        run(["git", "checkout", "main"], cwd=path)
        run(["git", "reset", "--hard", "origin/main"], cwd=path)
        run(["git", "clean", "-fd", "--exclude=state"], cwd=path)

    # ---- GitHub queries ---------------------------------------------------
    def queued_issues(self):
        return gh_json(["issue", "list", "-R", self.repo, "--label", "ai:queued",
                        "--state", "open", "--json", "number,title,body",
                        "--jq", "sort_by(.number)"]) or []

    def all_issues(self):
        return gh_json(["issue", "list", "-R", self.repo, "--state", "all", "--limit", "100",
                        "--json", "number,title,state,labels"]) or []

    def fleet_open_prs(self):
        prs = gh_json(["pr", "list", "-R", self.repo, "--state", "open",
                       "--json", "number,title,headRefName,labels,isCrossRepository,author"]) or []
        out = []
        for pr in prs:
            if pr.get("isCrossRepository"):
                continue
            if not re.fullmatch(r"issue-\d+", pr.get("headRefName", "")):
                continue
            if (pr.get("author") or {}).get("login", "").lower() != self.login:
                continue
            out.append(pr)
        return out

    def unjudged_prs(self):
        out = [p for p in self.fleet_open_prs()
               if not ({"verdict:pass", "verdict:fail"} & {l["name"] for l in p["labels"]})]
        return sorted(out, key=lambda p: p["number"])

    # ---- progress / completion -------------------------------------------
    def refresh_meta(self):
        issues = self.all_issues()
        # ignore meta/epic issues without an ai: label history; count real work items
        total = len(issues)
        done = sum(1 for i in issues if i["state"].upper() == "CLOSED")
        created = total
        set_project_meta(self.name, goal=self.goal, done=done, total=total,
                         repo=self.repo, hostname=None)
        return created

    # ---- pipeline stages --------------------------------------------------
    def code_issue(self, issue):
        n = issue["number"]
        log_event(self.name, "issue_picked", issue=n, title=issue["title"])
        run(["gh", "issue", "edit", str(n), "-R", self.repo,
             "--remove-label", "ai:queued", "--add-label", "ai:in-progress"])
        self.ensure_clone(self.coder_clone, "coder")
        prompt = self._fill(PROMPTS / "coder.md", {
            "{ISSUE_NUMBER}": str(n), "{ISSUE_TITLE}": issue["title"],
            "{ISSUE_BODY}": issue["body"] or ""})
        claude_agent(self.name, "coder", prompt, self.coder_clone)
        prs = gh_json(["pr", "list", "-R", self.repo, "--state", "open",
                       "--json", "number,body,headRefName"]) or []
        linked = [p for p in prs if f"#{n}" in (p.get("body") or "")
                  or p["headRefName"] == f"issue-{n}"]
        if linked:
            log_event(self.name, "pr_opened", issue=n, pr=linked[0]["number"])
        else:
            log_event(self.name, "coder_no_pr", issue=n)
            run(["gh", "issue", "edit", str(n), "-R", self.repo,
                 "--remove-label", "ai:in-progress", "--add-label", "ai:queued"], check=False)

    def verify_pr(self, pr):
        n = pr["number"]
        log_event(self.name, "verify_start", pr=n, title=pr["title"])
        self.ensure_clone(self.tester_clone, "tester")
        run(["gh", "pr", "checkout", str(n), "-R", self.repo], cwd=self.tester_clone, timeout=300)
        verdict_path = self.tester_clone / "state" / f"verdict-pr-{n}.json"
        verdict_path.parent.mkdir(exist_ok=True)
        if verdict_path.exists():
            verdict_path.unlink()
        prompt = self._fill(PROMPTS / "tester.md", {
            "{PR_NUMBER}": str(n), "{VERDICT_PATH}": str(verdict_path)})
        claude_agent(self.name, "tester", prompt, self.tester_clone)
        return verdict_path

    def gate(self, pr, verdict_path: Path) -> bool:
        n = pr["number"]
        reasons, verdict = [], None
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
                    reasons.append(f"failed: {it.get('item','?')[:80]}")
                if not str(it.get("evidence", "")).strip():
                    reasons.append(f"empty evidence: {it.get('item','?')[:80]}")
            if not verdict.get("all_pass"):
                reasons.append("verdict all_pass is false")
        files = gh_json(["pr", "view", str(n), "-R", self.repo, "--json", "files",
                         "--jq", "[.files[].path]"]) or []
        touched = [f for f in files if any(f.startswith(p) for p in PROTECTED)]
        if touched:
            reasons.append(f"touches protected paths: {touched}")
        if reasons:
            log_event(self.name, "gate_red", pr=n, reasons=reasons)
            run(["gh", "pr", "edit", str(n), "-R", self.repo, "--add-label", "verdict:fail"], check=False)
            run(["gh", "pr", "comment", str(n), "-R", self.repo, "--body",
                 "GATE RED (deterministic):\n- " + "\n- ".join(reasons)], check=False)
            return False
        run(["gh", "pr", "edit", str(n), "-R", self.repo, "--add-label", "verdict:pass"], check=False)
        run(["gh", "pr", "comment", str(n), "-R", self.repo, "--body",
             "GATE GREEN (deterministic): all checklist items passed with evidence, "
             "no protected paths touched. Merging."], check=False)
        run(["gh", "pr", "merge", str(n), "-R", self.repo, "--merge", "--delete-branch"], timeout=300)
        log_event(self.name, "gate_green_merged", pr=n)
        self.deploy()
        return True

    def deploy(self):
        self.live_dir.mkdir(parents=True, exist_ok=True)
        if not (self.live_dir / ".git").exists():
            run(["git", "clone",
                 f"https://x-access-token:{TOKEN}@github.com/{self.repo}.git", str(self.live_dir)])
        run(["git", "fetch", "origin"], cwd=self.live_dir)
        run(["git", "checkout", "main"], cwd=self.live_dir)
        run(["git", "reset", "--hard", "origin/main"], cwd=self.live_dir)
        log_event(self.name, "deployed", target=self.name)

    def plan_next(self) -> bool:
        """Ask the planner for the next issue toward the goal, or declare complete.
        Returns True if it created an issue (more work), False if goal complete."""
        issues = self.all_issues()
        created = len(issues)
        if created >= self.max_issues:
            log_event(self.name, "project_complete", reason=f"issue cap {self.max_issues} reached")
            set_project_meta(self.name, complete=True)
            return False
        log_event(self.name, "planner_start", created=created, cap=self.max_issues)
        self.ensure_clone(self.coder_clone, "coder")
        history = "\n".join(f"#{i['number']} [{i['state']}] {i['title']}" for i in issues) or "(none yet)"
        prompt = self._fill(PROMPTS / "planner.md", {
            "{HISTORY}": history, "{CREATED}": str(created), "{CAP}": str(self.max_issues)})
        out = claude_agent(self.name, "planner", prompt, self.coder_clone)
        if "GOAL COMPLETE" in out.upper():
            log_event(self.name, "project_complete", reason="planner declared goal met")
            set_project_meta(self.name, complete=True)
            return False
        # planner created the issue itself via gh; confirm a new ai:queued exists
        if self.queued_issues():
            return True
        log_event(self.name, "planner_no_issue")
        set_project_meta(self.name, complete=True)
        return False

    def reconcile(self):
        """Re-queue issues stuck in ai:in-progress with no open fleet PR (orphans
        from a previous run that was killed mid-flight)."""
        stuck = gh_json(["issue", "list", "-R", self.repo, "--label", "ai:in-progress",
                         "--state", "open", "--json", "number"]) or []
        if not stuck:
            return
        pr_branches = {p["headRefName"] for p in self.fleet_open_prs()}
        for it in stuck:
            n = it["number"]
            if f"issue-{n}" not in pr_branches:
                run(["gh", "issue", "edit", str(n), "-R", self.repo,
                     "--remove-label", "ai:in-progress", "--add-label", "ai:queued"], check=False)
                log_event(self.name, "requeued_orphan", issue=n)

    def _fill(self, path: Path, extra: dict) -> str:
        text = path.read_text(encoding="utf-8")
        base = {"{REPO}": self.repo, "{PROJECT}": self.name,
                "{GOAL}": self.goal, "{PROJECT_BRIEF}": self.brief}
        base.update(extra)
        for k, v in base.items():
            text = text.replace(k, v)
        return text

    # ---- the loop ---------------------------------------------------------
    def loop(self):
        set_agent_state(self.name, "coder", "idle", "")
        set_agent_state(self.name, "tester", "idle", "")
        self.reconcile()
        complete = False
        while True:
            try:
                self.refresh_meta()
                prs = self.unjudged_prs()
                if prs:
                    vp = self.verify_pr(prs[0])
                    self.gate(prs[0], vp)
                    continue
                issues = self.queued_issues()
                if issues:
                    self.code_issue(issues[0])
                    continue
                if not complete and not self.fleet_open_prs():
                    more = self.plan_next()
                    if not more:
                        complete = True
                    continue
                time.sleep(POLL_SECONDS)
            except Exception as e:
                log_event(self.name, "orchestrator_error", error=str(e)[:500])
                time.sleep(POLL_SECONDS)


def main():
    cfgs = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    log_event("fleet", "fleet_start", model=MODEL,
              projects=[c["name"] for c in cfgs])
    threads = []
    for cfg in cfgs:
        p = Project(cfg)
        t = threading.Thread(target=p.loop, name=p.name, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(2)  # stagger startup so the log reads clearly
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        log_event("fleet", "fleet_stop")


if __name__ == "__main__":
    main()

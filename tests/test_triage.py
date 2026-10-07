"""End-to-end tests for triage.py against fake gutsy and GitHub servers.

Run: python tests/test_triage.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LABELS = [
    {"name": "bug", "description": "Something isn't working"},
    {"name": "enhancement", "description": "New feature or request"},
    {"name": "documentation", "description": "Improvements or additions to documentation"},
    {"name": "question", "description": "Further information is requested"},
    {"name": "duplicate", "description": "This issue or pull request already exists"},
    {"name": "needs-triage", "description": ""},
]


def make_issues():
    return {
        1: {"number": 1, "title": "App crashes on startup", "body": "Traceback ... segfault", "labels": []},
        2: {"number": 2, "title": "Please add dark mode", "body": "It would be nice", "labels": []},
        3: {"number": 3, "title": "hmm", "body": "", "labels": []},
        4: {"number": 4, "title": "Crash when saving", "body": "crash", "labels": [{"name": "bug"}]},
        5: {"number": 5, "title": "Typo in README", "body": "docs", "labels": [{"name": "documentation"}]},
        6: {"number": 6, "title": "PR", "body": "", "labels": [], "pull_request": {}},
    }


class World:
    def __init__(self):
        self.issues = make_issues()
        self.comments = {}
        self.writes = []
        self.gutsy_requests = []


def fake_answer(state, questions):
    s = state.lower()
    if "crash" in s:
        probs, reject = {"bug": 0.9, "enhancement": 0.04, "documentation": 0.03, "question": 0.03}, 0.03
    elif "dark mode" in s or "add" in s:
        probs, reject = {"bug": 0.1, "enhancement": 0.8, "documentation": 0.05, "question": 0.05}, 0.05
    elif "readme" in s:
        probs, reject = {"bug": 0.5, "enhancement": 0.1, "documentation": 0.3, "question": 0.1}, 0.1
    else:
        probs, reject = {"bug": 0.3, "enhancement": 0.3, "documentation": 0.2, "question": 0.2}, 0.4
    names = list(questions["label"]["criteria"])
    probs = {n: probs.get(n, 0.0) for n in names}
    total = sum(probs.values()) or 1
    probs = {n: p / total for n, p in probs.items()}
    answers = {"label": {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs,
                         "confidence": 0.5, "margin": 0.5, "reject": reject}}
    for qid, q in questions.items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.95 if '"documentation"' in q["instructions"] and "crash" in s else 0.1}
    return {"model": "fake", "answers": answers, "usage": {"latency_ms": 5}}


def handler_for(world):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj, link=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if link:
                self.send_header("Link", link)
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/health":
                return self.send(200, {"ok": True})
            if path == "/repos/o/r/labels":
                return self.send(200, LABELS)
            if path == "/repos/o/r/issues":
                return self.send(200, list(reversed(list(world.issues.values()))))
            if path.startswith("/repos/o/r/issues/") and path.endswith("/comments"):
                n = int(path.split("/")[5])
                return self.send(200, world.comments.get(n, []))
            if path.startswith("/repos/o/r/issues/"):
                return self.send(200, world.issues[int(path.split("/")[-1])])
            self.send(404, {"message": "not found"})

        def do_POST(self):
            data = self.body()
            path = self.path
            if path == "/v1/systemone":
                world.gutsy_requests.append(data)
                return self.send(200, fake_answer(data["state"], data["questions"]))
            world.writes.append(("POST", path, data))
            if path.endswith("/labels"):
                n = int(path.split("/")[5])
                world.issues[n]["labels"] += [{"name": x} for x in data["labels"]]
                return self.send(200, [])
            if path.endswith("/comments"):
                n = int(path.split("/")[5])
                c = {"id": 100 + n, "body": data["body"]}
                world.comments.setdefault(n, []).append(c)
                return self.send(201, c)
            self.send(404, {})

        def do_PATCH(self):
            data = self.body()
            world.writes.append(("PATCH", self.path, data))
            cid = int(self.path.split("/")[-1])
            for cs in world.comments.values():
                for c in cs:
                    if c["id"] == cid:
                        c["body"] = data["body"]
            self.send(200, {})

    return H


def run(world, env_extra, event=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(world))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory() as tmp:
        out, summ, ev = (os.path.join(tmp, f) for f in ("out", "summary", "event.json"))
        open(out, "w").close()
        json.dump(event or {}, open(ev, "w"))
        env = {**os.environ, "GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_API_URL": url,
               "GUTSY_URL": url, "GITHUB_OUTPUT": out, "GITHUB_STEP_SUMMARY": summ, "GITHUB_EVENT_PATH": ev,
               "GT_EXCLUDE_LABELS": "duplicate\nwontfix\ninvalid\ngood first issue\nhelp wanted",
               "GT_THRESHOLD": "0.6", "GT_COMMENT": "labeled", **env_extra}
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "triage.py")], env=env, capture_output=True, text=True)
        outputs = dict(line.split("=", 1) for line in open(out).read().splitlines() if "=" in line)
        summary_text = open(summ).read() if os.path.exists(summ) else ""
    server.shutdown()
    return proc, outputs, summary_text


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok  {msg}")


def test_event_issue_labeled_and_commented():
    w = World()
    proc, out, _ = run(w, {}, {"issue": w.issues[1]})
    check(proc.returncode == 0, f"exit 0 ({proc.stderr[-300:]})")
    check(out["label"] == "bug", "applies bug")
    check(abs(float(out["probability"]) - 0.873) < 0.001, "probability is p * (1 - reject)")
    check(("POST", "/repos/o/r/issues/1/labels", {"labels": ["bug"]}) in w.writes, "label POSTed")
    body = w.comments[1][0]["body"]
    check("Labeled **`bug`** (87%)" in body and "<!-- gutsy-triage -->" in body, "visible comment with marker")
    q = w.gutsy_requests[0]["questions"]["label"]["criteria"]
    check("duplicate" not in q and "needs-triage" in q, "default exclusions applied, others kept")


def test_low_confidence_fallback_no_comment():
    w = World()
    proc, out, _ = run(w, {"GT_FALLBACK_LABEL": "needs-triage"}, {"issue": w.issues[3]})
    check(proc.returncode == 0, "exit 0")
    check(("POST", "/repos/o/r/issues/3/labels", {"labels": ["needs-triage"]}) in w.writes, "fallback label applied")
    check(3 not in w.comments, "no comment when only the fallback was applied")
    check(out["label"] == "", "label output empty")
    q = w.gutsy_requests[0]["questions"]["label"]["criteria"]
    check("needs-triage" not in q, "fallback label is never a candidate")


def test_skip_already_labeled_and_prs():
    w = World()
    proc, out, _ = run(w, {"GT_ISSUES": "4, #6"})
    res = json.loads(out["results"])
    check([r["status"] for r in res] == ["skipped: already labeled", "skipped: pull request"], "skips labeled issue and PR")
    check(not w.writes and not w.gutsy_requests, "no model calls, no writes")


def test_dry_run_batch_unlabeled():
    w = World()
    proc, out, summ = run(w, {"GT_ISSUES": "unlabeled", "GT_DRY_RUN": "true"})
    res = json.loads(out["results"])
    check(proc.returncode == 0, "exit 0")
    check(sorted(r["issue"] for r in res) == [1, 2, 3], "picks open unlabeled issues, not PRs")
    check(not w.writes, "dry run writes nothing")
    check("| #2 |" in summ and "`enhancement` 76%" in summ, "step summary table")


def test_comment_update_not_duplicate():
    w = World()
    w.comments[1] = [{"id": 555, "body": "<!-- gutsy-triage -->\nold"}]
    proc, _, _ = run(w, {"GT_COMMENT": "always", "GT_SKIP_IF_LABELED": "false"}, {"issue": w.issues[1]})
    check(len(w.comments[1]) == 1 and "Labeled" in w.comments[1][0]["body"], "existing comment updated in place")


def test_multi_label():
    w = World()
    proc, out, _ = run(w, {"GT_MULTI_LABEL": "true"}, {"issue": w.issues[1]})
    res = json.loads(out["results"])[0]
    check([a["label"] for a in res["applied"]] == ["bug", "documentation"], "extra label from yes/no question")


def test_explicit_labels_with_descriptions():
    w = World()
    proc, _, _ = run(w, {"GT_LABELS": "bug\nenhancement = asks for something new\nquestion"}, {"issue": w.issues[2]})
    crit = w.gutsy_requests[0]["questions"]["label"]["criteria"]
    check(crit == {"bug": "Something isn't working", "enhancement": "asks for something new",
                   "question": "Further information is requested"}, "explicit list, repo descriptions filled in")


def test_evaluate():
    w = World()
    proc, out, summ = run(w, {"GT_EVALUATE": "10"})
    check(proc.returncode == 0, f"exit 0 ({proc.stderr[-300:]})")
    check(not w.writes, "evaluate writes nothing")
    check("| 0.0 | 2 (100%) | 50% |" in summ, "accuracy/coverage table")
    check("Misses" in summ and "#5" in summ, "lists misses")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nall {len(tests)} tests passed")

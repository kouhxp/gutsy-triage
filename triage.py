#!/usr/bin/env python3
"""gutsy-triage: label GitHub issues with a local gutsy decision model.

Standard library only. Talks to a gutsy-inference server (started by action.yml) and to the
GitHub REST API with the workflow token. The model only returns probabilities over the
candidate labels, so issue text can't make it apply anything outside that set.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MARKER = "<!-- gutsy-triage -->"
HOME = "https://github.com/kouhxp/gutsy-triage"
GUTSY_URL = os.environ.get("GUTSY_URL", "http://127.0.0.1:8765").rstrip("/")
API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
MAX_BODY_CHARS = 6000  # keeps state + question well inside the 8,192-token context
MAX_YES_NO = 63  # 64 questions per request, one is the choice question
EVAL_THRESHOLDS = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


# ---------------------------------------------------------------- inputs

def opt(name, default=""):
    value = os.environ.get("GT_" + name.upper().replace("-", "_"), "").strip()
    return value or default


def flag(name, default=False):
    value = opt(name).lower()
    return default if not value else value in ("1", "true", "yes", "on")


def number(name, default, cast=float):
    try:
        return cast(opt(name, str(default)))
    except ValueError:
        sys.exit(f"::error::input '{name}' must be a number, got '{opt(name)}'")


def lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


# ---------------------------------------------------------------- http

def http(method, url, body=None, headers=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return (json.loads(raw) if raw else None), resp.headers.get("Link", "")


class GitHub:
    def __init__(self, token, repo):
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gutsy-triage",
        }

    def call(self, method, path, body=None):
        url = path if path.startswith("http") else f"{API_URL}/repos/{self.repo}{path}"
        try:
            return http(method, url, body, self.headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            hint = " (does the workflow grant `permissions: issues: write`?)" if e.code in (403, 404) else ""
            raise RuntimeError(f"GitHub {method} {path} -> {e.code}{hint}: {detail}") from None

    def get(self, path):
        return self.call("GET", path)[0]

    def iterate(self, path):
        url = f"{path}{'&' if '?' in path else '?'}per_page=100"
        while url:
            page, link = self.call("GET", url)
            yield from page
            match = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            url = match.group(1) if match else None


# ---------------------------------------------------------------- labels

def candidate_labels(gh):
    """Return {name: description} for the labels gutsy may choose from."""
    raw = opt("labels")
    repo_labels = None
    if raw.startswith("{"):
        labels = {str(k): str(v or "") for k, v in json.loads(raw).items()}
    elif raw:
        labels = {}
        for line in lines(raw):
            name, _, desc = line.partition(" = ")
            labels[name.strip()] = desc.strip()
    else:
        repo_labels = {lb["name"]: lb.get("description") or "" for lb in gh.iterate("/labels")}
        labels = dict(repo_labels)

    if any(not d for d in labels.values()):
        if repo_labels is None:
            repo_labels = {lb["name"]: lb.get("description") or "" for lb in gh.iterate("/labels")}
        by_lower = {k.lower(): v for k, v in repo_labels.items()}
        labels = {n: d or by_lower.get(n.lower(), "") for n, d in labels.items()}

    excluded = {x.lower() for x in lines(opt("exclude-labels"))}
    if opt("fallback-label"):
        excluded.add(opt("fallback-label").lower())
    labels = {n: d for n, d in labels.items() if n.lower() not in excluded}

    if len(labels) < 2:
        sys.exit("::error::need at least two candidate labels; set `labels:` or create labels in the repo")
    if len(labels) > 255:
        sys.exit(f"::error::gutsy handles at most 255 options; got {len(labels)} labels. Narrow them with `labels:`")
    return labels


def has_candidate_label(issue, labels):
    wanted = {n.lower() for n in labels}
    return any(lb["name"].lower() in wanted for lb in issue.get("labels", []))


# ---------------------------------------------------------------- model

def issue_state(issue, repo):
    body = re.sub(r"<!--.*?-->", "", issue.get("body") or "", flags=re.S).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[...truncated]"
    return (
        f"An issue filed on the GitHub repository {repo}.\n\n"
        f"Title: {issue.get('title', '').strip()}\n\n"
        f"{body or '(no description)'}"
    )


def classify(state, labels, multi):
    names = list(labels)
    questions = {
        "label": {
            "type": "choice",
            "instructions": "Which label fits this issue best?",
            "criteria": {n: labels[n] or n for n in names},
        }
    }
    yes_no = names[:MAX_YES_NO] if multi else []
    for i, name in enumerate(yes_no):
        desc = f" ({labels[name]})" if labels[name] else ""
        questions[f"l{i}"] = {"type": "noul", "instructions": f'Should this issue get the label "{name}"{desc}?'}

    started = time.time()
    try:
        resp, _ = http("POST", f"{GUTSY_URL}/v1/systemone", {"state": state, "questions": questions}, timeout=600)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"gutsy returned {e.code}: {e.read().decode(errors='replace')[:300]}") from None

    answer = resp["answers"]["label"]
    reject = float(answer.get("reject") or 0.0)
    # The option probabilities are conditioned on "not reject"; scale them back so each
    # number is the model's calibrated probability that this label is the right one.
    probs = {n: float(p) * (1.0 - reject) for n, p in answer["probabilities"].items()}
    yes = {n: float(resp["answers"][f"l{i}"]["noul"]) for i, n in enumerate(yes_no)}
    return {
        "probabilities": probs,
        "reject": reject,
        "yes": yes,
        "seconds": round(time.time() - started, 2),
        "usage": resp.get("usage", {}),
    }


def decide(result, threshold, multi_threshold):
    """Return [(label, probability)] to apply, best first."""
    probs = result["probabilities"]
    top = max(probs, key=probs.get)
    chosen = [(top, probs[top])] if probs[top] >= threshold else []
    extra = sorted(result["yes"].items(), key=lambda kv: -kv[1])
    for name, p in extra:
        if p >= multi_threshold and name not in dict(chosen):
            chosen.append((name, p))
    return chosen


# ---------------------------------------------------------------- output

def pct(p):
    return f"{round(p * 100)}%"


def comment_body(chosen, result, fallback):
    ranked = sorted(result["probabilities"].items(), key=lambda kv: -kv[1])
    if chosen:
        picked = ", ".join(f"**`{n}`** ({pct(p)})" for n, p in chosen)
        head = f"🏷️ Labeled {picked} by [gutsy]({HOME})"
    else:
        best, p = ranked[0]
        head = f"🤔 [gutsy]({HOME}) wasn't confident enough to pick a label (best guess `{best}`, {pct(p)})"
        if fallback:
            head += f" and added `{fallback}` for a human to look at"
        head += "."

    table = ["| label | probability |", "|---|---|"]
    table += [f"| `{n}` | {pct(p)} |" for n, p in ranked]
    table.append(f"| *none of these* | {pct(result['reject'])} |")
    details = "\n".join(table)
    if result["yes"]:
        yes_rows = ["", "| also applies? | yes |", "|---|---|"]
        yes_rows += [f"| `{n}` | {pct(p)} |" for n, p in sorted(result["yes"].items(), key=lambda kv: -kv[1])]
        details += "\n" + "\n".join(yes_rows)

    return (
        f"{MARKER}\n{head}\n\n"
        f"<details><summary>All probabilities</summary>\n\n{details}\n\n</details>\n\n"
        "<sub>Calibrated probabilities from a 0.8B model running on this repo's Actions runner: "
        "no API key, and the issue text never left GitHub. Wrong label? Just change it.</sub>"
    )


def upsert_comment(gh, number, body):
    for existing in gh.iterate(f"/issues/{number}/comments"):
        if MARKER in (existing.get("body") or ""):
            gh.call("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
            return
    gh.call("POST", f"/issues/{number}/comments", {"body": body})


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def summary(markdown):
    print(markdown)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown + "\n")


def md_cell(text, limit=70):
    text = (text or "").replace("|", "\\|").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- modes

def triage(gh, issue, labels, cfg):
    number = issue["number"]
    record = {"issue": number, "title": issue.get("title", ""), "applied": [], "status": ""}
    if "pull_request" in issue:
        record["status"] = "skipped: pull request"
        return record
    if cfg["skip_if_labeled"] and has_candidate_label(issue, labels):
        record["status"] = "skipped: already labeled"
        return record

    result = classify(issue_state(issue, gh.repo), labels, cfg["multi"])
    chosen = decide(result, cfg["threshold"], cfg["multi_threshold"])
    top = max(result["probabilities"], key=result["probabilities"].get)
    record.update(
        top=top,
        probability=round(result["probabilities"][top], 4),
        reject=round(result["reject"], 4),
        probabilities={k: round(v, 4) for k, v in result["probabilities"].items()},
        seconds=result["seconds"],
        applied=[{"label": n, "probability": round(p, 4)} for n, p in chosen],
    )
    to_apply = [n for n, _ in chosen] or ([cfg["fallback"]] if cfg["fallback"] else [])
    record["status"] = "labeled" if chosen else ("fallback" if to_apply else "below threshold")

    if cfg["dry_run"]:
        record["status"] += " (dry run)"
        return record
    if to_apply:
        gh.call("POST", f"/issues/{number}/labels", {"labels": to_apply})
    if cfg["comment"] == "always" or (cfg["comment"] == "labeled" and chosen):
        upsert_comment(gh, number, comment_body(chosen, result, cfg["fallback"]))
    return record


def select_issues(gh, labels, cfg):
    spec = opt("issues")
    if not spec:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        event = json.load(open(event_path)) if event_path and os.path.exists(event_path) else {}
        if "issue" not in event:
            sys.exit("::error::no issue in the triggering event; set `issues:` to `unlabeled` or a list of numbers")
        return [event["issue"]]
    if spec.lower() == "unlabeled":
        picked = []
        for issue in gh.iterate("/issues?state=open&sort=created&direction=desc"):
            if "pull_request" not in issue and not has_candidate_label(issue, labels):
                picked.append(issue)
                if len(picked) >= cfg["limit"]:
                    break
        return picked
    numbers = [n.strip().lstrip("#") for n in re.split(r"[,\s]+", spec) if n.strip()]
    return [gh.get(f"/issues/{n}") for n in numbers]


def run_triage(gh, labels, cfg):
    issues = select_issues(gh, labels, cfg)
    print(f"Triaging {len(issues)} issue(s) against {len(labels)} labels: {', '.join(labels)}")
    records = []
    for issue in issues:
        print(f"::group::#{issue['number']} {issue.get('title', '')}")
        try:
            record = triage(gh, issue, labels, cfg)
        except Exception as e:  # keep going in batch mode
            record = {"issue": issue["number"], "title": issue.get("title", ""), "applied": [], "status": f"error: {e}"}
            print(f"::warning::#{issue['number']}: {e}")
        print(json.dumps(record, indent=2))
        print("::endgroup::")
        records.append(record)

    rows = ["## gutsy triage", "", "| issue | title | applied | best guess | time |", "|---|---|---|---|---|"]
    for r in records:
        applied = ", ".join(f"`{a['label']}` {pct(a['probability'])}" for a in r["applied"]) or r["status"]
        guess = f"`{r['top']}` {pct(r['probability'])}" if "top" in r else "—"
        secs = f"{r['seconds']} s" if "seconds" in r else "—"
        rows.append(f"| #{r['issue']} | {md_cell(r['title'])} | {applied} | {guess} | {secs} |")
    summary("\n".join(rows))

    first = records[0] if len(records) == 1 and records[0]["applied"] else None
    set_output("label", first["applied"][0]["label"] if first else "")
    set_output("probability", first["applied"][0]["probability"] if first else "")
    set_output("results", json.dumps(records, separators=(",", ":")))
    if any(r["status"].startswith("error") for r in records):
        sys.exit(1)


def run_evaluate(gh, labels, cfg, wanted):
    """Score already-labeled issues; report accuracy and coverage per threshold. Writes nothing."""
    by_lower = {n.lower(): n for n in labels}
    samples = []
    for issue in gh.iterate("/issues?state=all&sort=created&direction=desc"):
        if "pull_request" in issue:
            continue
        truth = {by_lower[lb["name"].lower()] for lb in issue.get("labels", []) if lb["name"].lower() in by_lower}
        if truth:
            samples.append((issue, truth))
            if len(samples) >= wanted:
                break
    if not samples:
        sys.exit("::error::no issues carry any of the candidate labels, so there is nothing to evaluate against")

    print(f"Evaluating on {len(samples)} labeled issues")
    scored, misses, total_secs = [], [], 0.0
    for i, (issue, truth) in enumerate(samples, 1):
        result = classify(issue_state(issue, gh.repo), labels, False)
        probs = result["probabilities"]
        top = max(probs, key=probs.get)
        ok = top in truth
        total_secs += result["seconds"]
        scored.append((probs[top], ok))
        if not ok:
            misses.append((issue, top, probs[top], truth))
        print(f"[{i}/{len(samples)}] #{issue['number']}: {'✓' if ok else '✗'} {top} {pct(probs[top])} (truth: {', '.join(sorted(truth))})")

    n = len(scored)
    rows = [
        "## gutsy evaluation",
        "",
        f"{n} recent issues that already carry a candidate label; a prediction counts as correct when "
        f"gutsy's top label is one of the issue's labels. Mean model time {total_secs / n:.1f} s per issue.",
        "",
        "| threshold | issues labeled (coverage) | accuracy on those |",
        "|---|---|---|",
    ]
    suggestion = None
    for t in EVAL_THRESHOLDS:
        covered = [ok for p, ok in scored if p >= t]
        acc = sum(covered) / len(covered) if covered else 0.0
        rows.append(f"| {t:.1f} | {len(covered)} ({pct(len(covered) / n)}) | {pct(acc) if covered else '—'} |")
        if suggestion is None and len(covered) >= 5 and acc >= 0.9:
            suggestion = t
    rows.append("")
    if suggestion is not None:
        rows.append(f"**Suggested `threshold: \"{suggestion}\"`** (lowest threshold with ≥90% accuracy on at least 5 issues).")
    else:
        rows.append("No threshold reached 90% accuracy here. Tighten `labels:` or add clearer label descriptions, then re-run.")
    if misses:
        rows += ["", "<details><summary>Misses</summary>", "", "| issue | predicted | actual |", "|---|---|---|"]
        for issue, top, p, truth in misses[:25]:
            rows.append(f"| #{issue['number']} {md_cell(issue.get('title'), 50)} | `{top}` {pct(p)} | {', '.join(f'`{t}`' for t in sorted(truth))} |")
        rows += ["", "</details>"]
    rows += ["", "<sub>Labels that gutsy itself applied count as ground truth here, so evaluate before you enable it, or expect optimistic numbers.</sub>"]
    summary("\n".join(rows))
    set_output("results", json.dumps({"n": n, "suggested_threshold": suggestion}, separators=(",", ":")))


def main():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        sys.exit("::error::GITHUB_TOKEN and GITHUB_REPOSITORY must be set")
    gh = GitHub(token, repo)

    cfg = {
        "threshold": number("threshold", 0.6),
        "multi": flag("multi-label"),
        "multi_threshold": number("multi-threshold", 0.85),
        "fallback": opt("fallback-label"),
        "skip_if_labeled": flag("skip-if-labeled", True),
        "comment": opt("comment", "labeled").lower(),
        "dry_run": flag("dry-run"),
        "limit": number("limit", 50, int),
    }
    if cfg["comment"] not in ("labeled", "always", "never"):
        sys.exit("::error::`comment` must be labeled, always or never")

    labels = candidate_labels(gh)
    evaluate = number("evaluate", 0, int)
    if evaluate > 0:
        run_evaluate(gh, labels, cfg, evaluate)
    else:
        run_triage(gh, labels, cfg)


if __name__ == "__main__":
    main()

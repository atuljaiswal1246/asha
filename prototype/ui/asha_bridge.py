import json, time, urllib.request, base64, os, sys

# Asha bridge (coordinator = brain's watcher, NOT the doer).
# Correct loop per the operating model:
#   1. BRAIN (free-first supervisor) writes the brief  -> supervisor.write_brief
#   2. FREE WORKER executes on :4096                   -> serve API
#   3. BRAIN (free-first supervisor) reviews the diff  -> supervisor.review_diff
#   4. COORDINATOR (this script) only verifies + commits when review passes
# The coordinator never writes briefs and never judges diffs.

ROOT = "/Users/neharohilla/Desktop/Jarvis"
PASS = open(os.path.join(ROOT, "prototype/.env")).read().split("OPENCODE_SERVER_PASSWORD=")[1].splitlines()[0]

def serve(url, payload=None, method="GET", timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(("opencode:" + PASS).encode()).decode(),
    })
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:300]}

def brain_brief(task_text):
    """Step 1: the free brain writes a scoped brief for the worker."""
    import subprocess
    env = dict(os.environ)
    with open(os.path.join(ROOT, "prototype/.env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k, v)
    env["PYTHONPATH"] = os.path.join(ROOT, "prototype/ui")
    py = os.path.join(ROOT, ".venv/bin/python")
    code = (
        "import supervisor as sv; "
        "r = sv.write_brief(open('/tmp/_brain_brief_input.txt').read(), timeout=120, attempts=2); "
        "import json; "
        "print(json.dumps({'brief': r.get('brief') or '', 'plan': r.get('plan') or [], "
        "'model': r['model'], 'cost': r['cost'], 'endpoint': r['endpoint']}))"
    )
    open("/tmp/_brain_brief_input.txt", "w").write(task_text)
    r = subprocess.run([py, "-c", code], env=env, capture_output=True, text=True, timeout=180)
    out = r.stdout.strip().splitlines()
    for line in reversed(out):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    raise RuntimeError(f"brain brief failed: {r.stderr[-500:]}")

def dispatch(issue_text, model="big-pickle", agent="build"):
    """Step 2: free worker executes the brief on :4096, poll until done."""
    s = serve("http://127.0.0.1:4096/api/session", {"agent": agent, "model": {"providerID": "opencode", "id": model}}, "POST")
    if "error" in s or "data" not in s:
        return {"error": s}
    sid = s["data"]["id"]
    p = serve(f"http://127.0.0.1:4096/api/session/{sid}/prompt", {"prompt": {"text": issue_text}}, "POST")
    if "error" in p:
        return {"error": p, "session": sid}
    print(f"[bridge] dispatched to {model} session={sid}", flush=True)
    for i in range(120):
        time.sleep(10)
        msgs = serve(f"http://127.0.0.1:4096/api/session/{sid}/message")
        if "error" in msgs:
            continue
        for m in msgs.get("data", []):
            if m.get("type") == "assistant" and m.get("finish"):
                text = ""
                for c in m.get("content", []):
                    if c.get("type") == "text":
                        text += c.get("text", "")
                return {"session": sid, "finish": m.get("finish"), "text": text,
                        "model": m.get("model", {}).get("id"), "cost": m.get("cost")}
        if i % 6 == 0:
            print(f"[bridge] ...waiting ({(i+1)*10}s)", flush=True)
    return {"session": sid, "finish": "timeout"}

def brain_review(diff_text, brief_text):
    """Step 3: the free brain judges the worker's diff against the brief."""
    import subprocess
    env = dict(os.environ)
    with open(os.path.join(ROOT, "prototype/.env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k, v)
    env["PYTHONPATH"] = os.path.join(ROOT, "prototype/ui")
    py = os.path.join(ROOT, ".venv/bin/python")
    open("/tmp/_brain_review_diff.txt", "w").write(diff_text)
    open("/tmp/_brain_review_brief.txt", "w").write(brief_text)
    code = (
        "import supervisor as sv; "
        "r = sv.review_diff(open('/tmp/_brain_review_diff.txt').read(), "
        "open('/tmp/_brain_review_brief.txt').read(), timeout=120, attempts=2); "
        "import json; print(json.dumps({k: r.get(k) for k in ('verdict','approved','issues','notes')}))"
    )
    r = subprocess.run([py, "-c", code], env=env, capture_output=True, text=True, timeout=180)
    out = r.stdout.strip().splitlines()
    for line in reversed(out):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    raise RuntimeError(f"brain review failed: {r.stderr[-500:]}")

if __name__ == "__main__":
    task_file = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "big-pickle"
    task_text = open(task_file).read()

    print("=== STEP 1: BRAIN writes the brief (free-first) ===")
    brief = brain_brief(task_text)
    print(f"  brain={brief['model']} cost=${brief['cost']:.5f}")
    print(f"  brief: {(brief['brief'] or '')[:300]}")

    worker_text = (
        "You are a coding worker in the Asha repo (/Users/neharohilla/Desktop/Jarvis). "
        "Execute the brief below. Use file tools + bash (curl, playwright at /tmp/pwdiag). "
        "Do NOT edit .env. Do NOT commit — the coordinator verifies and commits.\n\n"
        "BRIEF:\n" + (brief["brief"] or "") + "\n\nPLAN:\n" + json.dumps(brief.get("plan") or [])
    )
    open("/tmp/_worker_brief.md", "w").write(worker_text)

    print("=== STEP 2: FREE WORKER executes on :4096 ===")
    result = dispatch(worker_text, model)
    print(f"  finish={result.get('finish')} cost={result.get('cost')}")
    print("  worker output tail:", (result.get("text") or "")[-300:])

    print("=== STEP 3: BRAIN reviews the diff ===")
    import subprocess
    diff = subprocess.run(["git", "-C", ROOT, "diff"], capture_output=True, text=True).stdout
    if not diff.strip():
        print("  NOTE: no working-tree diff found; worker may not have changed files or may have stalled.")
    review = brain_review(diff[:20000] or "no diff", brief.get("brief") or "")
    print(f"  review: {json.dumps(review)[:400]}")

    print("=== COORDINATOR (watcher) decision ===")
    approved = review.get("approved")
    if approved is True:
        print("  BRAIN APPROVED — coordinator verifies + commits.")
    else:
        print("  BRAIN REJECTED — do NOT commit. Investigate worker output / brief.")
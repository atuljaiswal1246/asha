import json, time, urllib.request, base64, os, sys
ROOT = "/Users/neharohilla/Desktop/Jarvis"
PASS = open(os.path.join(ROOT, "prototype/.env")).read().split("OPENCODE_SERVER_PASSWORD=")[1].splitlines()[0]
sid = sys.argv[1]
def serve(url):
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(("opencode:" + PASS).encode()).decode()})
    try: return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    except Exception: return {}
for i in range(120):
    time.sleep(10)
    msgs = serve(f"http://127.0.0.1:4096/api/session/{sid}/message")
    if "error" in msgs: continue
    for m in msgs.get("data", []):
        if m.get("type") == "assistant" and m.get("finish"):
            text = "".join(c.get("text","") for c in m.get("content",[]) if c.get("type")=="text")
            print(f"[watch] FINISH={m.get('finish')} cost={m.get('cost')} model={m.get('model',{}).get('id')}", flush=True)
            print(text[:3000], flush=True)
            sys.exit(0)
    if i % 6 == 0: print(f"[watch] ...{(i+1)*10}s", flush=True)
print("[watch] TIMEOUT")

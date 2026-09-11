import json, urllib.request
BASE="http://127.0.0.1:8000"
def get(path):
    with urllib.request.urlopen(BASE+path, timeout=60) as r:
        return json.loads(r.read().decode())
ss=get("/sessions")
print("sessions:", len(ss))
for s in ss:
    sid=s["session_id"]
    print("==", sid, s["email"], s["status"])
    try:
        st=get(f"/email/status?session_id={sid}")
        print(json.dumps(st, indent=1)[:3000])
    except Exception as e:
        print("status erro:", e)
print("events tail:")
ev=get("/email/events?limit=10")
print(json.dumps(ev, indent=1)[:5000])

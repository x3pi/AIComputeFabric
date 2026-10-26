import json, sys

req = json.load(sys.stdin)
t = req["text"]
print(json.dumps({"chars": len(t), "words": len(t.split()), "lines": t.count("\n") + 1}))

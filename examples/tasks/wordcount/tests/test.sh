#!/bin/sh
mkdir -p /logs/verifier
python3 - <<'PY'
import sys
sys.path.insert(0, "/app")
ok = False
detail = ""
try:
    from solution import top_words
    cases = [
        ("the cat the dog the bird cat", ["the:3", "cat:2", "bird:1"]),
        ("a a a b b c", ["a:3", "b:2", "c:1"]),
        ("Hello, hello! WORLD world world", ["world:3", "hello:2"]),
    ]
    ok = True
    for text, exp in cases:
        got = list(top_words(text))
        # third case has only 2 distinct words tied with rest; check prefix
        if got[:len(exp)] != exp:
            ok = False
            detail = f"text={text!r} got={got} exp={exp}"
            break
except Exception as e:
    detail = f"error: {e}"
print("PASS" if ok else "FAIL", detail)
open("/logs/verifier/reward.txt", "w").write("1.0" if ok else "0.0")
PY

import importlib.util, pathlib, sys, time
BEFORE_PATH = str(pathlib.Path(__file__).with_name("R4-016_redaction_before.py"))
spec = importlib.util.spec_from_file_location("redaction_before", BEFORE_PATH)
before = importlib.util.module_from_spec(spec); sys.modules["redaction_before"] = before
spec.loader.exec_module(before)
import machina.memory.redaction as after

def bench(fn, text, n=50):
    fn(text)  # warm
    t0 = time.perf_counter()
    for _ in range(n):
        fn(text)
    return (time.perf_counter() - t0) / n * 1000

# realistic small log line with one secret
small = "2026-10-14 12:00:01 INFO provider=openai call completed in 812ms; token=sk-" + "A"*24 + " rotated"
small2 = "the report was a bearer of bad news; order 1234567890123 confirmed; commit a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 merged"
mid = (small + "\n" + small2 + "\n") * 40   # ~5KB
big = (small + "\n" + small2 + "\n") * 500  # ~64KB
for name, text in [("small(one secret)", small), ("mid ~5KB", mid), ("big ~64KB", big)]:
    b = bench(before.redact_secrets, text, 200 if len(text) < 6000 else 50)
    a = bench(after.redact_secrets, text, 200 if len(text) < 6000 else 50)
    print(f"{name:20s} len={len(text):7d}  before={b:7.3f} ms  after={a:7.3f} ms  x{a/b:.2f}")

# pass split on the huge blob
blob = ("The quick brown fox jumps over the lazy dog. " * 200 +
        "token=Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OT= " +
        "Bearer abcdef1234567890XYZ " +
        "api_key=abc.defghijklmnop.qrstuvwxyz0123 " +
        "user@example.com 4111 1111 1111 1111 123-45-6789 card 40000000000000006\n") * 200
r = after._default_redactor
t0=time.perf_counter(); out1, names = r._apply_patterns(blob); t1=time.perf_counter()
out2, extra = r._apply_patterns(out1, guard_markers=True, only=set(names)); t2=time.perf_counter()
print("huge blob pass1: %.1f ms (fired=%s); pass2(only fired): %.1f ms" % ((t1-t0)*1000, sorted(names), (t2-t1)*1000))
t0=time.perf_counter()
for rp in r._patterns: rp.pattern.search(blob)
print("all-pattern scan cost: %.1f ms" % ((time.perf_counter()-t0)*1000))

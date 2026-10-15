from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

def stream(text, chunks):
    r = StreamingSecretRedactor()
    return "".join(r.feed(c) for c in chunks) + r.flush()

cases = {
    "sk_213": ("sk-" + "a"*210, "b"*80 + " done."),
    "bearer_550": ("Bearer " + "a"*510, "b"*40 + " done."),
    "password_250": ("password=" + "x"*250, "y"*30 + " done."),
    "token_eq_250": ("token=" + "a"*250, "b"*30 + " done."),
    "jwt_1300": ("eyJ" + "a"*1300, "b"*30 + " done."),
    "openrouter_213": ("sk-or-" + "a"*210, "b"*80 + " done."),
    "aiiza_213": ("AIza" + "a"*210, "b"*80 + " done."),
    "xoxb_213": ("xoxb-" + "a"*210, "b"*80 + " done."),
}
for name, chunks in cases.items():
    full = "".join(chunks)
    batch = redact_secrets(full).text
    out = stream(full, chunks)
    same = out == batch
    print(f"{name:16s} batch={batch[:40]!r:45s} same={same}")
    if not same:
        print(f"                 stream={out[:60]!r}")

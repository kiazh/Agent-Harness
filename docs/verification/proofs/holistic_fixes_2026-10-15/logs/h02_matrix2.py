from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

def stream(chunks):
    r = StreamingSecretRedactor()
    return "".join(r.feed(c) for c in chunks) + r.flush()

cases = {
    "token_eq_350": ("token=" + "a"*350, "b"*30 + " done."),
    "quoted_key_350": ('api_key="' + "a"*350, "b"*30 + '" done.'),
    "db_350": ("postgres://u:" + "a"*350, "b"*30 + "@host/db done."),
    "cohere_213": ("cohere_api_key=" + "a"*210, "b"*80 + " done."),
    "pwd_250": ("pwd=" + "x"*250, "y"*30 + " done."),
}
for name, chunks in cases.items():
    full = "".join(chunks)
    batch = redact_secrets(full).text
    out = stream(chunks)
    same = out == batch
    print(f"{name:16s} same={same} batch={batch[:45]!r}")
    if not same:
        print(f"                 stream={out[:60]!r}")

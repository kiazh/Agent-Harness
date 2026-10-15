from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

def stream(chunks):
    r = StreamingSecretRedactor()
    return "".join(r.feed(c) for c in chunks) + r.flush()

# Synthetic marker with a long all-caps run between BEGIN and PRIVATE KEY.
text = "-----BEGIN " + "A"*100 + " PRIVATE KEY-----\nSECRETBODYLINE\n-----END " + "A"*100 + " PRIVATE KEY-----\n"
batch = redact_secrets(text).text
print("batch:", repr(batch[:60]), "redacted:", "SECRETBODYLINE" not in batch)
for size in (1, 7, 40, 41, 97, 128):
    chunks = [text[i:i+size] for i in range(0, len(text), size)]
    out = stream(chunks)
    ok = "SECRETBODYLINE" not in out and out == batch
    print(f"chunks of {size}: ok={ok}")
    if not ok:
        print("   out:", repr(out[:120]))

# The >10-space assignment gap case.
text2 = "password" + " "*15 + "= hunter2synthetic"
print("gap batch:", repr(redact_secrets(text2).text))
print("gap stream:", repr(stream(["password" + " "*15, "= hunter2synthetic"])))

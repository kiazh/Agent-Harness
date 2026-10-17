"""Prevent exception messages/source lines from entering application logs."""

import importlib
import logging
import sys

#: Depth bound for recursive redaction of structured log args. Container
#: nesting beyond this is replaced by a marker instead of being walked (a
#: cyclic arg would otherwise recurse forever).
_MAX_STRUCTURE_DEPTH = 8


class _RedactionFailed(Exception):
    """A redactor failure masks the WHOLE record (see ``factory`` below).

    Returning a per-string marker would leave a redacted ``%``-template next
    to redacted args only when the redactor half-worked — and a template
    replaced by a marker no longer matches the arg tuple, which breaks
    ``record.getMessage()`` inside handlers. A failed redactor means nothing
    in the record can be trusted, so it is masked whole.
    """


def install() -> None:
    """Idempotently install a log factory that redacts Machina records and masks failures."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_ah_sanitized", False):
        return

    def value(item, depth=0):
        if isinstance(item, BaseException):
            return type(item).__name__
        if isinstance(item, str):
            redaction = sys.modules.get("machina.memory.redaction")
            try:
                if redaction is None:
                    redaction = importlib.import_module("machina.memory.redaction")
                # A partially initialized module also fails closed here.
                return redaction.redact_secrets(item).text
            except Exception as exc:
                raise _RedactionFailed from exc
        elif isinstance(item, (dict, list, tuple)):
            if depth >= _MAX_STRUCTURE_DEPTH:
                # Cyclic/absurdly deep args are masked rather than walked;
                # returning the raw container would leak nested values.
                return "[REDACTED]"
            # Structured args are walked recursively: a secret nested below
            # the top level would otherwise pass through verbatim (R4-063).
            if isinstance(item, dict):
                return {
                    value(key, depth + 1): value(entry, depth + 1) for key, entry in item.items()
                }
            redacted = [value(entry, depth + 1) for entry in item]
            return tuple(redacted) if isinstance(item, tuple) else redacted
        return item

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name == "machina" or record.name.startswith("machina."):
            try:
                record.msg = value(record.msg)
                if isinstance(record.args, dict):
                    record.args = {key: value(item) for key, item in record.args.items()}
                else:
                    record.args = tuple(value(item) for item in record.args)
                # Objects may reveal credentials only when logging formats
                # %s/%r. Redact that final text and prevent later conversion.
                record.msg = value(record.getMessage())
                record.args = ()
                if record.exc_info:
                    # Exceptions can contain echoed provider prompts or credentials.
                    # Keep class and source locations, never source text/message.
                    kind, _, traceback = record.exc_info
                    frames = []
                    while traceback is not None:
                        code = traceback.tb_frame.f_code
                        frames.append(f"{code.co_filename}:{traceback.tb_lineno} ({code.co_name})")
                        traceback = traceback.tb_next
                    record.exc_text = value("\n".join([kind.__name__, *frames]))
                    record.exc_info = None
            except Exception:
                # Fail closed: a redactor failure (or any conversion failure)
                # masks the whole record and must never raise into the caller.
                record.msg = "[REDACTED]"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
        return record

    factory._ah_sanitized = True
    logging.setLogRecordFactory(factory)

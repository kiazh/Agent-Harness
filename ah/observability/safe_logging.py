"""Prevent exception messages/source lines from entering application logs."""

import logging
import sys


def install() -> None:
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_ah_sanitized", False):
        return

    def value(item):
        if isinstance(item, BaseException):
            return type(item).__name__
        if isinstance(item, str):
            # Avoid importing the memory graph while modules initialize.
            redaction = sys.modules.get("ah.memory.redaction")
            if redaction is not None and hasattr(redaction, "redact_secrets"):
                return redaction.redact_secrets(item).text
        return item

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name == "ah" or record.name.startswith("ah."):
            record.msg = value(record.msg)
            if isinstance(record.args, dict):
                record.args = {key: value(item) for key, item in record.args.items()}
            else:
                record.args = tuple(value(item) for item in record.args)
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
        return record

    factory._ah_sanitized = True
    logging.setLogRecordFactory(factory)

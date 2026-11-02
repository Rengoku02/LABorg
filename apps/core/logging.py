import logging
import re

# Signed links carry a token in the path: action links /a/<token>/<action>/ and the claims
# email's /claims/t/<token>/. Never let a token reach a log.
TOKEN_PATH = re.compile(r"(/a/|/claims/t/)[^/\s?#]+")


def _redact(value):
    return TOKEN_PATH.sub(r"\1[redacted]", value) if isinstance(value, str) else value


class RedactActionTokens(logging.Filter):
    def filter(self, record):
        record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact(arg) for key, arg in record.args.items()}
        return True

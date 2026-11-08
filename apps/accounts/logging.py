import logging
import re


_SECRET_PATH = re.compile(r"(/accounts/(?:verify-email|reset)/)[^\s?\"']+")


class AccountTokenFilter(logging.Filter):
    """Keep one-time link paths out of Django's access and request logs."""

    def filter(self, record):
        message = record.getMessage()
        scrubbed = _SECRET_PATH.sub(r"\1[redacted]", message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True

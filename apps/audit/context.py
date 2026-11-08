from contextvars import ContextVar
from uuid import uuid4

current_operation_id = ContextVar("audit_operation_id", default=None)


class AuditOperationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return self.get_response(request)
        token = current_operation_id.set(uuid4())
        try:
            return self.get_response(request)
        finally:
            current_operation_id.reset(token)

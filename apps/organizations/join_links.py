from django.core import signing
from django.http import Http404

SALT = "enactus.organization-join"


def make_join_code(org_id):
    return signing.dumps(org_id, salt=SALT)


def read_join_code(code):
    if len(code) > 128:
        raise Http404
    try:
        org_id = signing.loads(code, salt=SALT)
    except (signing.BadSignature, ValueError, TypeError):
        raise Http404 from None
    if type(org_id) is not int or not 1 <= org_id <= 2**63 - 1:
        raise Http404
    return org_id

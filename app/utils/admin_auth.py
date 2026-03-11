"""
Operator admin authentication decorators.
Protects the /admin/* routes.
"""

from functools import wraps
from flask import session, redirect, url_for, request


def operator_admin_required(f):
    """Require operator admin session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("operator_admin"):
            return redirect(url_for("admin.login", next=request.url))
        return f(*args, **kwargs)
    return decorated

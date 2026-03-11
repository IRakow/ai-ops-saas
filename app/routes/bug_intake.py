"""
Bug Intake Routes — Receives auto-detected and user-reported bugs.
No auth required (session_token acts as bearer for status polling).
"""

import logging
import config
from flask import Blueprint, request, jsonify
from app.services.bug_intake_service import BugIntakeService

logger = logging.getLogger(__name__)

bug_intake_bp = Blueprint(
    "bug_intake", __name__,
    url_prefix="/api/bug-intake",
)


def _get_service():
    return BugIntakeService()


def _detect_environment(req):
    """Detect test vs production from request host."""
    host = req.host or ""
    # Test VM or localhost → "test" environment
    test_hosts = ["localhost", "127.0.0.1"]
    if config.TEST_VM_IP:
        test_hosts.append(config.TEST_VM_IP)
    if any(h in host for h in test_hosts):
        return "test"
    return "production"


def _extract_user_id(req):
    """Try to extract user ID from JWT if present. Returns None on failure."""
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        import jwt
        import os
        token = auth.split(" ", 1)[1]
        payload = jwt.decode(
            token,
            os.getenv("SECRET_KEY", ""),
            algorithms=["HS256"],
        )
        return payload.get("user_id") or payload.get("sub")
    except Exception:
        return None


@bug_intake_bp.route("/report", methods=["POST"])
def report_bug():
    """Receive a bug report from the frontend."""
    try:
        data = request.get_json(silent=True) or {}

        if not data.get("error_message") and not data.get("user_description"):
            return jsonify({"error": "error_message or user_description required"}), 400

        data["environment"] = _detect_environment(request)
        data["reporter_user_id"] = _extract_user_id(request)

        svc = _get_service()
        result = svc.submit_report(data)

        return jsonify(result), 201

    except Exception as e:
        logger.error("Bug report submission failed: %s", e, exc_info=True)
        return jsonify({"error": "Failed to submit bug report"}), 500


@bug_intake_bp.route("/status", methods=["GET"])
def bug_status():
    """Poll bug report status by bug_id or session_token."""
    try:
        bug_id = request.args.get("bug_id")
        session_token = request.args.get("session_token")

        if not bug_id and not session_token:
            return jsonify({"error": "bug_id or session_token required"}), 400

        svc = _get_service()
        reports = svc.get_status(bug_id=bug_id, session_token=session_token)

        return jsonify({"reports": reports}), 200

    except Exception as e:
        logger.error("Bug status check failed: %s", e, exc_info=True)
        return jsonify({"error": "Failed to check status"}), 500

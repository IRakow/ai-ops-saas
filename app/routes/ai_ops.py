"""
AI Ops Panel Routes
Blueprint at /ai-ops/ with login, dashboard, chat, plan review, and status pages.
Separate auth from main app — session-based, not JWT.
"""

import os
import logging
import config
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, jsonify, current_app, g
)
from app.utils.ai_ops_auth import ai_ops_auth_required, get_current_ai_ops_user
from app.services.ai_ops_service import AIOpsService

logger = logging.getLogger(__name__)

ai_ops_bp = Blueprint(
    "ai_ops", __name__,
    url_prefix="/ai-ops",
    template_folder="../templates",
    static_folder="../static",
)


MAX_ACTIVE_JOBS = 20  # Hard limit on concurrent queued/running sessions


def get_service():
    return AIOpsService()


def _active_job_count(svc):
    """Count sessions that are queued, running, or awaiting approval for current tenant."""
    try:
        tenant_id = session.get("ai_ops_tenant_id")
        query = svc.supabase.table("ai_ops_sessions").select(
            "id", count="exact"
        ).in_(
            "status", ["queued", "running", "awaiting_approval", "gathering_info"]
        )
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        result = query.execute()
        return result.count or 0
    except Exception as e:
        logger.warning("Failed to count active jobs: %s", e)
        return 0




# =========================================================================
# AUTH
# =========================================================================

@ai_ops_bp.route("/login", methods=["GET", "POST"])
def login():
    """AI Ops login page — separate from main app auth."""
    if session.get("ai_ops_user_id"):
        return redirect(url_for("ai_ops.dashboard"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        svc = get_service()
        user = svc.authenticate_user(email, password)

        if user:
            session["ai_ops_user_id"] = user["id"]
            session["ai_ops_user_name"] = user["name"]
            session["ai_ops_user_email"] = user["email"]
            session["ai_ops_user_role"] = user.get("role", "user")
            session["ai_ops_tenant_id"] = user.get("tenant_id", "")
            session.permanent = True

            # Load tenant slug for URL routing
            if user.get("tenant_id"):
                try:
                    from app.tenant import load_tenant
                    tenant = load_tenant(user["tenant_id"])
                    session["ai_ops_tenant_slug"] = tenant.slug
                except Exception:
                    pass

            svc.log_audit(None, user["id"], "login", {"email": email})

            # Redirect to onboarding if not completed
            if user.get("tenant_id"):
                from app.tenant import load_tenant as lt
                try:
                    t = lt(user["tenant_id"])
                    if not t.onboarded_at:
                        return redirect(url_for("onboarding.start"))
                except Exception:
                    pass

            return redirect(url_for("ai_ops.dashboard"))
        else:
            error = "Invalid email or password"

    return render_template("ai_ops/login.html", error=error)


@ai_ops_bp.route("/logout")
def logout():
    """Log out of AI Ops panel."""
    user_id = session.get("ai_ops_user_id")
    if user_id:
        get_service().log_audit(None, user_id, "logout")

    session.pop("ai_ops_user_id", None)
    session.pop("ai_ops_user_name", None)
    session.pop("ai_ops_user_email", None)
    session.pop("ai_ops_user_role", None)
    session.pop("ai_ops_tenant_id", None)
    session.pop("ai_ops_tenant_slug", None)
    return redirect(url_for("ai_ops.login"))


# =========================================================================
# DASHBOARD
# =========================================================================

@ai_ops_bp.route("/")
@ai_ops_auth_required
def dashboard():
    """Main dashboard — session list + create new."""
    user = get_current_ai_ops_user()
    svc = get_service()

    filters = {
        "user_id": request.args.get("user_id") or None,
        "status": request.args.get("status") or None,
        "date_from": request.args.get("date_from") or None,
        "date_to": request.args.get("date_to") or None,
    }
    sessions = svc.list_sessions(limit=50, **filters)
    attention_sessions = svc.list_attention_sessions()
    users = svc.list_users()

    return render_template("ai_ops/dashboard.html",
                           user=user, sessions=sessions,
                           attention_sessions=attention_sessions,
                           users=users, filters=filters)


# =========================================================================
# SESSION MANAGEMENT
# =========================================================================

@ai_ops_bp.route("/new", methods=["POST"])
@ai_ops_auth_required
def new_session():
    """Create a new AI Ops session."""
    user = get_current_ai_ops_user()
    mode = request.form.get("mode", "bug_fix")

    if mode not in ("bug_fix", "new_feature"):
        flash("Invalid mode selected")
        return redirect(url_for("ai_ops.dashboard"))

    svc = get_service()

    # Hard limit: max 10 active jobs
    if _active_job_count(svc) >= MAX_ACTIVE_JOBS:
        flash(f"Queue is full ({MAX_ACTIVE_JOBS} active jobs). Please wait for some to finish before submitting new ones.")
        return redirect(url_for("ai_ops.dashboard"))

    new_session = svc.create_session(user["id"], mode)

    if new_session:
        return redirect(url_for("ai_ops.session_view", session_id=new_session["id"]))
    else:
        flash("Failed to create session")
        return redirect(url_for("ai_ops.dashboard"))


@ai_ops_bp.route("/session/<session_id>")
@ai_ops_auth_required
def session_view(session_id):
    """Main chat interface for a session."""
    user = get_current_ai_ops_user()
    svc = get_service()

    sess = svc.get_session(session_id)
    if not sess:
        flash("Session not found")
        return redirect(url_for("ai_ops.dashboard"))

    exclude = ["plan"] if user.get("role") != "admin" else None
    messages = svc.get_messages(session_id, exclude_types=exclude)
    tasks = svc.get_tasks(session_id)
    files = svc.get_files(session_id)

    return render_template("ai_ops/session.html",
                           user=user, session=sess, messages=messages,
                           tasks=tasks, files=files)


@ai_ops_bp.route("/session/<session_id>/plan")
@ai_ops_auth_required
def plan_view(session_id):
    """Simplified task plan review page."""
    user = get_current_ai_ops_user()
    svc = get_service()

    sess = svc.get_session(session_id)
    if not sess:
        flash("Session not found")
        return redirect(url_for("ai_ops.dashboard"))

    tasks = svc.get_tasks(session_id)

    return render_template("ai_ops/plan.html",
                           user=user, session=sess, tasks=tasks)


@ai_ops_bp.route("/session/<session_id>/status")
@ai_ops_auth_required
def status_view(session_id):
    """Pipeline progress page with live updates."""
    user = get_current_ai_ops_user()
    svc = get_service()

    sess = svc.get_session(session_id)
    if not sess:
        flash("Session not found")
        return redirect(url_for("ai_ops.dashboard"))

    tasks = svc.get_tasks(session_id)

    return render_template("ai_ops/status.html",
                           user=user, session=sess, tasks=tasks)


@ai_ops_bp.route("/history")
@ai_ops_auth_required
def history():
    """Past sessions view."""
    user = get_current_ai_ops_user()
    svc = get_service()

    filters = {
        "user_id": request.args.get("user_id") or None,
        "status": request.args.get("status") or None,
        "date_from": request.args.get("date_from") or None,
        "date_to": request.args.get("date_to") or None,
    }
    sessions = svc.list_sessions(limit=100, **filters)
    users = svc.list_users()

    return render_template("ai_ops/history.html",
                           user=user, sessions=sessions,
                           users=users, filters=filters)


@ai_ops_bp.route("/calculator")
@ai_ops_auth_required
def calculator():
    """Calculator tool."""
    user = get_current_ai_ops_user()
    return render_template("ai_ops/tools/calculator.html", user=user)


# =========================================================================
# API ENDPOINTS (AJAX)
# =========================================================================

@ai_ops_bp.route("/api/messages/<session_id>", methods=["GET"])
@ai_ops_auth_required
def api_get_messages(session_id):
    """Poll for new messages (AJAX)."""
    user = get_current_ai_ops_user()
    svc = get_service()
    after_id = request.args.get("after_id")
    exclude = ["plan"] if user.get("role") != "admin" else None
    messages = svc.get_messages(session_id, after_id=after_id,
                                exclude_types=exclude)
    return jsonify({"messages": messages})


@ai_ops_bp.route("/api/messages/<session_id>", methods=["POST"])
@ai_ops_auth_required
def api_send_message(session_id):
    """Send a user message (AJAX). Stores the message — agent processing is async."""
    user = get_current_ai_ops_user()
    data = request.get_json()

    if not data or not data.get("content"):
        return jsonify({"error": "Message content required"}), 400

    content = data["content"].strip()
    if not content:
        return jsonify({"error": "Message cannot be empty"}), 400

    try:
        svc = get_service()

        # Store the user message
        msg = svc.add_message(session_id, "user", user["name"], content)

        # Auto-set session title from first message if not set
        sess = svc.get_session(session_id)
        if sess and not sess.get("title"):
            title = content[:100] + ("..." if len(content) > 100 else "")
            svc.update_session(session_id, title=title)

        return jsonify({"status": "message_received", "message": msg})
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return jsonify({"error": str(e)}), 500


@ai_ops_bp.route("/api/session/<session_id>/submit", methods=["POST"])
@ai_ops_auth_required
def api_submit_to_agent(session_id):
    """Queue this session's task for the autonomous agent."""
    user = get_current_ai_ops_user()
    svc = get_service()

    # Hard limit: max 10 active jobs
    if _active_job_count(svc) >= MAX_ACTIVE_JOBS:
        return jsonify({"error": f"Queue is full ({MAX_ACTIVE_JOBS} active jobs). Please wait for some to finish."}), 429

    sess = svc.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    if sess["status"] not in ("gathering_info",):
        return jsonify({"error": f"Cannot submit from status: {sess['status']}"}), 400

    # Read auto_approve preference from request body
    data = request.get_json(silent=True) or {}
    auto_approve = bool(data.get("auto_approve", False))

    # Combine all user messages as the task description
    messages = svc.get_messages(session_id)
    user_messages = [m["content"] for m in messages if m["sender_type"] == "user"]
    description = "\n\n".join(user_messages)

    if not description.strip():
        return jsonify({"error": "No description provided"}), 400

    # Get attachments
    files = svc.get_files(session_id)
    attachments = [{"filename": f["filename"], "gcs_url": f.get("gcs_url")} for f in files]

    # Determine task type from session mode
    task_type = "bug" if sess["mode"] == "bug_fix" else "feature"

    # Store auto_approve on the session
    svc.update_session(session_id, auto_approve=auto_approve)

    # Queue the understanding phase first
    svc.queue_task(session_id, task_type, description, attachments, phase="understand")
    svc.update_session(session_id, status="queued", task_type=task_type)
    svc.add_message(
        session_id, "system", "System",
        "Your request is being analyzed. The agent will review the relevant code "
        "and post its understanding for your approval before making any changes.",
        message_type="status_update"
    )
    svc.log_audit(session_id, user["id"], "submitted_for_analysis")

    return jsonify({"status": "queued"})


@ai_ops_bp.route("/api/session/<session_id>/status", methods=["GET"])
@ai_ops_auth_required
def api_session_status(session_id):
    """Get current session status and tasks (AJAX polling)."""
    user = get_current_ai_ops_user()
    svc = get_service()
    sess = svc.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    tasks = svc.get_tasks(session_id)

    # Non-admin users see user_summary instead of full understanding
    is_admin = user.get("role") == "admin"
    summary = sess.get("summary")
    if not is_admin and sess.get("user_summary"):
        summary = sess.get("user_summary")

    result = {
        "status": sess["status"],
        "title": sess.get("title"),
        "gate_reached": sess.get("gate_reached"),
        "commit_sha": sess.get("commit_sha"),
        "escalated": sess.get("escalated"),
        "escalation_reason": sess.get("escalation_reason"),
        "rollback_happened": sess.get("rollback_happened"),
        "smoke_passed": sess.get("smoke_passed"),
        "soak_passed": sess.get("soak_passed"),
        "agent_elapsed_seconds": sess.get("agent_elapsed_seconds"),
        "summary": summary,
        "assessor_verdict": sess.get("assessor_verdict"),
        "assessor_explanation": sess.get("assessor_explanation"),
        "regression_detected": sess.get("regression_detected"),
        "retry_count": sess.get("retry_count"),
        "tasks": tasks,
    }

    if is_admin:
        result["user_summary"] = sess.get("user_summary")

    return jsonify(result)


@ai_ops_bp.route("/api/session/<session_id>/approve", methods=["POST"])
@ai_ops_auth_required
def api_approve_plan(session_id):
    """Approve and queue for agent execution."""
    user = get_current_ai_ops_user()
    svc = get_service()

    sess = svc.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    # Combine all messages for context — include agent's understanding
    messages = svc.get_messages(session_id)
    user_messages = [m["content"] for m in messages if m["sender_type"] == "user"]
    agent_messages = [m["content"] for m in messages if m["sender_type"] == "agent"]
    description = "\n\n".join(user_messages)
    if agent_messages:
        description += "\n\n## PRIOR ANALYSIS (from understanding phase)\n" + "\n\n".join(agent_messages)

    # Get understanding_output from session (stored by multi-agent understanding phase)
    understanding_output = sess.get("understanding_output") or ""

    # Get attachments
    files = svc.get_files(session_id)
    attachments = [{"filename": f["filename"], "gcs_url": f.get("gcs_url")} for f in files]

    task_type = "bug" if sess["mode"] == "bug_fix" else "feature"
    svc.queue_task(
        session_id, task_type, description, attachments,
        phase="execute", understanding_output=understanding_output,
    )
    svc.update_session(session_id, status="queued", task_type=task_type)
    svc.log_audit(session_id, user["id"], "approved_and_queued")

    return jsonify({"status": "queued"})


@ai_ops_bp.route("/api/session/<session_id>/approve-test", methods=["POST"])
@ai_ops_auth_required
def api_approve_test(session_id):
    """Approve test results and trigger production deployment."""
    user = get_current_ai_ops_user()
    svc = get_service()

    sess = svc.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    if sess["status"] != "awaiting_test_approval":
        return jsonify({"error": f"Session not in awaiting_test_approval state (current: {sess['status']})"}), 400

    # Trigger production deployment
    try:
        from app.services.bug_intake_service import BugIntakeService
        bug_svc = BugIntakeService()
        bug = bug_svc.find_bug_by_session(session_id)

        if bug:
            # Set environment to production for deploy
            bug_svc.update_status(bug["id"], "deploying", "Test approved. Deploying to production...")

            # Import and call the deploy function from the worker
            import subprocess
            import importlib
            worker = importlib.import_module("worker")
            worker._trigger_auto_deploy(bug_svc, {**bug, "environment": "production"})

            svc.update_session(session_id, status="completed")
            svc.add_message(
                session_id, "system", "System",
                "Test approved by {name}. Production deployment triggered.".format(name=user["name"]),
                message_type="status_update",
            )
            svc.log_audit(session_id, user["id"], "approved_test_deploy")
            return jsonify({"status": "deploying"})
        else:
            # No linked bug — just mark completed
            svc.update_session(session_id, status="completed")
            svc.add_message(
                session_id, "system", "System",
                "Test approved by {name}. Marked as completed.".format(name=user["name"]),
                message_type="status_update",
            )
            svc.log_audit(session_id, user["id"], "approved_test_completed")
            return jsonify({"status": "completed"})

    except Exception as e:
        logger.error("approve-test failed for session %s: %s", session_id, e)
        return jsonify({"error": str(e)}), 500


@ai_ops_bp.route("/api/session/<session_id>/upload", methods=["POST"])
@ai_ops_auth_required
def api_upload_file(session_id):
    """Upload a file (screenshot, log) to a session."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    # Validate file type
    allowed_types = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".txt", ".log", ".csv"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_types:
        return jsonify({"error": f"File type not allowed: {ext}"}), 400

    try:
        import uuid as _uuid
        # Try to upload to GCS first
        gcs_url = None
        gcs_path = None
        local_url = None

        try:
            from app.services.gcs_service import GCSService
        except ImportError:
            GCSService = None

        if GCSService:
            try:
                gcs = GCSService()
                gcs_path = f"ai-ops/{session_id}/{file.filename}"
                result = gcs.upload_file(file, gcs_path)
                # Handle both dict and string return values
                if isinstance(result, dict):
                    gcs_url = result.get("url", result.get("gcs_url", ""))
                elif isinstance(result, str):
                    gcs_url = result
            except Exception as e:
                logger.warning(f"GCS upload failed, falling back to local storage: {e}")
        else:
            logger.info("GCS not available, using local storage for upload")

        # Local fallback if GCS failed
        if not gcs_url:
            upload_dir = os.path.join(
                current_app.root_path, "static", "uploads", "ai-ops", session_id
            )
            os.makedirs(upload_dir, exist_ok=True)
            safe_name = f"{_uuid.uuid4().hex[:8]}_{file.filename}"
            local_path = os.path.join(upload_dir, safe_name)
            file.save(local_path)
            local_url = f"/static/uploads/ai-ops/{session_id}/{safe_name}"

        svc = get_service()
        file_record = svc.add_file(
            session_id, file.filename, gcs_path,
            gcs_url or local_url
        )
        # Include local_url in response for JS sidebar update
        if file_record and local_url and not gcs_url:
            file_record["local_url"] = local_url

        return jsonify({
            "file": file_record,
            "message": f"File uploaded: {file.filename}"
        })

    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================================
# NOTES & FEEDBACK
# =========================================================================

@ai_ops_bp.route("/notes")
@ai_ops_auth_required
def notes_page():
    """Notes & Feedback page — view notes and AI suggestions."""
    user = get_current_ai_ops_user()
    from app.services.ai_ops_notes_service import AIOpsNotesService
    notes_svc = AIOpsNotesService()

    notes = notes_svc.list_notes(limit=200)
    suggestions = notes_svc.list_suggestions(status="pending", limit=50)
    unreviewed_count = notes_svc.count_unreviewed()

    return render_template("ai_ops/notes.html",
                           user=user, notes=notes,
                           suggestions=suggestions,
                           unreviewed_count=unreviewed_count)


@ai_ops_bp.route("/api/notes", methods=["POST"])
def api_submit_note():
    """Submit a note from the floating button. No AI Ops auth required."""
    data = request.get_json()
    if not data or not data.get("content", "").strip():
        return jsonify({"error": "Note content is required"}), 400

    submitter_name = (
        session.get("ai_ops_user_name")
        or session.get("user_name")
        or "Anonymous"
    )
    submitter_email = (
        session.get("ai_ops_user_email")
        or session.get("user_email")
    )
    submitter_id = (
        session.get("ai_ops_user_id")
        or session.get("user_id")
    )

    from app.services.ai_ops_notes_service import AIOpsNotesService
    notes_svc = AIOpsNotesService()
    note = notes_svc.submit_note(
        content=data["content"],
        submitter_name=submitter_name,
        submitter_email=submitter_email,
        submitter_id=submitter_id,
        page_url=data.get("page_url"),
        page_title=data.get("page_title"),
    )

    if note:
        return jsonify({"status": "ok", "note_id": note["id"]})
    return jsonify({"error": "Failed to save note"}), 500


@ai_ops_bp.route("/api/notes/analyze", methods=["POST"])
@ai_ops_auth_required
def api_analyze_notes():
    """Trigger Gemini batch analysis of unreviewed notes."""
    from app.services.ai_ops_notes_service import AIOpsNotesService
    notes_svc = AIOpsNotesService()

    suggestions = notes_svc.analyze_notes()
    if suggestions is None:
        return jsonify({"error": "Analysis failed"}), 500

    user = get_current_ai_ops_user()
    get_service().log_audit(None, user["id"], "notes_analyzed", {
        "suggestion_count": len(suggestions),
    })

    return jsonify({"suggestions": suggestions})


@ai_ops_bp.route("/api/notes/suggestions", methods=["GET"])
@ai_ops_auth_required
def api_list_suggestions():
    """List note suggestions (AJAX refresh)."""
    from app.services.ai_ops_notes_service import AIOpsNotesService
    notes_svc = AIOpsNotesService()
    status = request.args.get("status")
    suggestions = notes_svc.list_suggestions(status=status, limit=50)
    return jsonify({"suggestions": suggestions})


@ai_ops_bp.route("/api/notes/suggestions/<suggestion_id>/promote", methods=["POST"])
@ai_ops_auth_required
def api_promote_suggestion(suggestion_id):
    """Promote a suggestion to an AI Ops session."""
    user = get_current_ai_ops_user()
    from app.services.ai_ops_notes_service import AIOpsNotesService
    notes_svc = AIOpsNotesService()

    new_session = notes_svc.promote_to_session(suggestion_id, user["id"])
    if not new_session:
        return jsonify({"error": "Failed to create session from suggestion"}), 400

    return jsonify({
        "status": "promoted",
        "session_id": new_session["id"],
        "session_url": url_for("ai_ops.session_view", session_id=new_session["id"]),
    })


@ai_ops_bp.route("/api/notes/suggestions/<suggestion_id>/dismiss", methods=["POST"])
@ai_ops_auth_required
def api_dismiss_suggestion(suggestion_id):
    """Dismiss a suggestion."""
    from app.services.ai_ops_notes_service import AIOpsNotesService
    notes_svc = AIOpsNotesService()

    result = notes_svc.dismiss_suggestion(suggestion_id)
    if result:
        return jsonify({"status": "dismissed"})
    return jsonify({"error": "Failed to dismiss suggestion"}), 400


# =========================================================================
# SETTINGS / USAGE / INTEGRATIONS
# =========================================================================

@ai_ops_bp.route("/settings", methods=["GET", "POST"])
@ai_ops_auth_required
def settings():
    user = get_current_ai_ops_user()
    tenant = g.tenant
    if request.method == "POST":
        from app.services.tenant_service import update_tenant
        update_tenant(tenant.id, {
            "app_name": request.form.get("app_name", tenant.app_name),
            "app_url": request.form.get("app_url", tenant.app_url),
            "app_description": request.form.get("app_description", tenant.app_description),
            "notification_emails": [e.strip() for e in request.form.get("notification_emails", "").split(",") if e.strip()],
        })
        flash("Settings saved", "success")
        return redirect(url_for("ai_ops.settings"))
    return render_template("ai_ops/settings.html", user=user, tenant=tenant)


@ai_ops_bp.route("/usage")
@ai_ops_auth_required
def usage():
    user = get_current_ai_ops_user()
    tenant = g.tenant
    from app.services.usage_service import get_monthly_summary
    summary = get_monthly_summary(tenant.id)
    svc = get_service()
    records = svc.supabase.table("usage_records") \
        .select("*") \
        .eq("tenant_id", tenant.id) \
        .order("created_at", desc=True) \
        .limit(50) \
        .execute()
    return render_template("ai_ops/usage.html", user=user, tenant=tenant,
        usage={**summary, "monthly_fix_limit": tenant.monthly_fix_limit, "monthly_feature_limit": tenant.monthly_feature_limit},
        records=records.data or [])


@ai_ops_bp.route("/integrations")
@ai_ops_auth_required
def integrations():
    user = get_current_ai_ops_user()
    tenant = g.tenant
    svc = get_service()
    webhooks = svc.supabase.table("webhooks") \
        .select("*") \
        .eq("tenant_id", tenant.id) \
        .execute()
    api_keys = svc.supabase.table("tenant_api_keys") \
        .select("id, name, key_prefix, scopes, created_at, last_used_at") \
        .eq("tenant_id", tenant.id) \
        .execute()
    return render_template("ai_ops/integrations.html", user=user, tenant=tenant,
        webhooks=webhooks.data or [], api_keys=api_keys.data or [],
        saas_domain=config.SAAS_DOMAIN,
        api_key=tenant.api_key_prefix + "...")

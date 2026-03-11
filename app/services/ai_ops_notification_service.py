"""
AI Ops Notification Service
Sends email (SendGrid) and SMS (Twilio) notifications for AI Ops events.
Uses the existing SendGridService and TwilioService from the codebase.
"""

import os
import logging
import config

logger = logging.getLogger(__name__)

# Notification recipients — built dynamically from config
RECIPIENTS = []
if config.NOTIFICATION_EMAILS:
    for i, email in enumerate(config.NOTIFICATION_EMAILS):
        RECIPIENTS.append({
            "name": email.split("@")[0].title(),
            "email": email,
            "phone": config.NOTIFICATION_PHONES[i] if i < len(config.NOTIFICATION_PHONES) else ""
        })


class AIOpsNotificationService:
    """Sends notifications for AI Ops pipeline events."""

    def __init__(self):
        self.sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
        self.sendgrid_from_email = config.NOTIFICATION_FROM_EMAIL
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.twilio_from_number = os.getenv("TWILIO_PHONE_NUMBER")

    # =========================================================================
    # HIGH-LEVEL NOTIFICATION METHODS
    # =========================================================================

    def notify_plan_ready(self, session_title, session_id, plan_url,
                          tenant_emails: list[str] = None):
        """Notify when a task plan is ready for review."""
        subject = f"AI Ops: Plan Ready for Review — {session_title}"
        body = (
            f"A task plan is ready for your review.\n\n"
            f"Session: {session_title}\n"
            f"Review the plan here: {plan_url}\n\n"
            f"Please approve or provide feedback."
        )
        sms = f"AI Ops: Plan ready for \"{session_title}\". Review at {plan_url}"

        self._send_all_emails(subject, body, tenant_emails=tenant_emails)
        self._send_all_sms(sms)

    def notify_coding_started(self, session_title, session_id,
                              tenant_emails: list[str] = None):
        """Notify when coding has started (email only)."""
        subject = f"AI Ops: Coding Started — {session_title}"
        body = (
            f"The AI agents have started implementing the approved plan.\n\n"
            f"Session: {session_title}\n"
            f"You'll be notified when the work is deployed to staging."
        )
        self._send_all_emails(subject, body, tenant_emails=tenant_emails)

    def notify_deployed_staging(self, session_title, session_id, staging_url,
                                commit_sha=None, tenant_emails: list[str] = None):
        """Notify when changes are deployed to staging."""
        subject = f"AI Ops: Deployed to Staging — {session_title}"
        body = (
            f"Changes have been deployed to staging!\n\n"
            f"Session: {session_title}\n"
            f"Staging URL: {staging_url}\n"
        )
        if commit_sha:
            body += f"Commit: {commit_sha}\n"
        body += (
            f"\nPlease test the changes and confirm they work as expected.\n"
            f"Production deployment is always manual — contact your administrator when ready."
        )
        sms = f"AI Ops: \"{session_title}\" deployed to staging. Test at {staging_url}"

        self._send_all_emails(subject, body, tenant_emails=tenant_emails)
        self._send_all_sms(sms)

    def notify_pipeline_failed(self, session_title, session_id, error_summary,
                               tenant_emails: list[str] = None):
        """Notify when the pipeline fails."""
        subject = f"AI Ops: Pipeline Failed — {session_title}"
        body = (
            f"The AI pipeline encountered an error.\n\n"
            f"Session: {session_title}\n"
            f"Error: {error_summary}\n\n"
            f"The team will investigate and resolve this manually."
        )
        sms = f"AI Ops: Pipeline FAILED for \"{session_title}\". Check email for details."

        self._send_all_emails(subject, body, tenant_emails=tenant_emails)
        self._send_all_sms(sms)

    # =========================================================================
    # TRANSPORT METHODS
    # =========================================================================

    def _send_all_emails(self, subject, body, tenant_emails: list[str] = None):
        """Send email to all recipients via SendGrid.

        If tenant_emails is provided, send to those addresses instead of
        the default RECIPIENTS list from config.
        """
        if not self.sendgrid_api_key:
            logger.warning("SendGrid API key not configured, skipping email")
            return

        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            sg = SendGridAPIClient(self.sendgrid_api_key)

            # Use tenant-specific emails if provided, otherwise fall back to config
            if tenant_emails:
                recipients = [
                    {"name": email.split("@")[0].title(), "email": email}
                    for email in tenant_emails
                ]
            else:
                recipients = RECIPIENTS

            for recipient in recipients:
                message = Mail(
                    from_email=self.sendgrid_from_email,
                    to_emails=recipient["email"],
                    subject=subject,
                    plain_text_content=body,
                )
                response = sg.send(message)
                logger.info(
                    f"Email sent to {recipient['name']} ({recipient['email']}): "
                    f"status {response.status_code}"
                )
        except Exception as e:
            logger.error(f"Failed to send email: {e}")

    def _send_all_sms(self, message):
        """Send SMS to all recipients via Twilio."""
        if not all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_from_number]):
            logger.warning("Twilio not configured, skipping SMS")
            return

        try:
            from twilio.rest import Client

            client = Client(self.twilio_account_sid, self.twilio_auth_token)

            for recipient in RECIPIENTS:
                if not recipient.get("phone"):
                    continue
                msg = client.messages.create(
                    body=message,
                    from_=self.twilio_from_number,
                    to=recipient["phone"],
                )
                logger.info(
                    f"SMS sent to {recipient['name']} ({recipient['phone']}): "
                    f"SID {msg.sid}"
                )
        except Exception as e:
            logger.error(f"Failed to send SMS: {e}")

"""
AI OPS - NOTIFICATION SYSTEM
==============================
Sends triage reports and fix proposals via:
  1. iMessage (via AppleScript on macOS)
  2. Email (via SMTP or SendGrid)
  3. Webhook (Slack, Discord, custom)
  4. Local file (always, as backup)

Priority routing:
  - CRITICAL errors → iMessage immediately + email
  - HIGH errors → email + webhook
  - MEDIUM/LOW → email digest (batched)
"""

import os
import json
import logging
import smtplib
import subprocess
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("ai_ops.notifications")


# =============================================================================
# NOTIFICATION CONFIG
# =============================================================================

@dataclass
class NotificationConfig:
    """Configuration for all notification channels."""

    # iMessage (macOS only — uses AppleScript)
    imessage_enabled: bool = False
    imessage_recipients: list = field(default_factory=list)  # Phone numbers or emails

    # Email (SMTP)
    email_enabled: bool = False
    email_smtp_host: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_password: str = ""  # App password for Gmail
    email_from: str = ""
    email_to: list = field(default_factory=list)

    # SendGrid (alternative to SMTP)
    sendgrid_enabled: bool = False
    sendgrid_api_key: str = ""
    sendgrid_from: str = ""
    sendgrid_to: list = field(default_factory=list)

    # Webhook (Slack, Discord, custom)
    webhook_enabled: bool = False
    webhook_url: str = ""

    # Local file backup (always on)
    log_dir: str = "triage_notifications"

    # Routing rules
    critical_channels: list = field(
        default_factory=lambda: ["imessage", "email"]
    )
    high_channels: list = field(
        default_factory=lambda: ["email", "webhook"]
    )
    medium_channels: list = field(
        default_factory=lambda: ["email"]
    )
    low_channels: list = field(
        default_factory=lambda: ["log"]
    )


# =============================================================================
# NOTIFICATION MANAGER
# =============================================================================

class NotificationManager:
    """
    Routes notifications to the right channels based on severity.
    All notifications are also saved to local log files.
    """

    def __init__(self, config: NotificationConfig = None):
        self.config = config or NotificationConfig()
        self.log_dir = Path(self.config.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._pending_digest = []  # Batch medium/low for digest

    # -------------------------------------------------------------------------
    # MAIN SEND METHOD
    # -------------------------------------------------------------------------

    def send(self, consensus_result, severity: str = None):
        """
        Send notification based on consensus result and severity.
        Routes to appropriate channels.
        """
        severity = (severity or consensus_result.severity or "medium").lower()

        # Determine which channels to use
        channel_map = {
            "critical": self.config.critical_channels,
            "high": self.config.high_channels,
            "medium": self.config.medium_channels,
            "low": self.config.low_channels,
        }
        channels = channel_map.get(severity, ["log"])

        # Format the message
        short_msg = self._format_short(consensus_result)
        full_msg = self._format_full(consensus_result)

        # Always log to file
        self._save_to_file(consensus_result, full_msg)

        # Route to channels
        for channel in channels:
            try:
                if channel == "imessage" and self.config.imessage_enabled:
                    self._send_imessage(short_msg)
                elif channel == "email":
                    if self.config.email_enabled:
                        self._send_email(consensus_result, full_msg)
                    elif self.config.sendgrid_enabled:
                        self._send_sendgrid(consensus_result, full_msg)
                elif channel == "webhook" and self.config.webhook_enabled:
                    self._send_webhook(short_msg)
                elif channel == "log":
                    pass  # Already saved above
            except Exception as e:
                logger.error(f"Failed to send via {channel}: {e}")

        logger.info(
            f"Notification sent [{severity.upper()}] via {channels}: "
            f"{consensus_result.error_type}"
        )

    def send_digest(self):
        """Send batched medium/low notifications as a digest."""
        if not self._pending_digest:
            return

        digest_text = self._format_digest(self._pending_digest)

        if self.config.email_enabled:
            self._send_email_raw(
                subject="AI Ops Triage Digest",
                body=digest_text,
            )
        elif self.config.sendgrid_enabled:
            self._send_sendgrid_raw(
                subject="AI Ops Triage Digest",
                body=digest_text,
            )

        self._pending_digest = []

    # -------------------------------------------------------------------------
    # iMESSAGE (macOS AppleScript)
    # -------------------------------------------------------------------------

    def _send_imessage(self, message):
        """
        Send iMessage via AppleScript.
        Only works on macOS with Messages.app configured.
        """
        if not self.config.imessage_recipients:
            logger.warning("No iMessage recipients configured")
            return

        for recipient in self.config.imessage_recipients:
            try:
                # Escape the message for AppleScript
                escaped_msg = message.replace('\\', '\\\\').replace('"', '\\"')
                escaped_recipient = recipient.replace('"', '\\"')

                applescript = f'''
                tell application "Messages"
                    set targetService to 1st account whose service type = iMessage
                    set targetBuddy to participant "{escaped_recipient}" of targetService
                    send "{escaped_msg}" to targetBuddy
                end tell
                '''

                result = subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

                if result.returncode == 0:
                    logger.info(f"iMessage sent to {recipient}")
                else:
                    # Fallback: try using the buddy's phone/email directly
                    applescript_fallback = f'''
                    tell application "Messages"
                        set targetBuddy to a]reference to buddy "{escaped_recipient}"
                        send "{escaped_msg}" to targetBuddy
                    end tell
                    '''
                    # Try simpler approach
                    applescript_simple = f'''
                    tell application "Messages"
                        send "{escaped_msg}" to buddy "{escaped_recipient}"
                    end tell
                    '''
                    result2 = subprocess.run(
                        ["osascript", "-e", applescript_simple],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if result2.returncode == 0:
                        logger.info(f"iMessage sent to {recipient} (fallback)")
                    else:
                        logger.error(
                            f"iMessage to {recipient} failed: "
                            f"{result.stderr} / {result2.stderr}"
                        )

            except subprocess.TimeoutExpired:
                logger.error(f"iMessage to {recipient} timed out")
            except FileNotFoundError:
                logger.error("osascript not found — iMessage only works on macOS")
            except Exception as e:
                logger.error(f"iMessage to {recipient} error: {e}")

    # -------------------------------------------------------------------------
    # EMAIL (SMTP)
    # -------------------------------------------------------------------------

    def _send_email(self, consensus_result, full_message):
        """Send email via SMTP."""
        severity = (consensus_result.severity or "medium").upper()
        subject = (
            f"{'🚨' if severity == 'CRITICAL' else '⚠️'} "
            f"AI Ops Triage [{severity}]: {consensus_result.error_type}"
        )
        self._send_email_raw(subject=subject, body=full_message)

    def _send_email_raw(self, subject, body):
        """Send a raw email via SMTP."""
        if not self.config.email_smtp_user or not self.config.email_to:
            logger.warning("Email not configured")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.config.email_from or self.config.email_smtp_user
            msg["To"] = ", ".join(self.config.email_to)

            # Plain text version
            msg.attach(MIMEText(body, "plain"))

            # HTML version (for better formatting)
            html_body = self._text_to_html(body)
            msg.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP(
                self.config.email_smtp_host, self.config.email_smtp_port
            ) as server:
                server.starttls()
                server.login(
                    self.config.email_smtp_user,
                    self.config.email_smtp_password,
                )
                server.send_message(msg)

            logger.info(f"Email sent: {subject}")

        except Exception as e:
            logger.error(f"Email failed: {e}")

    # -------------------------------------------------------------------------
    # SENDGRID
    # -------------------------------------------------------------------------

    def _send_sendgrid(self, consensus_result, full_message):
        """Send email via SendGrid API."""
        severity = (consensus_result.severity or "medium").upper()
        subject = (
            f"{'🚨' if severity == 'CRITICAL' else '⚠️'} "
            f"AI Ops Triage [{severity}]: {consensus_result.error_type}"
        )
        self._send_sendgrid_raw(subject=subject, body=full_message)

    def _send_sendgrid_raw(self, subject, body):
        """Send via SendGrid API."""
        if not self.config.sendgrid_api_key or not self.config.sendgrid_to:
            logger.warning("SendGrid not configured")
            return

        try:
            import httpx

            personalizations = [{
                "to": [{"email": addr} for addr in self.config.sendgrid_to],
                "subject": subject,
            }]

            response = httpx.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {self.config.sendgrid_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "personalizations": personalizations,
                    "from": {"email": self.config.sendgrid_from},
                    "content": [
                        {"type": "text/plain", "value": body},
                        {"type": "text/html", "value": self._text_to_html(body)},
                    ],
                },
                timeout=30,
            )

            if response.status_code in (200, 202):
                logger.info(f"SendGrid email sent: {subject}")
            else:
                logger.error(
                    f"SendGrid failed: {response.status_code} {response.text}"
                )

        except Exception as e:
            logger.error(f"SendGrid error: {e}")

    # -------------------------------------------------------------------------
    # WEBHOOK
    # -------------------------------------------------------------------------

    def _send_webhook(self, message):
        """Send to webhook URL (Slack, Discord, custom)."""
        if not self.config.webhook_url:
            return

        try:
            import httpx

            # Try Slack format first, then generic
            payload = {"text": message}

            response = httpx.post(
                self.config.webhook_url,
                json=payload,
                timeout=10,
            )

            if response.status_code == 200:
                logger.info("Webhook notification sent")
            else:
                logger.error(
                    f"Webhook failed: {response.status_code} {response.text}"
                )

        except Exception as e:
            logger.error(f"Webhook error: {e}")

    # -------------------------------------------------------------------------
    # LOCAL FILE BACKUP
    # -------------------------------------------------------------------------

    def _save_to_file(self, consensus_result, full_message):
        """Always save notification to local file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = consensus_result.fingerprint if hasattr(consensus_result, 'fingerprint') else "unknown"

        # Save readable report
        report_path = self.log_dir / f"{timestamp}_{fp}_report.txt"
        with open(report_path, "w") as f:
            f.write(full_message)

        # Save full JSON data
        json_path = self.log_dir / f"{timestamp}_{fp}_data.json"
        try:
            from dataclasses import asdict
            data = asdict(consensus_result) if hasattr(consensus_result, '__dataclass_fields__') else {}
            with open(json_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception:
            pass

        logger.debug(f"Notification saved to {report_path}")

    # -------------------------------------------------------------------------
    # MESSAGE FORMATTING
    # -------------------------------------------------------------------------

    def _format_short(self, result) -> str:
        """Short format for iMessage/SMS (under 500 chars)."""
        consensus_emoji = "✅" if result.consensus_reached else "⚠️"
        severity = (result.severity or "?").upper()

        msg = (
            f"AI OPS TRIAGE [{severity}]\n"
            f"{consensus_emoji} Consensus: {'REACHED' if result.consensus_reached else 'NOT REACHED'}\n"
            f"Error: {result.error_type}\n"
            f"{result.error_message[:100]}\n"
            f"Votes: {result.vote_summary}\n"
            f"Confidence: {result.confidence:.0%}\n"
        )

        if result.consensus_reached and result.final_fix:
            msg += f"Fix: {result.final_fix[:150]}\n"
            if result.auto_fixable:
                msg += "🟢 Safe to auto-fix\n"
            else:
                msg += "🔴 Needs manual review\n"
        else:
            msg += "Action: Manual review required\n"

        msg += f"File: {result.final_fix_file}" if result.final_fix_file else ""

        return msg.strip()

    def _format_full(self, result) -> str:
        """Full format for email."""
        consensus_emoji = "✅" if result.consensus_reached else "⚠️"
        severity = (result.severity or "?").upper()

        lines = [
            "=" * 70,
            f"AI OPS — TRIAGE REPORT",
            f"Generated: {result.timestamp}",
            "=" * 70,
            "",
            f"SEVERITY: {severity}",
            f"ERROR: {result.error_type}: {result.error_message}",
            f"CONSENSUS: {consensus_emoji} {'REACHED' if result.consensus_reached else 'NOT REACHED'}",
            f"VOTES: {result.vote_summary}",
            f"CONFIDENCE: {result.confidence:.0%}",
            f"AUTO-FIXABLE: {'Yes' if result.auto_fixable else 'No — manual review required'}",
            "",
            "-" * 70,
            "DIAGNOSIS (from Diagnostician agent):",
            "-" * 70,
            result.final_diagnosis or "(no diagnosis)",
            "",
            "-" * 70,
            "PROPOSED FIX (from Engineer agent):",
            "-" * 70,
            f"File: {result.final_fix_file}",
            "",
            result.final_fix or "(no fix proposed)",
            "",
        ]

        if result.final_fix_diff:
            lines.extend([
                "-" * 70,
                "CODE DIFF:",
                "-" * 70,
                result.final_fix_diff,
                "",
            ])

        lines.extend([
            "-" * 70,
            "FULL AGENT DEBATE:",
            "-" * 70,
            result.debate_transcript or "(no debate transcript)",
            "",
            "=" * 70,
            "To approve this fix: POST /triage/approve/<fingerprint>",
            "To ignore: POST /triage/ignore/<fingerprint>",
            f"Fingerprint: {result.fingerprint}",
            "=" * 70,
        ])

        return "\n".join(lines)

    def _format_digest(self, items) -> str:
        """Format multiple items as a digest email."""
        lines = [
            "=" * 70,
            f"AI OPS — TRIAGE DIGEST",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Items: {len(items)}",
            "=" * 70,
            "",
        ]

        for i, item in enumerate(items, 1):
            lines.extend([
                f"--- Item {i} ---",
                self._format_short(item),
                "",
            ])

        return "\n".join(lines)

    def _text_to_html(self, text) -> str:
        """Convert plain text to basic HTML for email."""
        import html
        escaped = html.escape(text)
        # Convert line breaks
        escaped = escaped.replace("\n", "<br>\n")
        # Wrap in pre for monospace sections (diffs, code)
        return f"""
        <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; 
                      padding: 20px; background: #f5f5f5;">
            <div style="max-width: 800px; margin: 0 auto; background: white; 
                        padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                <pre style="font-family: 'SF Mono', Monaco, monospace; 
                            font-size: 13px; line-height: 1.5; 
                            white-space: pre-wrap; word-wrap: break-word;">
{escaped}
                </pre>
            </div>
        </body>
        </html>
        """

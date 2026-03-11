# Customization Guide

## Agent Tuning

### Model Selection

Set `AGENT_MODEL` in `.env`. Default is `claude-opus-4-6` (recommended for complex debugging). You can use `claude-sonnet-4-6` for faster but less thorough analysis.

### Timeouts and Turns

The worker has per-agent timeout and turn limits. To adjust:

| Setting | Where | Default | Notes |
|---------|-------|---------|-------|
| Specialist turns | `worker.py` | 50 | Increase for larger codebases |
| Implementer turns | `worker.py` | 150 | Increase for complex multi-file fixes |
| Fixer turns | `worker.py` | 80 | The retry agent |
| Overall timeout | `.env` `AGENT_TIMEOUT` | 1800s | Max time per agent |
| Poll interval | `.env` `POLL_INTERVAL` | 10s | How often worker checks for work |

**Important:** Agents lose ALL work if they hit the turn limit. Set turns high and use prompt instructions to tell agents to reserve their last 2 turns for output.

## Codebase Context

The `codebase_context.md` file is injected into every agent's system prompt. Good context means better fixes. Include:

- Tech stack with versions
- File count and project scale
- Directory structure
- Code patterns (decorators, naming conventions, service patterns)
- Critical paths that need extra care (payments, auth, data)

See `examples/codebase_context.example.md` for a template.

## Blast Radius

The `blast_radius.json` file limits which files each module/area can modify. Structure:

```json
{
  "auth": {
    "allowed_files": [
      "app/utils/auth.py",
      "app/routes/auth.py",
      "app/services/auth_service.py"
    ]
  },
  "payments": {
    "allowed_files": [
      "app/services/payment_service.py",
      "app/routes/payments.py"
    ]
  }
}
```

Without a blast radius file, agents can edit any file in `WORKING_DIR`.

## Agent Protocol

Create an `AGENT_PROTOCOL.md` file (set path via `PROTOCOL_FILE`) with custom rules for how agents should work. Examples:

- "Always run the test suite after making changes"
- "Never modify migration files directly"
- "Use the existing service pattern when adding new services"
- "All database queries must filter by organization_id"

This gets prepended to agent prompts.

## Notifications

### Email (SendGrid)
Set `SENDGRID_API_KEY` and `NOTIFICATION_FROM_EMAIL`. Add recipient emails to `NOTIFICATION_EMAILS` (comma-separated).

### SMS (Twilio)
Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, and `NOTIFICATION_PHONE`.

Notifications fire on:
- Pipeline failures
- Successful deployments
- Soak test regressions

## Deployment

### Auto-Deploy Setup

1. Set `PRODUCTION_VM` to your GCP VM name
2. Create a deploy script on the VM and set `PRODUCTION_DEPLOY_SCRIPT`
3. Set `PRODUCTION_BASE_URL` for post-deploy health checks
4. Set `GITHUB_REPO` for git push

See `examples/deploy_script.example.sh` for a template.

### Staging

If you have a staging environment:
- Set `STAGING_DEPLOY_SCRIPT` and `STAGING_URL`
- The system will deploy to staging first and run smoke tests before production

## Client-Side Bug Detection

Add the bug-intake JavaScript to your application's pages:

```html
<link rel="stylesheet" href="/path/to/bug-intake.css">
<script src="/path/to/html2canvas.min.js"></script>
<script src="/path/to/bug-intake.js"></script>
```

This captures:
- HTTP 500+ errors from fetch() calls
- Uncaught JavaScript errors
- Unhandled promise rejections
- Screenshots (via html2canvas)

Configure the intake API URL in `bug-intake.js` to point to your AI Ops instance.

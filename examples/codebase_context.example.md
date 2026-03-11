You are working on MyApp (https://myapp.com), a SaaS platform for [describe what your app does].

KEY FACTS:
- Stack: Flask 3.x (Python 3.12), Supabase (PostgreSQL), [other services]
- [X] route files, [Y] services, [Z] database tables
- Multi-tenant with RLS, JWT auth
- Hosted on [hosting provider] with Gunicorn + Nginx

PROJECT STRUCTURE:
- app/routes/ — Flask blueprints
- app/services/ — Business logic services
- app/utils/ — Auth, DB, encryption utilities
- app/templates/ — Jinja2 templates
- app/static/ — CSS, JS, images

PATTERNS:
- Routes use @auth_required decorator
- Services instantiated with (organization_id, user_id)
- All DB queries filter by organization_id for multi-tenancy
- Templates extend base.html

CRITICAL PATHS (agents should be extra careful here):
- Payment processing (app/services/payment_service.py)
- Authentication (app/utils/auth.py)
- Data migrations (migrations/)

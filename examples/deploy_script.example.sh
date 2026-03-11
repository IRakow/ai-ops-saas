#!/bin/bash
# Example deploy script for AI Ops auto-deploy
# Customize for your environment

set -e

DEPLOY_DIR="/srv/myapp/current"
REPO_DIR="/srv/myapp/repo"

echo "=== Starting deployment ==="

# Pull latest code
cd "$REPO_DIR"
git pull origin main

# Install dependencies
source /srv/myapp/venv/bin/activate
pip install -r requirements.txt --quiet

# Run database migrations (if applicable)
# python manage.py migrate

# Sync static files
rsync -a --delete "$REPO_DIR/" "$DEPLOY_DIR/" \
  --exclude='.git' \
  --exclude='venv' \
  --exclude='.env' \
  --exclude='__pycache__'

# Reload application
sudo supervisorctl restart myapp

echo "=== Deployment complete ==="

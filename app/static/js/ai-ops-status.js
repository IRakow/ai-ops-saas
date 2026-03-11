/**
 * AI Ops Status Page — Auto-refresh for pipeline progress.
 */

(function () {
    'use strict';

    const POLL_INTERVAL = 8000; // 8 seconds
    let currentStatus = null;

    function pollStatus() {
        fetch(`/ai-ops/api/session/${SESSION_ID}/status`, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then(r => r.json())
            .then(data => {
                if (data.error) return;

                // Reload page if status changed
                if (currentStatus && data.status !== currentStatus) {
                    window.location.reload();
                    return;
                }
                currentStatus = data.status;

                // Update task statuses
                if (data.tasks) {
                    data.tasks.forEach(task => {
                        updateTaskUI(task);
                    });
                }

                // Update status badge
                const badge = document.getElementById('session-status-badge');
                if (badge) {
                    badge.textContent = data.status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
                }
            })
            .catch(() => {
                // Silently fail
            });
    }

    function updateTaskUI(task) {
        const el = document.getElementById('task-' + task.id);
        if (!el) return;

        const icon = el.querySelector('.me-3 i');
        const badge = el.querySelector('.badge');

        if (icon) {
            icon.className = '';
            if (task.status === 'completed') {
                icon.className = 'fas fa-check-circle fa-lg text-success';
            } else if (task.status === 'in_progress') {
                icon.className = 'fas fa-spinner fa-spin fa-lg text-primary';
            } else if (task.status === 'failed') {
                icon.className = 'fas fa-times-circle fa-lg text-danger';
            } else {
                icon.className = 'far fa-circle fa-lg text-muted';
            }
        }

        if (badge) {
            const colors = {
                completed: 'success',
                in_progress: 'primary',
                failed: 'danger',
                pending: 'secondary',
            };
            badge.className = 'badge bg-' + (colors[task.status] || 'secondary');
            badge.textContent = task.status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        }
    }

    // Start polling
    currentStatus = document.querySelector('.ai-ops-pipeline-step.active') ?
        null : 'completed';
    setInterval(pollStatus, POLL_INTERVAL);
    pollStatus(); // Initial check
})();

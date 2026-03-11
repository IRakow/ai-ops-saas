/**
 * Notes Intake — IIFE
 * Floating notepad button for submitting feedback/ideas.
 * Mirrors bug-intake.js pattern.
 */
(function() {
    'use strict';

    try { _notesIntakeInit(); } catch(e) { /* silent */ }

    function _notesIntakeInit() {
        var SUBMIT_URL = '/ai-ops/api/notes';

        // ── Page Info Collector ─────────────────────────────────
        function _collectPageInfo() {
            var info = {
                page_url: window.location.pathname + window.location.hash,
                page_title: document.title || ''
            };
            try {
                var h1 = document.querySelector('h1');
                if (h1) info.page_title = h1.textContent.trim().substring(0, 200);
            } catch(e) { /* silent */ }
            return info;
        }

        // ── Modal Logic ────────────────────────────────────────
        function _openModal() {
            var overlay = document.getElementById('notes-intake-modal-overlay');
            if (!overlay) return;
            overlay.classList.add('active');

            var pageInfo = _collectPageInfo();
            var infoEl = document.getElementById('notes-intake-page-info');
            if (infoEl) {
                infoEl.textContent = 'Page: ' + pageInfo.page_url;
            }
        }

        function _closeModal() {
            var overlay = document.getElementById('notes-intake-modal-overlay');
            if (overlay) overlay.classList.remove('active');
            var textarea = document.getElementById('notes-intake-description');
            if (textarea) textarea.value = '';
        }

        function _submitModal() {
            var textarea = document.getElementById('notes-intake-description');
            var content = textarea ? textarea.value.trim() : '';

            if (!content) {
                _showToast('Please write something before submitting.', 'error', 4000);
                return;
            }

            var submitBtn = document.getElementById('notes-intake-submit-btn');
            if (submitBtn) submitBtn.disabled = true;

            var pageInfo = _collectPageInfo();
            var payload = {
                content: content,
                page_url: pageInfo.page_url,
                page_title: pageInfo.page_title
            };

            var xhr = new XMLHttpRequest();
            xhr.open('POST', SUBMIT_URL, true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onload = function() {
                try {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        _showToast('Note submitted! Thanks for the feedback.', 'success', 5000);
                    } else {
                        var resp = JSON.parse(xhr.responseText);
                        _showToast(resp.error || 'Failed to submit note.', 'error', 5000);
                    }
                } catch(e) {
                    _showToast('Failed to submit note.', 'error', 5000);
                }
                if (submitBtn) submitBtn.disabled = false;
            };
            xhr.onerror = function() {
                _showToast('Network error. Please try again.', 'error', 5000);
                if (submitBtn) submitBtn.disabled = false;
            };
            xhr.send(JSON.stringify(payload));
            _closeModal();
        }

        // ── Toast (reuse bug-intake container) ─────────────────
        function _showToast(message, type, duration) {
            try {
                var container = document.getElementById('bug-intake-toasts');
                if (!container) return;
                var toast = document.createElement('div');
                toast.className = 'bug-intake-toast ' + (type || 'info');
                toast.textContent = message;
                container.appendChild(toast);
                setTimeout(function() {
                    try { container.removeChild(toast); } catch(e) {}
                }, duration || 5000);
            } catch(e) { /* silent */ }
        }

        // ── Bind UI ────────────────────────────────────────────
        function _bindUI() {
            var btn = document.getElementById('notes-intake-btn');
            if (btn) btn.addEventListener('click', _openModal);

            var cancelBtn = document.getElementById('notes-intake-cancel-btn');
            if (cancelBtn) cancelBtn.addEventListener('click', _closeModal);

            var submitBtn = document.getElementById('notes-intake-submit-btn');
            if (submitBtn) submitBtn.addEventListener('click', _submitModal);

            var overlay = document.getElementById('notes-intake-modal-overlay');
            if (overlay) {
                overlay.addEventListener('click', function(e) {
                    if (e.target === overlay) _closeModal();
                });
            }

            // Escape key
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    var ov = document.getElementById('notes-intake-modal-overlay');
                    if (ov && ov.classList.contains('active')) _closeModal();
                }
            });
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _bindUI);
        } else {
            _bindUI();
        }
    }
})();

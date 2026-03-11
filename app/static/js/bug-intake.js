/**
 * Bug Intake System — IIFE
 *
 * Auto-detects JS errors, promise rejections, and HTTP 500s.
 * Provides floating 🐛 button for manual reports.
 * Captures screenshots via html2canvas, deduplicates client-side,
 * and polls for fix status.
 */
(function() {
    'use strict';

    // Self-protection: everything in try/catch
    try { _bugIntakeInit(); } catch(e) { /* silent */ }

    function _bugIntakeInit() {
        // ── Config ──────────────────────────────────────────────
        var cfg = window.AI_OPS_CONFIG || {};
        var REPORT_URL  = cfg.endpoint || '/api/bug-intake/report';
        var STATUS_URL  = cfg.statusEndpoint || '/api/bug-intake/status';
        var API_KEY     = cfg.apiKey || null;
        var APP_NAME    = cfg.appName || null;
        var DEDUP_MS    = 30 * 60 * 1000; // 30 minutes
        var POLL_MS     = 30 * 1000;      // 30 seconds
        var CONSOLE_MAX = 20;
        var NETWORK_MAX = 10;

        // ── State ───────────────────────────────────────────────
        var consoleTail   = [];
        var networkErrors = [];
        var dedupMap      = {};  // fingerprint -> timestamp
        var activeBugs    = {}; // bug_id -> session_token
        var sessionToken  = null;
        var pollTimer     = null;

        // ── Console Interceptor ─────────────────────────────────
        var origError = console.error;
        var origWarn  = console.warn;

        console.error = function() {
            _pushConsole('error', arguments);
            return origError.apply(console, arguments);
        };
        console.warn = function() {
            _pushConsole('warn', arguments);
            return origWarn.apply(console, arguments);
        };

        function _pushConsole(level, args) {
            try {
                var msg = Array.prototype.slice.call(args).map(function(a) {
                    return typeof a === 'object' ? JSON.stringify(a).substring(0, 500) : String(a);
                }).join(' ');
                consoleTail.push({ level: level, message: msg.substring(0, 1000), ts: Date.now() });
                if (consoleTail.length > CONSOLE_MAX) consoleTail.shift();
            } catch(e) { /* silent */ }
        }

        // ── Network Interceptor ─────────────────────────────────
        // Chain after existing fetch wrapper in base.html
        var _prevFetch = window.fetch;
        window.fetch = function() {
            var args = arguments;
            var url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';

            return _prevFetch.apply(window, args).then(function(response) {
                try {
                    if (response.status >= 400) {
                        networkErrors.push({
                            url: url.substring(0, 500),
                            status: response.status,
                            ts: Date.now()
                        });
                        if (networkErrors.length > NETWORK_MAX) networkErrors.shift();

                        // Auto-report 500+ server errors (but not from bug-intake itself)
                        if (response.status >= 500 && url.indexOf('/api/bug-intake/') === -1) {
                            _autoReport({
                                error_type: 'http_' + response.status,
                                error_message: 'Server error ' + response.status + ' on ' + url,
                                http_status: response.status,
                            });
                        }
                    }
                } catch(e) { /* silent */ }
                return response;
            });
        };

        // ── Auto-Detect: JS Errors ─────────────────────────────
        window.addEventListener('error', function(evt) {
            try {
                // Skip errors from bug-intake itself
                if (evt.filename && evt.filename.indexOf('bug-intake') !== -1) return;

                _autoReport({
                    error_type: 'js_error',
                    error_message: evt.message || 'Unknown JS error',
                    js_stack_trace: evt.error && evt.error.stack ? evt.error.stack : '',
                });
            } catch(e) { /* silent */ }
        });

        // ── Auto-Detect: Unhandled Promise Rejections ───────────
        window.addEventListener('unhandledrejection', function(evt) {
            try {
                var msg = '';
                if (evt.reason) {
                    msg = evt.reason.message || evt.reason.toString();
                }
                _autoReport({
                    error_type: 'unhandled_rejection',
                    error_message: msg || 'Unhandled promise rejection',
                    js_stack_trace: evt.reason && evt.reason.stack ? evt.reason.stack : '',
                });
            } catch(e) { /* silent */ }
        });

        // ── Client-Side Dedup ───────────────────────────────────
        function _fingerprint(msg, path) {
            // Simple hash: normalize and concat
            var normalized = (msg || '').replace(/:\d+:\d+/g, ':X:X')
                .replace(/[0-9a-f]{8}-[0-9a-f]{4}/gi, '<UUID>')
                .replace(/0x[0-9a-f]+/gi, '<HEX>')
                .toLowerCase().trim();
            return normalized + '|' + (path || '').split('?')[0].toLowerCase();
        }

        function _isDuplicate(fp) {
            var now = Date.now();
            if (dedupMap[fp] && (now - dedupMap[fp]) < DEDUP_MS) return true;
            dedupMap[fp] = now;
            // Prune old entries
            for (var k in dedupMap) {
                if (now - dedupMap[k] > DEDUP_MS) delete dedupMap[k];
            }
            return false;
        }

        // ── Screenshot Capture ──────────────────────────────────
        function _captureScreenshot(callback) {
            if (typeof html2canvas !== 'function') {
                callback(null);
                return;
            }
            try {
                html2canvas(document.body, {
                    scale: 0.5,
                    height: Math.min(window.innerHeight, 2000),
                    logging: false,
                    useCORS: true,
                }).then(function(canvas) {
                    try {
                        callback(canvas.toDataURL('image/png', 0.7));
                    } catch(e) { callback(null); }
                }).catch(function() { callback(null); });
            } catch(e) { callback(null); }
        }

        // ── Context Collector ───────────────────────────────────
        function _collectContext() {
            var ctx = {
                url_path: window.location.pathname + window.location.hash,
                user_agent: navigator.userAgent,
                viewport: window.innerWidth + 'x' + window.innerHeight,
                console_log_tail: consoleTail.slice(),
                network_errors: networkErrors.slice(),
                local_storage_snapshot: _safeLocalStorage(),
                page_html_snippet: _pageSnippet(),
            };
            return ctx;
        }

        function _safeLocalStorage() {
            try {
                var snap = {};
                var skip = ['token', 'refresh_token', 'access_token', 'password', 'secret'];
                for (var i = 0; i < localStorage.length && i < 20; i++) {
                    var key = localStorage.key(i);
                    var lower = key.toLowerCase();
                    var shouldSkip = false;
                    for (var s = 0; s < skip.length; s++) {
                        if (lower.indexOf(skip[s]) !== -1) { shouldSkip = true; break; }
                    }
                    if (!shouldSkip) {
                        snap[key] = (localStorage.getItem(key) || '').substring(0, 200);
                    }
                }
                return snap;
            } catch(e) { return {}; }
        }

        function _pageSnippet() {
            try {
                var title = document.title || '';
                var h1 = document.querySelector('h1');
                var h1Text = h1 ? h1.textContent.trim().substring(0, 200) : '';
                return 'Title: ' + title + ' | H1: ' + h1Text;
            } catch(e) { return ''; }
        }

        // ── Auto-Report (no user interaction) ───────────────────
        function _autoReport(errorData) {
            try {
                var fp = _fingerprint(errorData.error_message, window.location.pathname);
                if (_isDuplicate(fp)) return;

                var ctx = _collectContext();
                var payload = Object.assign({}, ctx, errorData, { source: 'auto_detect' });

                // Show toast immediately
                _showToast('We caught a bug. Fixing it now.', 'info', 5000);

                // Capture screenshot then send
                _captureScreenshot(function(screenshot) {
                    if (screenshot) payload.screenshot_base64 = screenshot;
                    _sendReport(payload);
                });
            } catch(e) { /* silent */ }
        }

        // ── Send Report to Server ───────────────────────────────
        function _sendReport(payload) {
            try {
                // Use XMLHttpRequest to avoid re-triggering our fetch interceptor
                if (APP_NAME) payload.app_name = APP_NAME;

                var xhr = new XMLHttpRequest();
                xhr.open('POST', REPORT_URL, true);
                xhr.setRequestHeader('Content-Type', 'application/json');
                if (API_KEY) xhr.setRequestHeader('X-API-Key', API_KEY);
                xhr.onload = function() {
                    try {
                        if (xhr.status >= 200 && xhr.status < 300) {
                            var resp = JSON.parse(xhr.responseText);
                            if (resp.session_token) sessionToken = resp.session_token;
                            if (resp.bug_id && !resp.is_duplicate) {
                                activeBugs[resp.bug_id] = resp.session_token;
                                _startPolling();
                            }
                        }
                    } catch(e) { /* silent */ }
                };
                xhr.send(JSON.stringify(payload));
            } catch(e) { /* silent */ }
        }

        // ── Status Polling ──────────────────────────────────────
        function _startPolling() {
            if (pollTimer) return;
            pollTimer = setInterval(function() {
                try {
                    var ids = Object.keys(activeBugs);
                    if (ids.length === 0) {
                        clearInterval(pollTimer);
                        pollTimer = null;
                        return;
                    }
                    // Poll by session token (gets all bugs for this browser session)
                    var token = sessionToken || activeBugs[ids[0]];
                    if (!token) return;

                    var xhr = new XMLHttpRequest();
                    xhr.open('GET', STATUS_URL + '?session_token=' + token, true);
                    if (API_KEY) xhr.setRequestHeader('X-API-Key', API_KEY);
                    xhr.onload = function() {
                        try {
                            if (xhr.status !== 200) return;
                            var data = JSON.parse(xhr.responseText);
                            var reports = data.reports || [];
                            for (var i = 0; i < reports.length; i++) {
                                var r = reports[i];
                                if (r.status === 'fixed' || r.status === 'deployed') {
                                    _showToast('Bug fixed! ' + (r.status_message || ''), 'success', 8000);
                                    delete activeBugs[r.id];
                                } else if (r.status === 'failed' || r.status === 'escalated') {
                                    _showToast(r.status_message || 'Fix attempt needs attention.', 'error', 8000);
                                    delete activeBugs[r.id];
                                }
                            }
                        } catch(e) { /* silent */ }
                    };
                    xhr.send();
                } catch(e) { /* silent */ }
            }, POLL_MS);
        }

        // ── Toast Display ───────────────────────────────────────
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

        // ── Modal Logic ─────────────────────────────────────────
        function _openModal() {
            var overlay = document.getElementById('bug-intake-modal-overlay');
            if (!overlay) return;
            overlay.classList.add('active');

            // Capture screenshot for preview
            var previewContainer = document.getElementById('bug-intake-screenshot-preview');
            if (previewContainer) {
                previewContainer.style.display = 'none';
                previewContainer.innerHTML = '';
            }

            _captureScreenshot(function(screenshot) {
                if (screenshot && previewContainer) {
                    var img = document.createElement('img');
                    img.src = screenshot;
                    previewContainer.innerHTML = '';
                    previewContainer.appendChild(img);
                    previewContainer.style.display = 'block';
                    // Store for submit
                    previewContainer.dataset.screenshot = screenshot;
                }
            });
        }

        function _closeModal() {
            var overlay = document.getElementById('bug-intake-modal-overlay');
            if (overlay) overlay.classList.remove('active');
            var textarea = document.getElementById('bug-intake-description');
            if (textarea) textarea.value = '';
        }

        function _submitModal() {
            var textarea = document.getElementById('bug-intake-description');
            var description = textarea ? textarea.value.trim() : '';
            var previewContainer = document.getElementById('bug-intake-screenshot-preview');
            var screenshot = previewContainer ? previewContainer.dataset.screenshot || null : null;

            var submitBtn = document.getElementById('bug-intake-submit-btn');
            if (submitBtn) submitBtn.disabled = true;

            var ctx = _collectContext();
            var payload = Object.assign({}, ctx, {
                source: 'user_report',
                error_type: 'user_report',
                error_message: description || 'User-reported bug (no description)',
                user_description: description,
            });
            if (screenshot) payload.screenshot_base64 = screenshot;

            _sendReport(payload);
            _closeModal();
            _showToast('Bug report submitted. We\'re on it!', 'info', 5000);

            if (submitBtn) submitBtn.disabled = false;
        }

        // ── Feature Request Modal Logic ──────────────────────────
        function _openFeatureModal() {
            var overlay = document.getElementById('feature-request-modal-overlay');
            if (!overlay) return;
            overlay.classList.add('active');
        }

        function _closeFeatureModal() {
            var overlay = document.getElementById('feature-request-modal-overlay');
            if (overlay) overlay.classList.remove('active');
            var textarea = document.getElementById('feature-request-description');
            if (textarea) textarea.value = '';
        }

        function _submitFeatureModal() {
            var textarea = document.getElementById('feature-request-description');
            var description = textarea ? textarea.value.trim() : '';

            if (!description) {
                _showToast('Please describe the feature you need.', 'error', 4000);
                return;
            }

            var submitBtn = document.getElementById('feature-request-submit-btn');
            if (submitBtn) submitBtn.disabled = true;

            var ctx = _collectContext();
            var payload = Object.assign({}, ctx, {
                source: 'feature_request',
                error_type: 'feature_request',
                error_message: description,
                user_description: description,
            });

            _sendReport(payload);
            _closeFeatureModal();
            _showToast('Feature request submitted! We\'ll review it soon.', 'success', 5000);

            if (submitBtn) submitBtn.disabled = false;
        }

        // ── Bind UI Events (after DOM ready) ────────────────────
        function _bindUI() {
            var btn = document.getElementById('bug-intake-btn');
            if (btn) btn.addEventListener('click', _openModal);

            var cancelBtn = document.getElementById('bug-intake-cancel-btn');
            if (cancelBtn) cancelBtn.addEventListener('click', _closeModal);

            var submitBtn = document.getElementById('bug-intake-submit-btn');
            if (submitBtn) submitBtn.addEventListener('click', _submitModal);

            var overlay = document.getElementById('bug-intake-modal-overlay');
            if (overlay) {
                overlay.addEventListener('click', function(e) {
                    if (e.target === overlay) _closeModal();
                });
            }

            // Feature request bindings
            var featureBtn = document.getElementById('feature-request-btn');
            if (featureBtn) featureBtn.addEventListener('click', _openFeatureModal);

            var featureCancelBtn = document.getElementById('feature-request-cancel-btn');
            if (featureCancelBtn) featureCancelBtn.addEventListener('click', _closeFeatureModal);

            var featureSubmitBtn = document.getElementById('feature-request-submit-btn');
            if (featureSubmitBtn) featureSubmitBtn.addEventListener('click', _submitFeatureModal);

            var featureOverlay = document.getElementById('feature-request-modal-overlay');
            if (featureOverlay) {
                featureOverlay.addEventListener('click', function(e) {
                    if (e.target === featureOverlay) _closeFeatureModal();
                });
            }
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _bindUI);
        } else {
            _bindUI();
        }
    }
})();

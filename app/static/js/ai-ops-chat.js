/**
 * AI Ops Chat Interface
 * Handles message sending, polling, drag-and-drop file upload, and plan approval.
 */

(function () {
    'use strict';

    // --- State ---
    let lastMessageId = null;
    let pollInterval = null;
    let isTyping = false;

    // Initialize last message ID from existing messages
    const existingMessages = document.querySelectorAll('.ai-ops-message[data-message-id]');
    if (existingMessages.length > 0) {
        lastMessageId = existingMessages[existingMessages.length - 1].dataset.messageId;
    }

    // --- DOM ---
    const chatMessages = document.getElementById('chat-messages');
    const chatForm = document.getElementById('chat-form');
    const messageInput = document.getElementById('message-input');
    const sendBtn = document.getElementById('send-btn');
    const fileInput = document.getElementById('file-input');
    const dropzone = document.getElementById('dropzone');
    const approveBtn = document.getElementById('approve-btn');
    const emptyState = document.getElementById('empty-state');

    // --- Send Message ---
    if (chatForm) {
        chatForm.addEventListener('submit', function (e) {
            e.preventDefault();
            sendMessage();
        });
    }

    if (messageInput) {
        messageInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
    }

    function sendMessage() {
        const content = messageInput.value.trim();
        if (!content || isTyping) return;

        // Hide empty state
        if (emptyState) emptyState.style.display = 'none';

        // Add user message to UI immediately
        appendMessage({
            sender_type: 'user',
            sender_name: 'You',
            content: content,
            created_at: new Date().toISOString(),
        });

        // Clear input
        messageInput.value = '';
        messageInput.style.height = 'auto';

        // Disable input while processing
        setInputEnabled(false);
        showTypingIndicator();

        // Send to server
        fetch(`/ai-ops/api/messages/${SESSION_ID}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': CSRF_TOKEN,
                'X-Requested-With': 'XMLHttpRequest',
            },
            body: JSON.stringify({ content: content }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    appendMessage({
                        sender_type: 'system',
                        sender_name: 'System',
                        content: 'Error: ' + data.error,
                    });
                }

                if (data.status === 'approved') {
                    window.location.href = `/ai-ops/session/${SESSION_ID}/status`;
                }

                // Start aggressive polling for response
                startPolling(2000);
            })
            .catch(err => {
                appendMessage({
                    sender_type: 'system',
                    sender_name: 'System',
                    content: 'Failed to send message. Please try again.',
                });
            })
            .finally(() => {
                setInputEnabled(true);
                hideTypingIndicator();
                messageInput.focus();
                checkShowSubmitButton();
            });
    }

    // --- Approve Plan ---
    if (approveBtn) {
        approveBtn.addEventListener('click', function () {
            approveBtn.disabled = true;
            approveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Approving...';

            fetch(`/ai-ops/api/session/${SESSION_ID}/approve`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': CSRF_TOKEN,
                    'X-Requested-With': 'XMLHttpRequest',
                },
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'approved' || data.status === 'queued') {
                        window.location.reload();
                    } else {
                        approveBtn.disabled = false;
                        approveBtn.innerHTML = '<i class="fas fa-check"></i> Approve Plan';
                    }
                })
                .catch(() => {
                    approveBtn.disabled = false;
                    approveBtn.innerHTML = '<i class="fas fa-check"></i> Approve Plan';
                });
        });
    }

    // --- Submit to Agent ---
    const submitAgentBtn = document.getElementById('submit-agent-btn');
    const submitAgentArea = document.getElementById('submit-agent-area');
    if (submitAgentBtn) {
        submitAgentBtn.addEventListener('click', function () {
            submitAgentBtn.disabled = true;
            submitAgentBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Submitting...';

            const autoApproveCheckbox = document.getElementById('auto-approve-checkbox');
            const autoApprove = autoApproveCheckbox ? autoApproveCheckbox.checked : false;

            fetch(`/ai-ops/api/session/${SESSION_ID}/submit`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': CSRF_TOKEN,
                    'X-Requested-With': 'XMLHttpRequest',
                },
                body: JSON.stringify({ auto_approve: autoApprove }),
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'queued') {
                        window.location.reload();
                    } else if (data.error) {
                        alert('Error: ' + data.error);
                        submitAgentBtn.disabled = false;
                        submitAgentBtn.innerHTML = '<i class="fas fa-rocket"></i> Submit to Agent';
                    }
                })
                .catch(() => {
                    submitAgentBtn.disabled = false;
                    submitAgentBtn.innerHTML = '<i class="fas fa-rocket"></i> Submit to Agent';
                });
        });
    }

    // --- Approve Test (deploy to production) ---
    const approveTestBtn = document.getElementById('approve-test-btn');
    if (approveTestBtn) {
        approveTestBtn.addEventListener('click', function () {
            approveTestBtn.disabled = true;
            approveTestBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Deploying...';

            fetch(`/ai-ops/api/session/${SESSION_ID}/approve-test`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': CSRF_TOKEN,
                    'X-Requested-With': 'XMLHttpRequest',
                },
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'deploying' || data.status === 'completed') {
                        window.location.reload();
                    } else if (data.error) {
                        alert('Error: ' + data.error);
                        approveTestBtn.disabled = false;
                        approveTestBtn.innerHTML = '<i class="fas fa-rocket"></i> Deploy to Production';
                    }
                })
                .catch(() => {
                    approveTestBtn.disabled = false;
                    approveTestBtn.innerHTML = '<i class="fas fa-rocket"></i> Deploy to Production';
                });
        });
    }

    // Show "Submit to Agent" button after first message is sent
    function checkShowSubmitButton() {
        if (submitAgentArea && SESSION_STATUS === 'gathering_info') {
            const msgs = document.querySelectorAll('.ai-ops-message-user');
            if (msgs.length > 0) {
                submitAgentArea.style.display = 'flex';
            }
        }
    }
    checkShowSubmitButton();

    // --- File Upload ---
    if (fileInput) {
        fileInput.addEventListener('change', function () {
            if (this.files.length > 0) {
                uploadFile(this.files[0]);
            }
        });
    }

    // Drag and drop
    if (dropzone) {
        ['dragenter', 'dragover'].forEach(evt => {
            dropzone.addEventListener(evt, function (e) {
                e.preventDefault();
                dropzone.classList.add('dragover');
            });
        });

        ['dragleave', 'drop'].forEach(evt => {
            dropzone.addEventListener(evt, function (e) {
                e.preventDefault();
                dropzone.classList.remove('dragover');
            });
        });

        dropzone.addEventListener('drop', function (e) {
            if (e.dataTransfer.files.length > 0) {
                uploadFile(e.dataTransfer.files[0]);
            }
        });
    }

    function uploadFile(file) {
        const formData = new FormData();
        formData.append('file', file);

        appendMessage({
            sender_type: 'system',
            sender_name: 'System',
            content: `Uploading ${file.name}...`,
        });

        fetch(`/ai-ops/api/session/${SESSION_ID}/upload`, {
            method: 'POST',
            headers: {
                'X-CSRFToken': CSRF_TOKEN,
                'X-Requested-With': 'XMLHttpRequest',
            },
            body: formData,
        })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    appendMessage({
                        sender_type: 'system',
                        sender_name: 'System',
                        content: 'Upload failed: ' + data.error,
                    });
                } else {
                    appendMessage({
                        sender_type: 'system',
                        sender_name: 'System',
                        content: `Attached: ${file.name}`,
                    });
                    // Dynamically add file to sidebar
                    addFileToSidebar(data.file);
                }
            })
            .catch(() => {
                appendMessage({
                    sender_type: 'system',
                    sender_name: 'System',
                    content: 'Upload failed. Please try again.',
                });
            })
            .finally(() => {
                // Reset file input so same file can be re-uploaded
                if (fileInput) fileInput.value = '';
            });
    }

    function addFileToSidebar(fileRecord) {
        // Find or create the attachments section in the sidebar
        let attachSection = document.getElementById('attachments-section');
        if (!attachSection) {
            const sidebar = document.querySelector('.ai-ops-sidebar');
            if (!sidebar) return;
            attachSection = document.createElement('div');
            attachSection.id = 'attachments-section';
            attachSection.className = 'mb-3';
            attachSection.innerHTML = '<small class="text-muted text-uppercase fw-bold">Attachments</small><div class="mt-1" id="attachments-list"></div>';
            sidebar.appendChild(attachSection);
        }
        let attachList = document.getElementById('attachments-list');
        if (!attachList) {
            attachList = document.createElement('div');
            attachList.id = 'attachments-list';
            attachList.className = 'mt-1';
            attachSection.appendChild(attachList);
        }
        const fileDiv = document.createElement('div');
        fileDiv.className = 'small';
        const url = fileRecord.gcs_url || fileRecord.local_url;
        if (url) {
            fileDiv.innerHTML = `<i class="fas fa-paperclip"></i> <a href="${escapeHtml(url)}" target="_blank">${escapeHtml(fileRecord.filename)}</a>`;
        } else {
            fileDiv.innerHTML = `<i class="fas fa-paperclip"></i> ${escapeHtml(fileRecord.filename)}`;
        }
        attachList.appendChild(fileDiv);
    }

    // --- Polling ---
    function startPolling(interval) {
        stopPolling();
        pollInterval = setInterval(pollMessages, interval || 5000);
    }

    function stopPolling() {
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    }

    function pollMessages() {
        let url = `/ai-ops/api/messages/${SESSION_ID}`;
        if (lastMessageId) {
            url += `?after_id=${lastMessageId}`;
        }

        fetch(url, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then(r => r.json())
            .then(data => {
                if (data.messages && data.messages.length > 0) {
                    hideTypingIndicator();
                    data.messages.forEach(msg => {
                        // Don't duplicate messages already shown
                        if (!document.querySelector(`[data-message-id="${msg.id}"]`)) {
                            appendMessage(msg);
                            lastMessageId = msg.id;
                        }
                    });

                    // Slow down polling after getting a response
                    startPolling(5000);
                }
            })
            .catch(() => {
                // Silently fail on poll errors
            });
    }

    // Start background polling
    if (['gathering_info', 'queued', 'running', 'awaiting_approval', 'awaiting_test_approval'].includes(SESSION_STATUS)) {
        startPolling(5000);
    }

    // Also poll for status changes
    if (['queued', 'running', 'coding', 'testing', 'deploying_staging'].includes(SESSION_STATUS)) {
        setInterval(function () {
            fetch(`/ai-ops/api/session/${SESSION_ID}/status`, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status !== SESSION_STATUS) {
                        window.location.reload();
                    }
                });
        }, 10000);
    }

    // --- UI Helpers ---
    function appendMessage(msg) {
        if (emptyState) emptyState.style.display = 'none';

        const div = document.createElement('div');
        div.className = `ai-ops-message ai-ops-message-${msg.sender_type}`;
        if (msg.id) {
            div.dataset.messageId = msg.id;
        }
        const msgType = msg.message_type || 'chat';
        div.dataset.messageType = msgType;

        const icon = msg.sender_type === 'user' ? 'fa-user' :
            msg.sender_type === 'agent' ? 'fa-robot' : 'fa-cog';

        const time = msg.created_at ?
            msg.created_at.substring(0, 16).replace('T', ' ') : '';

        // Badge for message type
        let badge = '';
        if (msgType === 'plan' && typeof USER_ROLE !== 'undefined' && USER_ROLE === 'admin') {
            badge = '<span class="badge bg-info ms-2" style="font-size: 0.7em;">Full Technical Analysis</span>';
        } else if (msgType === 'user_summary') {
            badge = '<span class="badge bg-success ms-2" style="font-size: 0.7em;">Summary</span>';
        }

        div.innerHTML = `
            <div class="ai-ops-message-header">
                <i class="fas ${icon}"></i>
                <strong>${escapeHtml(msg.sender_name || '')}</strong>
                ${badge}
                <small class="text-muted ms-2">${time}</small>
            </div>
            <div class="ai-ops-message-body">
                ${escapeHtml(msg.content || '').replace(/\n/g, '<br>')}
            </div>
        `;

        chatMessages.appendChild(div);

        // Admin divider after technical analysis
        if (msgType === 'plan' && typeof USER_ROLE !== 'undefined' && USER_ROLE === 'admin') {
            const divider = document.createElement('div');
            divider.className = 'text-center py-2 text-muted small';
            divider.style.cssText = 'border-top: 1px dashed #ccc; border-bottom: 1px dashed #ccc; margin: 0.5rem 0;';
            divider.innerHTML = '<i class="fas fa-eye"></i> What users see below';
            chatMessages.appendChild(divider);
        }

        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function showTypingIndicator() {
        isTyping = true;
        let indicator = document.getElementById('typing-indicator');
        if (!indicator) {
            indicator = document.createElement('div');
            indicator.id = 'typing-indicator';
            indicator.className = 'ai-ops-message ai-ops-message-agent';
            indicator.innerHTML = `
                <div class="ai-ops-message-header">
                    <i class="fas fa-robot"></i>
                    <strong>AI Agent</strong>
                </div>
                <div class="ai-ops-typing">
                    <span></span><span></span><span></span>
                </div>
            `;
            chatMessages.appendChild(indicator);
        }
        indicator.style.display = 'block';
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function hideTypingIndicator() {
        isTyping = false;
        const indicator = document.getElementById('typing-indicator');
        if (indicator) {
            indicator.remove();
        }
    }

    function setInputEnabled(enabled) {
        if (messageInput) messageInput.disabled = !enabled;
        if (sendBtn) sendBtn.disabled = !enabled;
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
})();

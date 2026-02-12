/* ============================================
   DOWN_VIDEO Pipeline — App Logic
   ============================================ */

// State management
const state = {
    taskIds: { 1: null, 2: null, 3: null },
    eventSources: { 1: null, 2: null, 3: null },
    statuses: { 1: 'idle', 2: 'idle', 3: 'idle' },
};

// ============================================================
//  FOLDER ID PREVIEW (real-time)
// ============================================================
document.getElementById('gdrive-link').addEventListener('input', function () {
    const val = this.value.trim();
    const preview = document.getElementById('folder-id-preview');
    const folderId = parseFolderId(val);
    if (folderId) {
        preview.textContent = `📂 Folder ID: ${folderId}`;
        preview.style.color = 'var(--accent-green)';
    } else if (val.length > 0) {
        preview.textContent = '⚠️ Không nhận diện được Folder ID';
        preview.style.color = 'var(--accent-amber)';
    } else {
        preview.textContent = '';
    }
});

function parseFolderId(link) {
    if (!link) return null;
    // Full URL
    const m = link.match(/\/folders\/([a-zA-Z0-9_-]+)/);
    if (m) return m[1];
    // Raw ID
    if (/^[a-zA-Z0-9_-]{10,}$/.test(link.trim())) return link.trim();
    return null;
}

// ============================================================
//  RUN SCRIPTS
// ============================================================
function runGetLinks() {
    const link = document.getElementById('gdrive-link').value.trim();
    if (!link) {
        alert('Vui lòng nhập link Google Drive folder!');
        document.getElementById('gdrive-link').focus();
        return;
    }

    const folderId = parseFolderId(link);
    if (!folderId) {
        alert('Link không hợp lệ! Hãy nhập link folder GDrive hoặc Folder ID.');
        return;
    }

    startTask(1, '/api/run/getlinks', { gdrive_link: link, folder_name: document.getElementById('folder-name').value.trim() });
}

function runRemove() {
    startTask(2, '/api/run/remove', {});
}

function runSync() {
    const dryRun = document.getElementById('dry-run-toggle').checked;
    startTask(3, '/api/run/sync', { dry_run: dryRun });
}

async function startTask(stepNum, endpoint, body) {
    // Disable button
    const btnId = { 1: 'btn-getlinks', 2: 'btn-remove', 3: 'btn-sync' }[stepNum];
    const btn = document.getElementById(btnId);
    btn.disabled = true;
    btn.classList.add('loading');

    // Show stop button
    document.getElementById(`btn-stop-${stepNum}`).classList.remove('hidden');

    // Show log panel
    const logPanel = document.getElementById(`log-panel-${stepNum}`);
    logPanel.classList.remove('hidden');

    // Clear previous logs
    document.getElementById(`log-content-${stepNum}`).innerHTML = '';

    // Update status
    setStepStatus(stepNum, 'running', 'Đang chạy...');

    try {
        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        const data = await resp.json();

        if (!resp.ok) {
            addLogLine(stepNum, 'system', new Date().toTimeString().slice(0, 8), `❌ Error: ${data.error}`);
            setStepStatus(stepNum, 'error', 'Lỗi');
            btn.disabled = false;
            btn.classList.remove('loading');
            document.getElementById(`btn-stop-${stepNum}`).classList.add('hidden');
            return;
        }

        state.taskIds[stepNum] = data.task_id;

        // Start SSE stream
        connectSSE(stepNum, data.task_id);

    } catch (err) {
        addLogLine(stepNum, 'system', new Date().toTimeString().slice(0, 8), `❌ Lỗi kết nối: ${err.message}`);
        setStepStatus(stepNum, 'error', 'Lỗi kết nối');
        btn.disabled = false;
        btn.classList.remove('loading');
        document.getElementById(`btn-stop-${stepNum}`).classList.add('hidden');
    }
}

// ============================================================
//  SSE LOG STREAMING
// ============================================================
function connectSSE(stepNum, taskId) {
    // Close existing
    if (state.eventSources[stepNum]) {
        state.eventSources[stepNum].close();
    }

    const es = new EventSource(`/api/logs/${taskId}`);
    state.eventSources[stepNum] = es;

    es.onmessage = function (event) {
        const data = JSON.parse(event.data);

        if (data.type === 'status') {
            // Final status
            const finalStatus = data.status; // "done" or "error"
            finishStep(stepNum, finalStatus);
            es.close();
            state.eventSources[stepNum] = null;
            return;
        }

        // Log entry
        addLogLine(stepNum, data.type, data.time, data.msg);
    };

    es.onerror = function () {
        // Connection closed (normal for SSE when done)
        es.close();
        state.eventSources[stepNum] = null;

        // If still "running", mark as unknown
        if (state.statuses[stepNum] === 'running') {
            finishStep(stepNum, 'error');
        }
    };
}

// ============================================================
//  LOG DISPLAY
// ============================================================
function addLogLine(stepNum, type, time, msg) {
    const container = document.getElementById(`log-content-${stepNum}`);
    const line = document.createElement('div');
    line.className = `log-line ${type}`;
    line.innerHTML = `
        <span class="log-time">${time || ''}</span>
        <span class="log-msg">${escapeHtml(msg)}</span>
    `;
    container.appendChild(line);

    // Auto-scroll
    container.scrollTop = container.scrollHeight;
}

function clearLog(stepNum) {
    document.getElementById(`log-content-${stepNum}`).innerHTML = '';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================================
//  STATUS MANAGEMENT
// ============================================================
function setStepStatus(stepNum, status, text) {
    state.statuses[stepNum] = status;

    // Step card
    const card = document.getElementById(`step-${stepNum}`);
    card.classList.remove('active', 'done', 'error');
    if (status !== 'idle') card.classList.add(status === 'running' ? 'active' : status);

    // Status badge
    const statusEl = document.getElementById(`status-${stepNum}`);
    statusEl.className = `step-status ${status}`;
    statusEl.querySelector('.status-text').textContent = text;

    // Pipeline progress dot
    const progressStep = document.getElementById(`progress-step${stepNum}`);
    progressStep.classList.remove('active', 'done', 'error');
    if (status !== 'idle') progressStep.classList.add(status === 'running' ? 'active' : status);

    // Connectors
    updateConnectors();
}

function updateConnectors() {
    const c1 = document.getElementById('connector-1');
    const c2 = document.getElementById('connector-2');

    c1.classList.remove('active', 'done');
    c2.classList.remove('active', 'done');

    if (state.statuses[1] === 'done') {
        c1.classList.add('done');
    } else if (state.statuses[2] === 'running') {
        c1.classList.add('active');
    }

    if (state.statuses[2] === 'done') {
        c2.classList.add('done');
    } else if (state.statuses[3] === 'running') {
        c2.classList.add('active');
    }
}

function finishStep(stepNum, status) {
    const btnId = { 1: 'btn-getlinks', 2: 'btn-remove', 3: 'btn-sync' }[stepNum];
    const btn = document.getElementById(btnId);
    btn.disabled = false;
    btn.classList.remove('loading');

    document.getElementById(`btn-stop-${stepNum}`).classList.add('hidden');

    const statusText = status === 'done' ? '✅ Hoàn tất' : '❌ Có lỗi';
    setStepStatus(stepNum, status, statusText);
}

// ============================================================
//  STOP TASK
// ============================================================
async function stopTask(stepNum) {
    const taskId = state.taskIds[stepNum];
    if (!taskId) return;

    try {
        await fetch(`/api/stop/${taskId}`, { method: 'POST' });
    } catch (err) {
        console.error('Stop error:', err);
    }
}

// ============================================================
//  RECONNECT TO RUNNING TASK (after page refresh)
// ============================================================
function reconnectLog(stepNum) {
    const taskId = state.taskIds[stepNum];
    if (!taskId) return;

    // Hide reconnect button
    const reconnectBtn = document.getElementById(`btn-reconnect-${stepNum}`);
    if (reconnectBtn) reconnectBtn.classList.add('hidden');

    // Show stop button
    document.getElementById(`btn-stop-${stepNum}`).classList.remove('hidden');

    // Connect SSE to continue streaming
    connectSSE(stepNum, taskId);
}

// ============================================================
//  CHECK & RESTORE STATE ON PAGE LOAD
// ============================================================
const SCRIPT_TO_STEP = { getlinks: 1, remove: 2, sync: 3 };

async function checkRunningTasks() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) return;
        const tasks = await resp.json();

        // Find the latest task per script (by most recent logs)
        const latestByScript = {};
        for (const [tid, task] of Object.entries(tasks)) {
            const script = task.script;
            if (!latestByScript[script] || task.log_count > latestByScript[script].log_count) {
                latestByScript[script] = { ...task, task_id: tid };
            }
        }

        for (const [script, task] of Object.entries(latestByScript)) {
            const stepNum = SCRIPT_TO_STEP[script];
            if (!stepNum) continue;

            state.taskIds[stepNum] = task.task_id;

            // Show log panel with recent logs
            const logPanel = document.getElementById(`log-panel-${stepNum}`);
            logPanel.classList.remove('hidden');
            const logContent = document.getElementById(`log-content-${stepNum}`);
            logContent.innerHTML = '';

            // Render recent logs
            if (task.logs) {
                for (const entry of task.logs) {
                    addLogLine(stepNum, entry.type, entry.time, entry.msg);
                }
            }

            if (task.status === 'running') {
                // Running task — show reconnect button & status
                setStepStatus(stepNum, 'running', '🔄 Đang chạy...');

                const btnId = { 1: 'btn-getlinks', 2: 'btn-remove', 3: 'btn-sync' }[stepNum];
                document.getElementById(btnId).disabled = true;
                document.getElementById(btnId).classList.add('loading');

                // Show reconnect button
                const reconnectBtn = document.getElementById(`btn-reconnect-${stepNum}`);
                if (reconnectBtn) reconnectBtn.classList.remove('hidden');

                // Show stop button
                document.getElementById(`btn-stop-${stepNum}`).classList.remove('hidden');

                // Auto-reconnect SSE
                connectSSE(stepNum, task.task_id);

            } else if (task.status === 'done') {
                setStepStatus(stepNum, 'done', '✅ Hoàn tất');
            } else if (task.status === 'error') {
                setStepStatus(stepNum, 'error', '❌ Có lỗi');
            }
        }
    } catch (err) {
        console.log('No previous tasks or server not ready:', err.message);
    }
}

// Run on page load
document.addEventListener('DOMContentLoaded', checkRunningTasks);

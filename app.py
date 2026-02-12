"""
DOWN_VIDEO Pipeline — Web UI Server
Flask server với SSE real-time log streaming.
"""

import os
import sys
import re
import json
import uuid
import signal
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response

# ===== FIX WINDOWS UNICODE =====
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

app = Flask(__name__)
WORK_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
#  PROCESS MANAGER
# ============================================================
processes = {}  # task_id -> {process, logs, status, script}
logs_lock = threading.Lock()


def stream_output(task_id, pipe, label):
    """Read subprocess output line by line and append to logs."""
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            line = line.rstrip('\n').rstrip('\r')
            timestamp = datetime.now().strftime("%H:%M:%S")
            entry = {"time": timestamp, "type": label, "msg": line}
            with logs_lock:
                if task_id in processes:
                    processes[task_id]["logs"].append(entry)
    except Exception:
        pass
    finally:
        pipe.close()


def run_script(task_id, cmd, env=None):
    """Run a script in subprocess and capture output."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    # Force UTF-8 output
    merged_env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=WORK_DIR,
            env=merged_env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0,
        )
    except Exception as e:
        with logs_lock:
            processes[task_id]["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": "stderr",
                "msg": f"❌ Failed to start process: {e}"
            })
            processes[task_id]["status"] = "error"
        return

    processes[task_id]["process"] = proc

    t_out = threading.Thread(target=stream_output, args=(task_id, proc.stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=stream_output, args=(task_id, proc.stderr, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    proc.wait()
    t_out.join(timeout=3)
    t_err.join(timeout=3)

    exit_code = proc.returncode
    with logs_lock:
        if task_id in processes:
            ts = datetime.now().strftime("%H:%M:%S")
            if exit_code == 0:
                processes[task_id]["status"] = "done"
                processes[task_id]["logs"].append({
                    "time": ts, "type": "system",
                    "msg": f"✅ Hoàn tất (exit code: {exit_code})"
                })
            else:
                processes[task_id]["status"] = "error"
                processes[task_id]["logs"].append({
                    "time": ts, "type": "system",
                    "msg": f"❌ Lỗi (exit code: {exit_code})"
                })


def parse_folder_id(gdrive_link):
    """Extract folder ID from Google Drive link or raw ID."""
    if not gdrive_link:
        return None
    # Full URL: https://drive.google.com/drive/folders/XXXXX...
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', gdrive_link)
    if m:
        return m.group(1)
    # Raw ID (alphanumeric + - + _)
    m = re.match(r'^[a-zA-Z0-9_-]{10,}$', gdrive_link.strip())
    if m:
        return gdrive_link.strip()
    return None


# ============================================================
#  ROUTES
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/run/getlinks', methods=['POST'])
def run_getlinks():
    data = request.get_json() or {}
    gdrive_link = data.get('gdrive_link', '').strip()
    folder_name = data.get('folder_name', '').strip()
    folder_id = parse_folder_id(gdrive_link)

    if not folder_id:
        return jsonify({"error": "Không tìm được Folder ID từ link. Hãy nhập link GDrive folder hợp lệ."}), 400

    task_id = str(uuid.uuid4())[:8]
    processes[task_id] = {
        "process": None,
        "logs": [{
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": "system",
            "msg": f"🚀 Bắt đầu GetLinks — Folder ID: {folder_id}" + (f" — Tên: {folder_name}" if folder_name else "")
        }],
        "status": "running",
        "script": "getlinks"
    }

    def run_and_rename(task_id, cmd, env, folder_name):
        """Run getlinks.py then update root name in output.json."""
        run_script(task_id, cmd, env=env)
        # After script finishes, update root name if provided
        if folder_name and processes.get(task_id, {}).get('status') == 'done':
            output_path = os.path.join(WORK_DIR, 'output.json')
            try:
                with open(output_path, 'r', encoding='utf-8') as f:
                    tree = json.load(f)
                tree['name'] = folder_name
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(tree, f, ensure_ascii=False, indent=2)
                with logs_lock:
                    processes[task_id]['logs'].append({
                        'time': datetime.now().strftime('%H:%M:%S'),
                        'type': 'system',
                        'msg': f'📝 Đã đổi tên folder gốc → "{folder_name}"'
                    })
            except Exception as e:
                with logs_lock:
                    processes[task_id]['logs'].append({
                        'time': datetime.now().strftime('%H:%M:%S'),
                        'type': 'stderr',
                        'msg': f'⚠️ Không sửa được tên root: {e}'
                    })

    thread = threading.Thread(
        target=run_and_rename,
        args=(task_id, [sys.executable, 'getlinks.py'], {'FOLDER_ID': folder_id}, folder_name),
        daemon=True
    )
    thread.start()

    return jsonify({"task_id": task_id, "folder_id": folder_id})


@app.route('/api/run/remove', methods=['POST'])
def run_remove():
    task_id = str(uuid.uuid4())[:8]
    processes[task_id] = {
        "process": None,
        "logs": [{
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": "system",
            "msg": "🧹 Bắt đầu Remove — Xóa các mục không cần thiết"
        }],
        "status": "running",
        "script": "remove"
    }

    thread = threading.Thread(
        target=run_script,
        args=(task_id, [sys.executable, 'remove.py']),
        daemon=True
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route('/api/run/sync', methods=['POST'])
def run_sync():
    data = request.get_json() or {}
    dry_run = data.get('dry_run', False)

    cmd = [sys.executable, 'sync_gdrive.py']
    if dry_run:
        cmd.append('--dry-run')

    task_id = str(uuid.uuid4())[:8]
    mode_label = "DRY-RUN" if dry_run else "LIVE"
    processes[task_id] = {
        "process": None,
        "logs": [{
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": "system",
            "msg": f"🔄 Bắt đầu Sync GDrive — Mode: {mode_label}"
        }],
        "status": "running",
        "script": "sync"
    }

    thread = threading.Thread(
        target=run_script,
        args=(task_id, cmd),
        daemon=True
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route('/api/logs/<task_id>')
def stream_logs(task_id):
    """SSE endpoint — stream logs in real-time."""
    def generate():
        last_idx = 0
        while True:
            with logs_lock:
                task = processes.get(task_id)
                if not task:
                    yield f"data: {json.dumps({'type': 'error', 'msg': 'Task not found'})}\n\n"
                    return

                logs = task["logs"]
                status = task["status"]

                # Send new log entries
                while last_idx < len(logs):
                    entry = logs[last_idx]
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    last_idx += 1

                # If done/error, send final status and close
                if status in ("done", "error"):
                    yield f"data: {json.dumps({'type': 'status', 'status': status})}\n\n"
                    return

            time.sleep(0.3)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/stop/<task_id>', methods=['POST'])
def stop_task(task_id):
    with logs_lock:
        task = processes.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        proc = task.get("process")
        if proc and proc.poll() is None:
            try:
                if sys.platform == 'win32':
                    proc.terminate()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                task["status"] = "error"
                task["logs"].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "type": "system",
                    "msg": "⛔ Đã dừng process"
                })
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})


@app.route('/api/status')
def get_status():
    """Get status of all tasks — includes recent logs for UI recovery."""
    result = {}
    with logs_lock:
        for tid, task in processes.items():
            # Return last 50 log lines so frontend can show recent output
            recent_logs = task["logs"][-50:]
            result[tid] = {
                "task_id": tid,
                "status": task["status"],
                "script": task["script"],
                "log_count": len(task["logs"]),
                "logs": recent_logs,
            }
    return jsonify(result)


if __name__ == '__main__':
    print(f"\n🌐 DOWN_VIDEO Pipeline UI")
    print(f"   http://localhost:5000")
    print(f"   Working dir: {WORK_DIR}\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

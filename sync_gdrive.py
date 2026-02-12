"""
Sync Google Drive content from output.json

Flow:
  1. Đọc output.json (cây thư mục/file)
  2. Folder  → rclone mkdir trên GDrive đích
  3. video/mp4 → capture_urls.py download → rename → rclone upload → xóa local
  4. File khác → download qua GDrive URL + cookies Chrome → rclone upload → xóa local

Usage:
  python sync_gdrive.py                    # Chạy bình thường
  python sync_gdrive.py --dry-run          # Chỉ xem trước, không thực thi
  python sync_gdrive.py --json other.json  # Dùng file JSON khác
"""

import json
import os
import sys
import subprocess
import time
import glob
import re
import argparse
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

# ===== FIX WINDOWS UNICODE =====
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ===== CONFIG =====
RCLONE_PATH = r"C:\Users\phamt\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.73.0-windows-amd64"
REMOTE_NAME = "gdrive:"
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(WORK_DIR, "_temp_download")
CHROME_USER_DATA = os.path.join(WORK_DIR, 'chrome_profile')
PROGRESS_FILE = os.path.join(WORK_DIR, "_sync_progress.json")
COOKIE_REFRESH_INTERVAL = 600  # 10 phút (giây)


# ============================================================
#  PROGRESS TRACKING
# ============================================================
def load_progress():
    """Load progress from file. Returns dict with 'done_ids' set."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            progress = {
                'done_ids': set(data.get('done_ids', [])),
                'failed_ids': set(data.get('failed_ids', [])),
                'created_folders': set(data.get('created_folders', [])),
            }
            return progress
        except Exception:
            pass
    return {'done_ids': set(), 'failed_ids': set(), 'created_folders': set()}


def save_progress(progress):
    """Save progress to file (atomic write to prevent corruption)."""
    data = {
        'done_ids': sorted(list(progress['done_ids'])),
        'failed_ids': sorted(list(progress['failed_ids'])),
        'created_folders': sorted(list(progress['created_folders'])),
        'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_done': len(progress['done_ids']),
        'total_failed': len(progress['failed_ids']),
    }
    tmp_path = PROGRESS_FILE + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Atomic rename (Windows: replace if exists)
        if os.path.exists(PROGRESS_FILE):
            os.replace(tmp_path, PROGRESS_FILE)
        else:
            os.rename(tmp_path, PROGRESS_FILE)
    except Exception as e:
        log("WARN", f"Không lưu được progress: {e}")


# ============================================================
#  LOGGING
# ============================================================
def log(level, msg):
    """Print log with timestamp and icon"""
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {
        "INFO": "ℹ️", "OK": "✅", "WARN": "⚠️", "ERR": "❌",
        "SKIP": "⏭️", "DL": "⬇️", "UP": "⬆️", "DIR": "📁",
        "VID": "🎬", "FILE": "📄",
    }
    icon = icons.get(level, "•")
    print(f"[{ts}] {icon} {msg}")


def log_progress(stats):
    """Print progress bar"""
    total = stats['total_files']
    done = stats['done']
    skip = stats['skipped']
    fail = stats['failed']
    pct = (done / total * 100) if total > 0 else 0
    bar_len = 20
    filled = int(bar_len * done / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\n  [{bar}] {pct:.0f}%  ({done}/{total})  skip={skip}  fail={fail}\n")


# ============================================================
#  RCLONE HELPERS
# ============================================================
def ensure_rclone_path():
    """Add rclone to PATH if needed, verify it works"""
    if RCLONE_PATH not in os.environ.get('PATH', ''):
        os.environ['PATH'] = os.environ.get('PATH', '') + ";" + RCLONE_PATH
    try:
        result = subprocess.run(
            ['rclone', 'version'], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            version_line = result.stdout.strip().split('\n')[0]
            log("OK", f"rclone ready: {version_line}")
            return True
    except Exception:
        pass
    log("ERR", f"rclone không tìm thấy! PATH: {RCLONE_PATH}")
    return False


def run_rclone(args, dry_run=False):
    """Run rclone command, return True on success"""
    cmd = ['rclone'] + args
    cmd_str = ' '.join(f'"{a}"' if ' ' in a else a for a in cmd)

    if dry_run:
        log("INFO", f"[DRY-RUN] {cmd_str}")
        return True

    log("INFO", f"$ {cmd_str}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            stderr = result.stderr.strip()[:200]
            log("ERR", f"rclone error: {stderr}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log("ERR", "rclone timeout (10 min)")
        return False
    except Exception as e:
        log("ERR", f"rclone exception: {e}")
        return False


def create_folder(remote_path, dry_run=False):
    """Create folder on remote via rclone mkdir"""
    log("DIR", f"mkdir → {REMOTE_NAME}{remote_path}")
    return run_rclone(['mkdir', f'{REMOTE_NAME}{remote_path}'], dry_run)


def file_exists_remote(remote_dir, filename, dry_run=False):
    """Check if a file already exists on remote (for resume)"""
    if dry_run:
        return False
    try:
        result = subprocess.run(
            ['rclone', 'lsf', f'{REMOTE_NAME}{remote_dir}/'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            remote_files = result.stdout.strip().split('\n')
            return filename in remote_files
    except Exception:
        pass
    return False


def upload_and_cleanup(local_path, remote_dir, dry_run=False):
    """Upload file via rclone copy, then delete local file"""
    filename = os.path.basename(local_path)
    log("UP", f"Upload: {filename} → {REMOTE_NAME}{remote_dir}/")

    success = run_rclone([
        'copy', local_path, f'{REMOTE_NAME}{remote_dir}', '--progress'
    ], dry_run)

    if success and not dry_run:
        try:
            os.remove(local_path)
            log("OK", f"Xóa local: {filename}")
        except Exception as e:
            log("WARN", f"Không xóa được local: {e}")

    return success


# ============================================================
#  CHROME COOKIES
# ============================================================
def export_cookies_to_file(browser_cookies):
    """Export browser cookies to Netscape format file for capture_urls2.py"""
    cookie_file = os.path.join(WORK_DIR, 'drive.google.com_cookies.txt')
    try:
        with open(cookie_file, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# https://curl.haxx.se/rfc/cookie_spec.html\n")
            f.write("# This is a generated file! Do not edit.\n\n")
            for c in browser_cookies:
                domain = c.get('domain', '')
                if 'google' not in domain:
                    continue
                flag = "TRUE" if domain.startswith('.') else "FALSE"
                path = c.get('path', '/')
                secure = "TRUE" if c.get('secure', False) else "FALSE"
                expires = str(int(c.get('expires', 0)))
                name = c.get('name', '')
                value = c.get('value', '')
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
        log("OK", f"Đã cập nhật cookies → {cookie_file}")
    except Exception as e:
        log("ERR", f"Không ghi được cookies file: {e}")


def get_chrome_cookies(dry_run=False):
    """Open Playwright once to extract cookies from Chrome profile"""
    if dry_run:
        log("INFO", "[DRY-RUN] Skip lấy cookies")
        return {}

    log("INFO", "Đang lấy cookies từ Chrome profile...")
    cookies_dict = {}

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                CHROME_USER_DATA,
                channel='chrome',
                headless=False,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                ],
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.goto('https://drive.google.com', timeout=30000)
            page.wait_for_timeout(3000)

            browser_cookies = context.cookies()
            for c in browser_cookies:
                domain = c.get('domain', '')
                if 'google' in domain:
                    cookies_dict[c['name']] = c['value']

            # Export cookies ra file cho capture_urls2.py
            export_cookies_to_file(browser_cookies)

            context.close()

        log("OK", f"Lấy được {len(cookies_dict)} cookies từ Chrome")
    except Exception as e:
        log("ERR", f"Lỗi lấy cookies: {e}")

    return cookies_dict


def maybe_refresh_cookies(cookie_state, dry_run=False):
    """Refresh cookies nếu đã quá COOKIE_REFRESH_INTERVAL giây"""
    if dry_run:
        return cookie_state['cookies']
    elapsed = time.time() - cookie_state['last_refresh']
    if elapsed >= COOKIE_REFRESH_INTERVAL:
        log("INFO", f"🔄 Cookies đã {elapsed/60:.0f} phút — đang làm mới...")
        new_cookies = get_chrome_cookies(dry_run=False)
        if new_cookies:
            cookie_state['cookies'] = new_cookies
            cookie_state['last_refresh'] = time.time()
            log("OK", f"Cookies mới: {len(new_cookies)} cookies")
        else:
            log("WARN", "Không lấy được cookies mới, dùng cookies cũ")
    return cookie_state['cookies']


# ============================================================
#  DOWNLOAD: NON-VIDEO FILES
# ============================================================
def download_file_direct(file_id, file_name, local_path, cookies_dict, dry_run=False):
    """Download a non-video file from Google Drive via direct URL"""
    if dry_run:
        log("DL", f"[DRY-RUN] Download {file_name} (id={file_id})")
        return True

    log("DL", f"Download: {file_name} (id={file_id[:12]}...)")

    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }

    try:
        session = requests.Session()
        response = session.get(
            url, cookies=cookies_dict, headers=headers, stream=True, timeout=120
        )

        # Handle Google virus-scan confirmation page for large files
        content_type = response.headers.get('content-type', '')
        if 'text/html' in content_type:
            page_text = response.text

            # Method 1: confirm token
            confirm = re.search(r'confirm=([0-9A-Za-z_-]+)', page_text)
            if confirm:
                url2 = f"{url}&confirm={confirm.group(1)}"
                response = session.get(
                    url2, cookies=cookies_dict, headers=headers,
                    stream=True, timeout=120
                )
            else:
                # Method 2: uuid + confirm=t
                uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', page_text)
                if uuid_m:
                    url2 = f"{url}&uuid={uuid_m.group(1)}&confirm=t"
                    response = session.get(
                        url2, cookies=cookies_dict, headers=headers,
                        stream=True, timeout=120
                    )

        # Stream to disk
        total = int(response.headers.get('content-length', 0))
        downloaded = 0

        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded / total * 100
                        print(
                            f"\r  ⬇️ {downloaded // 1024}KB / {total // 1024}KB ({pct:.0f}%)",
                            end="", flush=True
                        )

        if total > 0:
            print()  # newline after progress

        # Verify: check it's not an HTML error page
        if os.path.exists(local_path):
            fsize = os.path.getsize(local_path)
            if fsize < 1000:
                with open(local_path, 'r', errors='ignore') as f:
                    head = f.read(500)
                if '<html' in head.lower():
                    log("ERR", f"Nhận được HTML thay vì file — có thể cần login")
                    os.remove(local_path)
                    return False
            log("OK", f"Downloaded: {file_name} ({fsize // 1024}KB)")
            return True

        return False

    except Exception as e:
        log("ERR", f"Download error: {e}")
        if os.path.exists(local_path):
            os.remove(local_path)
        return False


# ============================================================
#  DOWNLOAD: VIDEO (via capture_urls.py)
# ============================================================
def download_video(gdrive_link, file_name, local_dir, dry_run=False):
    """
    Download video by calling capture_urls.py as subprocess.
    Returns local path of the downloaded file, or None on failure.
    """
    if dry_run:
        log("VID", f"[DRY-RUN] capture_urls.py {gdrive_link}")
        return os.path.join(local_dir, file_name)

    log("VID", f"Bắt đầu capture video: {file_name}")
    log("INFO", f"Link: {gdrive_link}")

    # Snapshot merged_*.mp4 files BEFORE running capture
    existing_merged = set(glob.glob(os.path.join(WORK_DIR, 'merged_*.mp4')))

    # Run capture_urls.py (let stdout/stderr print directly for debug)
    try:
        result = subprocess.run(
            [sys.executable, 'capture_urls2.py', gdrive_link],
            cwd=WORK_DIR,
            timeout=900,  # 15 min timeout
        )

        if result.returncode != 0:
            log("ERR", f"capture_urls.py exit code = {result.returncode}")
            return None
    except subprocess.TimeoutExpired:
        log("ERR", "capture_urls.py timeout (15 min)")
        return None
    except Exception as e:
        log("ERR", f"capture_urls.py exception: {e}")
        return None

    # Find new merged file
    current_merged = set(glob.glob(os.path.join(WORK_DIR, 'merged_*.mp4')))
    new_files = current_merged - existing_merged

    if not new_files:
        # Fallback: check for any merged file (maybe it existed before but was overwritten)
        all_merged = sorted(
            glob.glob(os.path.join(WORK_DIR, 'merged_*.mp4')),
            key=os.path.getmtime, reverse=True
        )
        if all_merged:
            newest = all_merged[0]
            mod_time = os.path.getmtime(newest)
            if time.time() - mod_time < 120:  # modified within last 2 min
                new_files = {newest}

    if not new_files:
        log("ERR", "Không tìm thấy file merged sau capture_urls.py")
        return None

    merged_file = list(new_files)[0]
    merged_size = os.path.getsize(merged_file) // 1024 // 1024
    log("OK", f"Tìm thấy: {os.path.basename(merged_file)} ({merged_size}MB)")

    # Rename to target name
    os.makedirs(local_dir, exist_ok=True)
    target_path = os.path.join(local_dir, file_name)

    try:
        if os.path.exists(target_path):
            os.remove(target_path)
        os.rename(merged_file, target_path)
        log("OK", f"Rename → {file_name}")
        return target_path
    except Exception as e:
        log("WARN", f"Rename failed ({e}), dùng file gốc")
        return merged_file


# ============================================================
#  TREE PROCESSING
# ============================================================
def sanitize_name(name):
    """Sanitize folder/file name for Windows paths"""
    # Replace Windows-invalid characters
    invalid = '<>:"|?*'
    for c in invalid:
        name = name.replace(c, '_')
    # Remove trailing spaces/dots
    name = name.rstrip('. ')
    if not name:
        name = '_unnamed_'
    return name


def count_files(node):
    """Count total files in tree (recursive)"""
    count = 0
    if node.get('type') == 'file':
        count = 1
    for child in node.get('children', []):
        count += count_files(child)
    return count


def process_node(node, remote_parent_path, cookie_state, stats, progress, dry_run=False, depth=0):
    """Recursively process a node from the JSON tree"""
    indent = "│ " * depth
    name = node.get('name', 'unknown')
    node_type = node.get('type', '')
    mime_type = node.get('mimeType', '')
    file_id = node.get('id', '')
    link = node.get('link', '')
    safe_name = sanitize_name(name)

    # ── FOLDER ──────────────────────────────────────────────
    if node_type == 'folder':
        if remote_parent_path:
            folder_path = f"{remote_parent_path}/{safe_name}"
        else:
            folder_path = safe_name

        children = node.get('children', [])
        child_files = sum(1 for c in children if c.get('type') == 'file')
        child_dirs = sum(1 for c in children if c.get('type') == 'folder')

        # Skip mkdir if already created in previous run
        if folder_path in progress['created_folders'] and not dry_run:
            log("SKIP", f"{indent}📁 {name} (đã tạo)")
        else:
            log("DIR", f"{indent}📁 {name}  ({child_files} files, {child_dirs} folders)")
            create_folder(folder_path, dry_run)
            if not dry_run:
                progress['created_folders'].add(folder_path)
                save_progress(progress)

        for i, child in enumerate(children):
            child_name = child.get('name', '?')
            log("INFO", f"{indent}├─ [{i + 1}/{len(children)}] {child_name}")
            process_node(child, folder_path, cookie_state, stats, progress, dry_run, depth + 1)

        return

    # ── FILE ────────────────────────────────────────────────
    if node_type == 'file':
        stats['total_files'] += 1

        # Resume check 1: skip if already done in progress file (fastest)
        if not dry_run and file_id and file_id in progress['done_ids']:
            log("SKIP", f"{indent}⏭️  {name} (đã xong)")
            stats['skipped'] += 1
            stats['done'] += 1
            log_progress(stats)
            return

        # Resume check 2: skip if already on remote (fallback)
        if not dry_run and file_exists_remote(remote_parent_path, safe_name):
            log("SKIP", f"{indent}⏭️  {name} (có trên remote)")
            stats['skipped'] += 1
            stats['done'] += 1
            # Also save to progress so next run is faster
            if file_id:
                progress['done_ids'].add(file_id)
                save_progress(progress)
            log_progress(stats)
            return

        # ── VIDEO/MP4 ──
        if mime_type == 'video/mp4':
            log("VID", f"{indent}🎬 {name}  ({node.get('sizeFormatted', '?')})")

            local_path = download_video(link, safe_name, TEMP_DIR, dry_run)
            if local_path:
                ok = upload_and_cleanup(local_path, remote_parent_path, dry_run)
                if ok:
                    stats['done'] += 1
                    log("OK", f"{indent}✅ Video done: {name}")
                    if not dry_run and file_id:
                        progress['done_ids'].add(file_id)
                        progress['failed_ids'].discard(file_id)
                        save_progress(progress)
                else:
                    stats['failed'] += 1
                    log("ERR", f"{indent}❌ Upload fail: {name}")
                    if not dry_run and file_id:
                        progress['failed_ids'].add(file_id)
                        save_progress(progress)
            else:
                stats['failed'] += 1
                log("ERR", f"{indent}❌ Video download fail: {name}")
                if not dry_run and file_id:
                    progress['failed_ids'].add(file_id)
                    save_progress(progress)

        # ── OTHER FILE ──
        else:
            log("FILE", f"{indent}📄 {name}  ({node.get('sizeFormatted', '?')}, {mime_type})")

            os.makedirs(TEMP_DIR, exist_ok=True)
            local_path = os.path.join(TEMP_DIR, safe_name)

            # Refresh cookies nếu cần
            cookies_dict = maybe_refresh_cookies(cookie_state, dry_run)

            ok = download_file_direct(file_id, safe_name, local_path, cookies_dict, dry_run)
            if ok:
                up_ok = upload_and_cleanup(local_path, remote_parent_path, dry_run)
                if up_ok:
                    stats['done'] += 1
                    log("OK", f"{indent}✅ File done: {name}")
                    if not dry_run and file_id:
                        progress['done_ids'].add(file_id)
                        progress['failed_ids'].discard(file_id)
                        save_progress(progress)
                else:
                    stats['failed'] += 1
                    log("ERR", f"{indent}❌ Upload fail: {name}")
                    if not dry_run and file_id:
                        progress['failed_ids'].add(file_id)
                        save_progress(progress)
            else:
                stats['failed'] += 1
                log("ERR", f"{indent}❌ Download fail: {name}")
                if not dry_run and file_id:
                    progress['failed_ids'].add(file_id)
                    save_progress(progress)

        log_progress(stats)


# ============================================================
#  MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Sync Google Drive content from output.json to rclone remote'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview only — no downloads, no uploads')
    parser.add_argument('--json', default='output.json',
                        help='Input JSON file (default: output.json)')
    args = parser.parse_args()

    dry_run = args.dry_run

    # ── Banner ──
    print()
    print("═" * 60)
    print("  🔄 SYNC GOOGLE DRIVE")
    mode_label = "DRY-RUN (chỉ xem trước)" if dry_run else "LIVE (thực thi thật)"
    print(f"  Mode: {mode_label}")
    print(f"  Remote: {REMOTE_NAME} (gốc Drive của tôi)")
    print("═" * 60)
    print()

    # ── Setup rclone ──
    if not ensure_rclone_path():
        return

    # ── Load JSON ──
    json_path = os.path.join(WORK_DIR, args.json)
    log("INFO", f"Đọc {args.json}...")

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        log("ERR", f"Không tìm thấy file: {json_path}")
        return
    except json.JSONDecodeError as e:
        log("ERR", f"JSON không hợp lệ: {e}")
        return

    # ── Count files ──
    total = count_files(data)
    log("INFO", f"Tổng cộng: {total} files trong cây thư mục")

    # ── Get Chrome cookies ──
    cookies = get_chrome_cookies(dry_run)
    if not dry_run and not cookies:
        log("WARN", "Không lấy được cookies — download file khác video có thể thất bại")

    # ── Prepare temp dir ──
    os.makedirs(TEMP_DIR, exist_ok=True)
    log("INFO", f"Temp dir: {TEMP_DIR}")

    # ── Load progress ──
    progress = load_progress()
    if not dry_run:
        prev_done = len(progress['done_ids'])
        prev_fail = len(progress['failed_ids'])
        if prev_done > 0 or prev_fail > 0:
            log("INFO", f"📋 Tiếp tục từ lần trước: {prev_done} đã xong, {prev_fail} lỗi")
            log("INFO", f"   Progress file: {PROGRESS_FILE}")
        else:
            log("INFO", f"📋 Bắt đầu mới (progress file: {PROGRESS_FILE})")

    # ── Cookie state (auto-refresh) ──
    cookie_state = {
        'cookies': cookies,
        'last_refresh': time.time(),
    }

    # ── Stats ──
    stats = {
        'total_files': 0,  # sẽ được đếm trong process_node
        'done': 0,
        'skipped': 0,
        'failed': 0,
    }

    # ── Process ──
    start_time = time.time()
    log("INFO", "Bắt đầu xử lý...\n")

    process_node(data, "", cookie_state, stats, progress, dry_run)

    elapsed = time.time() - start_time

    # ── Cleanup temp dir ──
    try:
        if os.path.exists(TEMP_DIR) and not os.listdir(TEMP_DIR):
            os.rmdir(TEMP_DIR)
    except Exception:
        pass

    # ── Summary ──
    print()
    print("═" * 60)
    print("  📊 KẾT QUẢ")
    print("═" * 60)
    log("INFO", f"Thời gian:  {elapsed / 60:.1f} phút")
    log("INFO", f"Tổng files: {stats['total_files']}")
    log("OK",   f"Thành công: {stats['done']}")
    log("SKIP", f"Đã có:      {stats['skipped']}")
    if stats['failed'] > 0:
        log("ERR", f"Thất bại:   {stats['failed']}")
    else:
        log("OK", "Không có lỗi!")
    print("═" * 60)
    print()


if __name__ == '__main__':
    main()

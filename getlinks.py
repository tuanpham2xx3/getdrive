#!/usr/bin/env python3
"""
Google Drive Link Extractor
Sử dụng rclone để lấy toàn bộ danh sách file/folder từ Google Drive
và xuất ra file JSON dạng cây (nested tree).
"""

import json
import subprocess
import sys
import shutil
import os
import time

# ============== CẤU HÌNH ==============
REMOTE_NAME = "getlink:"
FOLDER_ID = os.environ.get("FOLDER_ID", "1yL-lpT9TKX06AX2c0dhZGji78gy8Mv3F")
OUTPUT_FILE = "output.json"
TIMEOUT_SECONDS = 7200  # 2 giờ
# =======================================


def find_rclone():
    """Tìm đường dẫn rclone executable."""
    rclone_path = shutil.which("rclone")
    if rclone_path:
        return rclone_path

    winget_path = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages"
    )
    if os.path.exists(winget_path):
        for root, dirs, files in os.walk(winget_path):
            if "rclone.exe" in files:
                return os.path.join(root, "rclone.exe")

    print("❌ Không tìm thấy rclone! Hãy cài đặt rclone trước.")
    sys.exit(1)


def format_size(size_bytes):
    """Chuyển đổi bytes sang dạng đọc được."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.2f} {units[i]}"


def make_link(item_id, is_dir):
    """Tạo Google Drive link từ ID."""
    if is_dir:
        return f"https://drive.google.com/drive/folders/{item_id}"
    else:
        return f"https://drive.google.com/file/d/{item_id}/view"


def fetch_root_folder_name(rclone_path):
    """Lấy tên folder gốc từ rclone backend get (query by ID)."""
    # Method 1: rclone backend get — query folder metadata by ID directly
    cmd = [
        rclone_path, "backend", "get",
        REMOTE_NAME, FOLDER_ID,
        "-o", "fields=name",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            name = data.get("name", "")
            if name:
                return name
    except Exception as e:
        print(f"⚠️  backend get thất bại: {e}")

    # Method 2: fallback to lsjson --stat
    cmd2 = [
        rclone_path, "lsjson",
        REMOTE_NAME,
        "--drive-root-folder-id", FOLDER_ID,
        "--stat",
    ]
    try:
        result = subprocess.run(
            cmd2, capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if isinstance(data, list) and len(data) > 0:
                name = data[0].get("Name", "")
            elif isinstance(data, dict):
                name = data.get("Name", "")
            if name:
                return name
    except Exception as e:
        print(f"⚠️  lsjson --stat thất bại: {e}")

    return "Root"


def fetch_drive_data(rclone_path):
    """Gọi rclone lsjson để lấy danh sách file/folder (streaming)."""
    cmd = [
        rclone_path, "lsjson",
        REMOTE_NAME,
        "--drive-root-folder-id", FOLDER_ID,
        "--recursive",
        "--no-modtime",
        "--fast-list",
    ]

    print(f"🔍 Đang quét Google Drive folder: {FOLDER_ID}")
    print(f"   Remote: {REMOTE_NAME}")
    print(f"   Rclone: {rclone_path}")
    print(f"   Timeout: {TIMEOUT_SECONDS}s ({TIMEOUT_SECONDS//60} phút)")
    print("   ⏳ Đang tải dữ liệu...")
    print()

    start_time = time.time()

    try:
        # Dùng Popen để stream output và hiển thị progress
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,  # binary mode
        )

        # Đọc stdout theo chunks
        stdout_data = b""
        while True:
            # Kiểm tra timeout
            elapsed = time.time() - start_time
            if elapsed > TIMEOUT_SECONDS:
                process.kill()
                print(f"\n❌ Timeout sau {elapsed:.0f}s!")
                sys.exit(1)

            # Đọc data
            chunk = process.stdout.read(65536)  # 64KB chunks
            if not chunk:
                break
            stdout_data += chunk

            # Hiển thị progress
            elapsed = time.time() - start_time
            size_mb = len(stdout_data) / (1024 * 1024)
            print(f"\r   📥 Đã nhận: {size_mb:.1f} MB | Thời gian: {elapsed:.0f}s", end="", flush=True)

        process.wait()
        elapsed = time.time() - start_time

        print(f"\n   ⏱️  Tổng thời gian quét: {elapsed:.1f}s")

        if process.returncode != 0:
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            print(f"❌ Rclone lỗi (exit code {process.returncode}):\n{stderr}")
            sys.exit(1)

        # Parse JSON
        json_str = stdout_data.decode("utf-8")
        items = json.loads(json_str)
        print(f"✅ Tìm thấy {len(items)} items")
        return items

    except json.JSONDecodeError as e:
        print(f"❌ Lỗi parse JSON: {e}")
        # Lưu raw output để debug
        with open("raw_output.txt", "wb") as f:
            f.write(stdout_data)
        print("   Raw output đã lưu vào raw_output.txt")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Lỗi: {e}")
        sys.exit(1)


def build_tree(items, root_name="Root"):
    """Chuyển flat list thành nested tree JSON."""
    root = {
        "name": root_name,
        "type": "folder",
        "id": FOLDER_ID,
        "link": make_link(FOLDER_ID, True),
        "children": []
    }

    path_index = {"": root}
    items.sort(key=lambda x: x["Path"])
    stats = {"folders": 0, "files": 0, "total_size": 0}

    for item in items:
        path = item["Path"]
        is_dir = item.get("IsDir", False)
        item_id = item.get("ID", "")
        name = item.get("Name", path.split("/")[-1])
        size = item.get("Size", 0)
        mime_type = item.get("MimeType", "")

        node = {
            "name": name,
            "type": "folder" if is_dir else "file",
            "id": item_id,
            "link": make_link(item_id, is_dir),
        }

        if is_dir:
            node["children"] = []
            stats["folders"] += 1
        else:
            node["size"] = size
            node["sizeFormatted"] = format_size(size)
            node["mimeType"] = mime_type
            stats["files"] += 1
            stats["total_size"] += size

        # Tìm parent path
        if "/" in path:
            parent_path = path.rsplit("/", 1)[0]
        else:
            parent_path = ""

        # Thêm node vào parent
        parent_node = path_index.get(parent_path)
        if parent_node is None:
            parts = parent_path.split("/")
            current_path = ""
            for part in parts:
                current_path = f"{current_path}/{part}" if current_path else part
                if current_path not in path_index:
                    missing_parent = {
                        "name": part,
                        "type": "folder",
                        "id": "",
                        "link": "",
                        "children": []
                    }
                    gp_path = current_path.rsplit("/", 1)[0] if "/" in current_path else ""
                    if gp_path in path_index:
                        path_index[gp_path]["children"].append(missing_parent)
                    path_index[current_path] = missing_parent
            parent_node = path_index.get(parent_path, root)

        parent_node["children"].append(node)

        if is_dir:
            path_index[path] = node

    return root, stats


def main():
    print("=" * 50)
    print("📂 Google Drive Link Extractor")
    print("=" * 50)
    print()

    rclone_path = find_rclone()
    items = fetch_drive_data(rclone_path)

    if not items:
        print("⚠️  Folder trống hoặc không có quyền truy cập")
        sys.exit(0)

    print("🔍 Đang lấy tên folder gốc...")
    root_name = fetch_root_folder_name(rclone_path)
    print(f"   📂 Tên folder gốc: {root_name}")

    print("🌳 Đang xây dựng cây thư mục...")
    tree, stats = build_tree(items, root_name)

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 50)
    print("📊 KẾT QUẢ:")
    print(f"   📁 Folders: {stats['folders']}")
    print(f"   📄 Files:   {stats['files']}")
    print(f"   💾 Tổng dung lượng: {format_size(stats['total_size'])}")
    print(f"   📝 Output:  {output_path}")
    print("=" * 50)
    print("✅ Hoàn tất!")


if __name__ == "__main__":
    main()

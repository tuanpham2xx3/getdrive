import json

TARGET_NAMES = [
    "khoahocgiahoi.com website bán khóa học uy tín, chất lượng, giá rẻ.txt",
    "Danh Sách Khóa Học",
    "NHÓM ZALO  - QUÉT MÃ QR VÀO NHÓM.png",
]
INPUT_FILE = "output.json"
OUTPUT_FILE = "output.json"


def remove_target_children(node):
    """Recursively remove children whose name contains any target string."""
    if "children" not in node:
        return 0

    removed = 0
    original_count = len(node["children"])

    # Filter out children matching the target name
    node["children"] = [
        child for child in node["children"]
        if not any(t in child.get("name", "") for t in TARGET_NAMES)
    ]
    removed += original_count - len(node["children"])

    # Recurse into remaining children
    for child in node["children"]:
        removed += remove_target_children(child)

    return removed


def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    removed_count = remove_target_children(data)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ Đã xóa {removed_count} mục chứa các từ khóa target")


if __name__ == "__main__":
    main()

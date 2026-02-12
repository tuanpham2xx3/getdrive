# Hướng dẫn sử dụng Rclone

## Vấn đề: rclone không có trong PATH

Rclone được cài qua WinGet nhưng không tự thêm vào PATH. Mỗi lần dùng cần thêm PATH trước.

## Cách 1: Thêm PATH tạm thời (mỗi phiên PowerShell)

```powershell
$env:PATH += ";C:\Users\phamt\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.73.0-windows-amd64"
```

Sau đó dùng bình thường:
```powershell
rclone listremotes
rclone lsd gdrive:
rclone copy gdrive:"Thu muc nguon" "C:\local\path" --progress
```

## Cách 2: Thêm PATH vĩnh viễn (chỉ cần làm 1 lần)

Chạy lệnh này trong PowerShell **với quyền Admin**:

```powershell
$rclonePath = "C:\Users\phamt\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.73.0-windows-amd64"
[Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";$rclonePath", "User")
```

> **Lưu ý:** Sau khi chạy, cần **mở lại terminal mới** để có hiệu lực.

## Các lệnh rclone thường dùng

| Lệnh | Mô tả |
|---|---|
| `rclone listremotes` | Liệt kê remote đã cấu hình |
| `rclone about gdrive:` | Xem dung lượng tài khoản |
| `rclone lsd gdrive:` | Liệt kê thư mục gốc |
| `rclone ls gdrive:"Folder"` | Liệt kê file trong thư mục |
| `rclone copy source dest --progress` | Copy file có hiển thị tiến trình |
| `rclone config delete gdrive` | Xóa remote |
| `rclone config create gdrive drive` | Tạo remote Google Drive mới |

## Remote hiện tại

- **Tên remote:** `gdrive:`
- **Loại:** Google Drive
- **Dung lượng:** 100 TiB

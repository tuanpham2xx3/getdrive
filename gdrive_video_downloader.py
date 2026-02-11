"""
Google Drive Video Downloader using Playwright
- Mở trình duyệt ẩn (headless mode)
- Load cookies từ file Netscape format
- Bắt URL video từ Network tab
- Tải video về máy

Yêu cầu:
    pip install playwright requests
    playwright install chromium
"""

import re
import sys
import time
import requests
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, BrowserContext


def parse_netscape_cookies(cookie_file: str) -> list[dict]:
    """
    Parse cookies từ file Netscape format (dùng bởi yt-dlp, curl, etc.)
    
    Format: domain  include_subdomains  path  secure  expiry  name  value
    """
    cookies = []
    
    with open(cookie_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            
            # Bỏ qua comment và dòng trống
            if not line or line.startswith('#'):
                continue
            
            parts = line.split('\t')
            if len(parts) >= 7:
                domain = parts[0]
                # include_subdomains = parts[1].upper() == 'TRUE'
                path = parts[2]
                secure = parts[3].upper() == 'TRUE'
                expiry = int(parts[4]) if parts[4].isdigit() else 0
                name = parts[5]
                value = parts[6]
                
                cookie = {
                    'name': name,
                    'value': value,
                    'domain': domain,
                    'path': path,
                    'secure': secure,
                }
                
                # Chỉ thêm expires nếu > 0
                if expiry > 0:
                    cookie['expires'] = expiry
                
                cookies.append(cookie)
    
    return cookies


def extract_file_id(url: str) -> str | None:
    """Trích xuất file ID từ Google Drive URL"""
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',  # Standard share link
        r'id=([a-zA-Z0-9_-]+)',        # Direct download link
        r'/d/([a-zA-Z0-9_-]+)',        # Short link
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None


def capture_video_url(
    google_drive_url: str,
    cookie_file: str = 'drive.google.com_cookies.txt',
    headless: bool = True,
    wait_time: int = 15000
) -> list[dict]:
    """
    Mở Google Drive video và bắt URL videoplayback từ network
    
    Args:
        google_drive_url: URL của video trên Google Drive
        cookie_file: Đường dẫn đến file cookies (Netscape format)
        headless: True để chạy ẩn trình duyệt
        wait_time: Thời gian đợi video load (ms)
    
    Returns:
        List các video URL đã bắt được
    """
    video_urls = []
    
    # Parse cookies từ file
    cookie_path = Path(cookie_file)
    if not cookie_path.exists():
        print(f"[ERROR] Không tìm thấy file cookies: {cookie_file}")
        return []
    
    cookies = parse_netscape_cookies(cookie_file)
    print(f"[INFO] Đã load {len(cookies)} cookies từ {cookie_file}")
    
    # Filter cookies cho Google Drive
    google_cookies = [c for c in cookies if 'google.com' in c['domain'] or 'drive.google.com' in c['domain']]
    print(f"[INFO] {len(google_cookies)} cookies liên quan đến Google")
    
    with sync_playwright() as p:
        # Khởi tạo browser
        print(f"[INFO] Khởi động browser (headless={headless})...")
        browser = p.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )
        
        # Tạo context với cookies
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        # Thêm cookies vào context
        try:
            context.add_cookies(google_cookies)
            print("[INFO] Đã thêm cookies vào browser")
        except Exception as e:
            print(f"[WARNING] Lỗi khi thêm cookies: {e}")
        
        page = context.new_page()
        
        # Handler để bắt network requests
        def handle_request(request):
            url = request.url
            # Lọc URL videoplayback với đầy đủ params (như mẫu user cung cấp)
            if 'videoplayback' in url and 'drive.google.com' in url:
                # Kiểm tra có đủ params quan trọng
                if 'expire=' in url and 'itag=' in url and 'source=' in url:
                    video_info = {
                        'url': url,
                        'timestamp': datetime.now().isoformat(),
                        'method': request.method,
                    }
                    
                    # Xác định loại dựa trên itag hoặc mime
                    if 'mime=video' in url:
                        video_info['type'] = 'video'
                    elif 'mime=audio' in url:
                        video_info['type'] = 'audio'
                    else:
                        video_info['type'] = 'unknown'
                    
                    # Trích xuất itag
                    import re
                    itag_match = re.search(r'itag=(\d+)', url)
                    if itag_match:
                        video_info['itag'] = itag_match.group(1)
                    
                    # Tránh duplicate
                    if not any(v['url'] == url for v in video_urls):
                        video_urls.append(video_info)
                        itag_str = f" (itag={video_info.get('itag', '?')})" if 'itag' in video_info else ""
                        print(f"\n[CAPTURED] {video_info['type'].upper()}{itag_str}:")
                        print(f"URL: {url}")
        
        page.on('request', handle_request)
        
        # Mở trang Google Drive
        print(f"[INFO] Đang mở: {google_drive_url}")
        try:
            page.goto(google_drive_url, wait_until='networkidle', timeout=30000)
        except Exception as e:
            print(f"[WARNING] Timeout khi load trang: {e}")
        
        # Chờ trang load
        print("[INFO] Đang đợi video player load...")
        page.wait_for_timeout(5000)
        
        # Thử click vào video để play - nhiều phương pháp
        print("[INFO] Đang thử click play...")
        clicked = False
        
        # Phương pháp 1: Click vào giữa màn hình (vị trí nút play)
        try:
            page.mouse.click(640, 390)  # Click vào giữa video
            print("[INFO] Đã click vào giữa video")
            clicked = True
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[DEBUG] Click giữa thất bại: {e}")
        
        # Phương pháp 2: Dùng keyboard
        try:
            page.keyboard.press('Space')
            print("[INFO] Đã nhấn Space để play")
            page.wait_for_timeout(1000)
        except:
            pass
        
        # Phương pháp 3: Tìm và click các selector
        play_selectors = [
            '[aria-label*="Play"]',
            '[aria-label*="play"]', 
            '[data-tooltip*="Play"]',
            '.ytp-play-button',
            'button[aria-label*="Play"]',
            '.ndfHFb-c4YZDc-Wrber',
            'video',
            '.ndfHFb-c4YZDc',  # Video container
        ]
        
        for selector in play_selectors:
            try:
                element = page.query_selector(selector)
                if element:
                    element.click()
                    print(f"[INFO] Đã click vào selector: {selector}")
                    clicked = True
                    page.wait_for_timeout(1000)
                    break
            except Exception as e:
                continue
        
        if not clicked:
            print("[WARNING] Không thể click play tự động")
        
        # Đợi network requests
        print(f"[INFO] Đang đợi {wait_time/1000}s để bắt video URLs...")
        page.wait_for_timeout(wait_time)
        
        # Thử scroll để trigger load thêm
        try:
            page.mouse.move(960, 540)
            page.mouse.wheel(0, 100)
        except:
            pass
        
        page.wait_for_timeout(3000)
        
        # Nếu visible mode và chưa bắt được URL, đợi user click play
        if not headless and len(video_urls) == 0:
            print("\n" + "="*50)
            print("[INTERACTIVE] Không tự động bắt được video URL!")
            print("[INTERACTIVE] Hãy CLICK VÀO NÚT PLAY trong browser")
            print("[INTERACTIVE] Sau đó nhấn ENTER ở đây để tiếp tục...")
            print("="*50)
            input()
            print("[INFO] Đang đợi thêm 10s để bắt URL...")
            page.wait_for_timeout(10000)
        
        # Đóng browser
        browser.close()
    
    return video_urls


def download_video(
    video_url: str,
    output_path: str = 'downloaded_video.mp4',
    cookie_file: str = 'drive.google.com_cookies.txt'
) -> bool:
    """
    Tải video từ URL videoplayback
    
    Args:
        video_url: URL của video (từ capture_video_url)
        output_path: Đường dẫn file output
        cookie_file: File cookies để authenticate
    
    Returns:
        True nếu tải thành công
    """
    print(f"[INFO] Đang tải video về: {output_path}")
    
    # Load cookies cho requests
    cookies = parse_netscape_cookies(cookie_file)
    cookie_dict = {c['name']: c['value'] for c in cookies if 'google.com' in c['domain']}
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://drive.google.com/',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        response = requests.get(
            video_url,
            headers=headers,
            cookies=cookie_dict,
            stream=True,
            timeout=60
        )
        
        if response.status_code == 200:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Progress indicator
                        if total_size > 0:
                            percent = (downloaded / total_size) * 100
                            print(f"\r[DOWNLOAD] {percent:.1f}% ({downloaded / 1024 / 1024:.1f} MB)", end='')
            
            print(f"\n[SUCCESS] Đã tải xong: {output_path}")
            return True
        else:
            print(f"[ERROR] HTTP {response.status_code}: {response.text[:200]}")
            return False
            
    except Exception as e:
        print(f"[ERROR] Lỗi khi tải: {e}")
        return False


def main():
    """Main function"""
    # Ví dụ sử dụng
    print("=" * 60)
    print("Google Drive Video Downloader")
    print("=" * 60)
    
    # URL video Google Drive (thay bằng URL của bạn)
    if len(sys.argv) > 1:
        gdrive_url = sys.argv[1]
    else:
        gdrive_url = input("Nhập Google Drive URL: ").strip()
    
    if not gdrive_url:
        print("[ERROR] Vui lòng nhập URL!")
        return
    
    # File cookies
    cookie_file = 'drive.google.com_cookies.txt'
    
    # Chế độ headless (True = ẩn, False = hiện)
    headless = True
    if '--visible' in sys.argv or '-v' in sys.argv:
        headless = False
        print("[INFO] Chế độ hiện trình duyệt")
    
    # Chế độ chỉ lấy URL
    url_only = '--url-only' in sys.argv or '-u' in sys.argv
    if url_only:
        print("[INFO] Chế độ chỉ lấy URL (không tải video)")
    
    # Bắt video URLs
    print("\n[STEP 1] Đang bắt video URLs...")
    video_urls = capture_video_url(
        google_drive_url=gdrive_url,
        cookie_file=cookie_file,
        headless=headless,
        wait_time=15000
    )
    
    if not video_urls:
        print("[ERROR] Không bắt được video URL nào!")
        print("[TIP] Thử chạy với --visible để xem trình duyệt")
        return
    
    print(f"\n[INFO] Đã bắt được {len(video_urls)} URL(s)")
    
    # Hiển thị các URL đã bắt (FULL URL)
    print("\n" + "="*60)
    print("CÁC URL ĐÃ BẮT ĐƯỢC:")
    print("="*60)
    for i, v in enumerate(video_urls):
        itag = v.get('itag', '?')
        print(f"\n--- URL {i+1} ({v['type']}, itag={itag}) ---")
        print(v['url'])
    print("="*60)
    
    # Nếu chỉ lấy URL thì dừng ở đây
    if url_only:
        print("\n[DONE] Đã hiển thị tất cả URLs. Không tải video (--url-only)")
        return
    
    # Tải video (ưu tiên video, không phải audio)
    video_only = [v for v in video_urls if v['type'] == 'video']
    url_to_download = video_only[0]['url'] if video_only else video_urls[0]['url']
    
    # Tạo tên output từ file ID
    file_id = extract_file_id(gdrive_url)
    output_name = f"gdrive_{file_id or 'video'}_{int(time.time())}.mp4"
    
    print(f"\n[STEP 2] Đang tải video...")
    success = download_video(
        video_url=url_to_download,
        output_path=output_name,
        cookie_file=cookie_file
    )
    
    if success:
        print(f"\n[DONE] Video đã được tải về: {output_name}")
    else:
        print("\n[FAILED] Không thể tải video.")
        print("[TIP] URL có thể đã hết hạn, thử chạy lại để bắt URL mới")


if __name__ == '__main__':
    main()

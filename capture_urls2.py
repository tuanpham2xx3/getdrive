"""
Capture Video/Audio URLs from Google Drive
Flow: 
1. Load cookies
2. Mở link
3. Click play video
4. Capture URLs có &mime=video hoặc &mime=audio
5. Lấy 1 URL video + 1 URL audio
6. Mở URL video
7. Tải video
8. Mở URL audio
9. Tải audio
10. Gộp video + audio bằng FFmpeg
11. Output file path để web tải xuống
"""

from playwright.sync_api import sync_playwright
import sys
import time
import subprocess
import os

# Config
cookies_file = "drive.google.com_cookies.txt"
captured_urls = []

def clean_url(url):
    """Cắt URL từ &range= đến hết"""
    if "&range=" in url:
        return url.split("&range=")[0]
    return url

def parse_netscape_cookies(cookie_file):
    cookies = []
    with open(cookie_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                cookie = {
                    'name': parts[5],
                    'value': parts[6],
                    'domain': parts[0],
                    'path': parts[2],
                    'secure': parts[3].upper() == 'TRUE',
                }
                if parts[4] != '0':
                    try:
                        cookie['expires'] = int(parts[4])
                    except:
                        pass
                cookies.append(cookie)
    return cookies

def check_url_size(url, cookies_dict, headers):
    """Kiểm tra size của URL, trả về 0 nếu là redirect page"""
    import requests
    try:
        response = requests.head(url, cookies=cookies_dict, headers=headers, timeout=10, allow_redirects=True)
        content_length = int(response.headers.get('content-length', 0))
        return content_length
    except:
        return -1

def download_part(url, start, end, part_file, cookies_dict, headers, part_num, progress_dict):
    """Tải 1 phần của file"""
    import requests
    request_headers = headers.copy()
    request_headers['Range'] = f'bytes={start}-{end}'
    
    try:
        response = requests.get(url, cookies=cookies_dict, headers=request_headers, stream=True, timeout=120)
        if response.status_code in [200, 206]:
            downloaded = 0
            total = end - start + 1
            with open(part_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        progress_dict[part_num] = downloaded
            return True
    except Exception as e:
        print(f"[ERROR] Part {part_num}: {str(e)[:50]}")
    return False

def download_file_multithread(url, filename, cookies_dict, headers, num_threads=64):
    """Tải file bằng multi-thread. Trả về 'redirect' nếu size < 10KB"""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    
    # Xóa file lỗi nhỏ từ lần chạy trước
    if os.path.exists(filename):
        file_size = os.path.getsize(filename)
        if file_size < 10000:
            os.remove(filename)
            print(f"[INFO] Deleted corrupted file: {filename}")
    
    # Kiểm tra total size
    try:
        response = requests.head(url, cookies=cookies_dict, headers=headers, timeout=10)
        total_size = int(response.headers.get('content-length', 0))
    except:
        total_size = 0
    
    if total_size < 10000:
        print(f"[WARNING] Size = {total_size} bytes (< 10KB), đây là redirect page...")
        return 'redirect'
    
    print(f"[INFO] Total size: {total_size // 1024 // 1024}MB - Using {num_threads} threads")
    
    # Chia file thành các phần
    part_size = total_size // num_threads
    parts = []
    for i in range(num_threads):
        start = i * part_size
        end = (i + 1) * part_size - 1 if i < num_threads - 1 else total_size - 1
        part_file = f"{filename}.part{i}"
        parts.append((start, end, part_file, i))
    
    # Progress tracking
    progress_dict = {i: 0 for i in range(num_threads)}
    
    def print_progress():
        while not download_done.is_set():
            total_downloaded = sum(progress_dict.values())
            percent = (total_downloaded / total_size) * 100 if total_size > 0 else 0
            print(f"\r  Đang tải: {percent:.1f}% ({total_downloaded//1024//1024}MB/{total_size//1024//1024}MB)", end="", flush=True)
            time.sleep(0.5)
    
    download_done = threading.Event()
    progress_thread = threading.Thread(target=print_progress)
    progress_thread.start()
    
    # Download các phần song song
    success = True
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(download_part, url, start, end, part_file, cookies_dict, headers, part_num, progress_dict): part_num
            for start, end, part_file, part_num in parts
        }
        for future in as_completed(futures):
            if not future.result():
                success = False
    
    download_done.set()
    progress_thread.join()
    
    if not success:
        print(f"\n[ERROR] Multi-thread download failed!")
        # Cleanup
        for _, _, part_file, _ in parts:
            if os.path.exists(part_file):
                os.remove(part_file)
        return False
    
    # Gộp các phần lại
    print(f"\n[INFO] Merging {num_threads} parts...")
    with open(filename, 'wb') as outfile:
        for _, _, part_file, _ in parts:
            if os.path.exists(part_file):
                with open(part_file, 'rb') as infile:
                    outfile.write(infile.read())
    
    # Xóa part files riêng (tránh PermissionError trên Windows)
    time.sleep(0.5)  # Đợi Windows release file handles
    for _, _, part_file, _ in parts:
        for retry in range(3):
            try:
                if os.path.exists(part_file):
                    os.remove(part_file)
                break
            except PermissionError:
                time.sleep(0.3)
                continue
    
    # Verify final size
    final_size = os.path.getsize(filename) if os.path.exists(filename) else 0
    if final_size < total_size * 0.95:  # Cho phép sai số 5%
        print(f"[WARNING] File size mismatch: {final_size} vs {total_size}")
        return False
    
    print(f"✅ Đã tải: {filename} ({final_size//1024//1024}MB)")
    return True

def download_file(url, filename, cookies_dict, headers, max_retries=3):
    """Tải file từ URL với cookies và retry logic. Trả về 'redirect' nếu size = 0"""
    import requests
    
    # Xóa file lỗi nhỏ (< 10KB) từ lần chạy trước
    if os.path.exists(filename):
        file_size = os.path.getsize(filename)
        if file_size < 10000:  # < 10KB chắc chắn là lỗi
            os.remove(filename)
            print(f"[INFO] Deleted corrupted file: {filename} ({file_size} bytes)")
    
    for attempt in range(max_retries):
        try:
            # Check if file partially downloaded
            downloaded = 0
            mode = 'wb'
            request_headers = headers.copy()
            
            if os.path.exists(filename):
                downloaded = os.path.getsize(filename)
                if downloaded > 0:
                    request_headers['Range'] = f'bytes={downloaded}-'
                    mode = 'ab'
                    print(f"[INFO] Resuming from {downloaded//1024//1024}MB...")
            
            print(f"[INFO] Downloading {filename}... (attempt {attempt + 1}/{max_retries})")
            response = requests.get(url, cookies=cookies_dict, headers=request_headers, stream=True, timeout=60)
            
            # Handle range response
            if response.status_code == 206:
                content_range = response.headers.get('content-range', '')
                if '/' in content_range:
                    total_size = int(content_range.split('/')[-1])
                else:
                    total_size = downloaded + int(response.headers.get('content-length', 0))
            else:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                mode = 'wb'
            
            # Nếu size = 0 hoặc quá nhỏ (< 10KB), đây là redirect page
            if total_size < 10000:
                print(f"[WARNING] Size = {total_size} bytes (< 10KB), đây là redirect page...")
                return 'redirect'
            
            if response.status_code in [200, 206] and total_size > 0:
                with open(filename, mode) as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            percent = (downloaded / total_size) * 100
                            print(f"\r  Đang tải: {percent:.1f}% ({downloaded//1024//1024}MB/{total_size//1024//1024}MB)", end="", flush=True)
                
                # Kiểm tra lại file size sau khi tải
                actual_size = os.path.getsize(filename) if os.path.exists(filename) else 0
                if actual_size < 10000:
                    print(f"\n[WARNING] Downloaded file too small ({actual_size} bytes), likely redirect...")
                    os.remove(filename)
                    return 'redirect'
                
                print(f"\n✅ Đã tải: {filename} ({total_size//1024//1024}MB)")
                return True
            else:
                print(f"[ERROR] Response status: {response.status_code}, size: {total_size}")
                
        except Exception as e:
            print(f"\n[WARNING] Download error (attempt {attempt + 1}): {str(e)[:100]}")
            if attempt < max_retries - 1:
                print(f"[INFO] Retrying in 3 seconds...")
                import time
                time.sleep(3)
            else:
                print(f"[ERROR] Failed after {max_retries} attempts")
                return False
    
    return False

def merge_video_audio(video_file, audio_file, output_file):
    """Gộp video và audio bằng FFmpeg"""
    print(f"\n[STEP 10] Đang gộp video + audio...")
    
    # Kiểm tra FFmpeg - thử nhiều đường dẫn
    ffmpeg_paths = [
        'ffmpeg',
        r'C:\Users\phamt\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe',
    ]
    
    ffmpeg_cmd = None
    for path in ffmpeg_paths:
        try:
            subprocess.run([path, '-version'], capture_output=True, check=True)
            ffmpeg_cmd = path
            print(f"[INFO] Found FFmpeg: {path[:50]}...")
            break
        except:
            continue
    
    if not ffmpeg_cmd:
        print("[ERROR] FFmpeg chưa được cài đặt!")
        print("[INFO] Cài FFmpeg: https://ffmpeg.org/download.html")
        print(f"[INFO] Hoặc gộp thủ công: ffmpeg -i {video_file} -i {audio_file} -c copy {output_file}")
        return False
    
    # Gộp video + audio
    cmd = [
        ffmpeg_cmd, '-y',
        '-i', video_file,
        '-i', audio_file,
        '-c', 'copy',
        output_file
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ Đã gộp thành công: {output_file}")
            # Xóa file tạm
            os.remove(video_file)
            os.remove(audio_file)
            print(f"[INFO] Đã xóa file tạm: {video_file}, {audio_file}")
            return True
        else:
            print(f"[ERROR] FFmpeg error: {result.stderr}")
            return False
    except Exception as e:
        print(f"[ERROR] Merge error: {e}")
        return False

def main():
    global captured_urls
    
    if len(sys.argv) > 1:
        gdrive_url = sys.argv[1]
    else:
        gdrive_url = input("Nhập Google Drive URL: ").strip()
    
    if not gdrive_url:
        print("[ERROR] Vui lòng nhập URL!")
        return

    print(f"\n[INFO] URL: {gdrive_url}")
    
    with sync_playwright() as p:
        # ===== STEP 0: Load cookies trước =====
        print("\n[STEP 0] Loading cookies...")
        cookies = parse_netscape_cookies(cookies_file)
        print(f"Loaded {len(cookies)} cookies")
        
        # Launch browser với DevTools mở sẵn
        browser = p.chromium.launch(
            headless=False,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        
        context = browser.new_context(
            viewport={'width': 3840, 'height': 2160},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            device_scale_factor=2
        )
        
        # Add cookies vào context
        try:
            context.add_cookies(cookies)
            print("Cookies added successfully")
        except Exception as e:
            print(f"[WARNING] Cookie error: {e}")
        
        page = context.new_page()
        
        # Bắt network requests
        def handle_response(response):
            url = response.url
            if "&mime=video" in url or "&mime=audio" in url:
                mime_type = "video" if "&mime=video" in url else "audio"
                if not any(u['url'] == url for u in captured_urls):
                    captured_urls.append({"type": mime_type, "url": url})
                    print(f"[CAPTURED] {mime_type}!", flush=True)
        
        page.on("response", handle_response)
        print("[STEP 1] Network listener ready")
        
        # ===== STEP 2: Mở link =====
        print(f"\n[STEP 2] Đang mở link...")
        try:
            page.goto(gdrive_url, timeout=60000)
        except Exception as e:
            print(f"[WARNING] Page load: {e}")
        
        page.wait_for_timeout(3000)
        
        # ===== STEP 3: Click play video =====
        print("\n[STEP 3] Click play video...")
        try:
            page.mouse.click(1920, 1080)
        except Exception as e:
            print(f"[DEBUG] Click error: {e}")
        
        page.wait_for_timeout(2000)
        
        # ===== STEP 3.5: Thử chọn chất lượng cao nhất trong player =====
        print("\n[STEP 3.5] Thử chọn chất lượng cao nhất trong player...")
        try:
            # Di chuột vào video để hiện controls
            page.mouse.move(1920, 1080)
            page.wait_for_timeout(1000)
            
            # Tìm nút settings (gear icon) trong Google Drive player
            settings_btn = page.query_selector('[aria-label="Settings"], [aria-label="Cài đặt"], [data-tooltip="Settings"], [data-tooltip="Cài đặt"], .ytp-settings-button')
            if settings_btn:
                settings_btn.click()
                print("  Đã click Settings")
                page.wait_for_timeout(1000)
                
                # Tìm menu Quality
                quality_item = page.query_selector(':text("Quality"), :text("Chất lượng")')
                if quality_item:
                    quality_item.click()
                    print("  Đã click Quality menu")
                    page.wait_for_timeout(1000)
                    
                    # Tìm option chất lượng cao nhất (1080p, 1440p, 2160p, etc.)
                    for quality in ['2160p', '1440p', '1080p', '720p']:
                        quality_option = page.query_selector(f':text("{quality}")')
                        if quality_option:
                            quality_option.click()
                            print(f"  ✅ Đã chọn chất lượng: {quality}")
                            break
                else:
                    print("  Không tìm thấy menu Quality")
                    # Click lại để đóng menu
                    settings_btn.click()
            else:
                print("  Không tìm thấy nút Settings")
        except Exception as e:
            print(f"  [DEBUG] Quality selection error: {e}")
        
        page.wait_for_timeout(3000)
        
        # ===== STEP 4: Đợi capture URLs =====
        print("\n[STEP 4] Video đang chạy, đang capture URLs...")
        
        for i in range(30):
            page.wait_for_timeout(1000)
            video_count = sum(1 for u in captured_urls if u['type'] == 'video')
            audio_count = sum(1 for u in captured_urls if u['type'] == 'audio')
            print(f"  Đã đợi {i+1}s - Captured: {video_count} video, {audio_count} audio URLs", flush=True)
            # Đợi ít nhất có 1 video + 1 audio, và đợi ít nhất 10s để bắt thêm URLs chất lượng cao
            if video_count >= 1 and audio_count >= 1 and i >= 10:
                page.wait_for_timeout(5000)
                break
        
        video_count = sum(1 for u in captured_urls if u['type'] == 'video')
        audio_count = sum(1 for u in captured_urls if u['type'] == 'audio')
        print(f"\n[INFO] Tổng cộng: {len(captured_urls)} URLs ({video_count} video, {audio_count} audio)")

        # ===== STEP 5: Chọn URL video và audio chất lượng cao nhất =====
        print("\n[STEP 5] Kiểm tra chất lượng từng URL...")
        
        # Lấy cookies từ browser để check size
        browser_cookies = context.cookies()
        cookies_dict = {c['name']: c['value'] for c in browser_cookies}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://drive.google.com/'
        }
        
        # Thu thập tất cả video URLs (đã clean)
        all_video_urls = list(set([clean_url(u['url']) for u in captured_urls if u['type'] == 'video']))
        all_audio_urls = list(set([clean_url(u['url']) for u in captured_urls if u['type'] == 'audio']))
        
        print(f"  Tìm thấy {len(all_video_urls)} video URL(s) khác nhau")
        print(f"  Tìm thấy {len(all_audio_urls)} audio URL(s) khác nhau")
        
        # Kiểm tra size từng video URL để chọn chất lượng cao nhất
        video_url = None
        best_video_size = 0
        
        if all_video_urls:
            print(f"\n  📊 Kiểm tra kích thước từng video URL:")
            for i, vurl in enumerate(all_video_urls):
                size = check_url_size(vurl, cookies_dict, headers)
                size_mb = size / 1024 / 1024 if size > 0 else 0
                # Parse itag nếu có
                itag = ''
                if 'itag=' in vurl:
                    try:
                        itag = vurl.split('itag=')[1].split('&')[0]
                        itag = f' (itag={itag})'
                    except:
                        pass
                print(f"    Video #{i+1}{itag}: {size_mb:.1f}MB ({size} bytes)")
                if size > best_video_size:
                    best_video_size = size
                    video_url = vurl
            
            if video_url:
                print(f"  ✅ Chọn video chất lượng cao nhất: {best_video_size / 1024 / 1024:.1f}MB")
        
        # Kiểm tra size từng audio URL để chọn chất lượng cao nhất
        audio_url = None
        best_audio_size = 0
        
        if all_audio_urls:
            print(f"\n  📊 Kiểm tra kích thước từng audio URL:")
            for i, aurl in enumerate(all_audio_urls):
                size = check_url_size(aurl, cookies_dict, headers)
                size_mb = size / 1024 / 1024 if size > 0 else 0
                itag = ''
                if 'itag=' in aurl:
                    try:
                        itag = aurl.split('itag=')[1].split('&')[0]
                        itag = f' (itag={itag})'
                    except:
                        pass
                print(f"    Audio #{i+1}{itag}: {size_mb:.1f}MB ({size} bytes)")
                if size > best_audio_size:
                    best_audio_size = size
                    audio_url = aurl
            
            if audio_url:
                print(f"  ✅ Chọn audio chất lượng cao nhất: {best_audio_size / 1024 / 1024:.1f}MB")
        
        if video_url and audio_url:
            print(f"\n✅ Đã chọn video ({best_video_size//1024//1024}MB) + audio ({best_audio_size//1024//1024}MB) chất lượng cao nhất")
        else:
            print(f"⚠️ Chỉ lấy được: video={'có' if video_url else 'không'}, audio={'có' if audio_url else 'không'}")
        
        # cookies_dict và headers đã được lấy ở STEP 5
        
        # Lấy file ID
        import re
        driveid_match = re.search(r'driveid=([^&]+)', video_url or audio_url or "")
        file_id = driveid_match.group(1)[:8] if driveid_match else "download"
        
        video_file = f"video_{file_id}.mp4"
        audio_file = f"audio_{file_id}.m4a"
        output_merged = f"merged_{file_id}.mp4"
        
        # ===== STEP 6 & 7: Mở và tải video =====
        if video_url:
            current_url = video_url
            max_redirects = 5
            for redirect_count in range(max_redirects):
                print(f"\n[STEP 6] Mở URL video... (attempt {redirect_count + 1}/{max_redirects})")
                
                # Clear captured URLs để capture URL mới
                redirect_urls = []
                def capture_redirect(response):
                    url = response.url
                    if "&mime=video" in url and url != current_url:
                        if url not in redirect_urls:
                            redirect_urls.append(url)
                            print(f"[CAPTURED] New video URL!")
                
                page.on("response", capture_redirect)
                page.goto(current_url)
                page.wait_for_timeout(3000)
                page.remove_listener("response", capture_redirect)
                
                print(f"\n[STEP 7] Tải video (multi-thread)...")
                result = download_file_multithread(current_url, video_file, cookies_dict, headers)
                
                if result == 'redirect':
                    # Tìm URL mới từ captured hoặc từ page
                    if redirect_urls:
                        current_url = clean_url(redirect_urls[0])
                        print(f"[DEBUG] Network URL: {current_url[:100]}...")
                        print(f"[INFO] Found new URL from network, following redirect...")
                        continue
                    else:
                        # Thử lấy từ page URL
                        page_url = page.url
                        if "videoplayback" in page_url and page_url != current_url:
                            current_url = clean_url(page_url)
                            print(f"[DEBUG] Page URL: {current_url[:100]}...")
                            print(f"[INFO] Using page URL as redirect...")
                            continue
                        
                        # Thử parse URL từ content của page
                        try:
                            content = page.content()
                            import re
                            # Tìm URL videoplayback trong HTML
                            matches = re.findall(r'(https://[^\s"\'<>]+videoplayback[^\s"\'<>]+mime=video[^\s"\'<>]*)', content)
                            if matches:
                                # Decode HTML entities
                                new_url = matches[0].replace('\\u0026', '&').replace('&amp;', '&')
                                current_url = clean_url(new_url)
                                print(f"[DEBUG] HTML URL: {current_url[:100]}...")
                                print(f"[INFO] Found URL in page content, following...")
                                continue
                        except Exception as e:
                            print(f"[DEBUG] Parse content error: {e}")
                    
                    print(f"[ERROR] No redirect URL found!")
                    break
                elif result == True:
                    print(f"[INFO] Video downloaded successfully!")
                    break
                else:
                    print(f"[ERROR] Video download failed!")
                    break
        
        # ===== STEP 8 & 9: Mở và tải audio =====
        if audio_url:
            current_url = audio_url
            max_redirects = 5
            for redirect_count in range(max_redirects):
                print(f"\n[STEP 8] Mở URL audio... (attempt {redirect_count + 1}/{max_redirects})")
                
                # Clear captured URLs để capture URL mới
                redirect_urls = []
                def capture_audio_redirect(response):
                    url = response.url
                    if "&mime=audio" in url and url != current_url:
                        if url not in redirect_urls:
                            redirect_urls.append(url)
                            print(f"[CAPTURED] New audio URL!")
                
                page.on("response", capture_audio_redirect)
                page.goto(current_url)
                page.wait_for_timeout(3000)
                page.remove_listener("response", capture_audio_redirect)
                
                print(f"\n[STEP 9] Tải audio (multi-thread)...")
                result = download_file_multithread(current_url, audio_file, cookies_dict, headers)
                
                if result == 'redirect':
                    # Tìm URL mới từ captured hoặc từ page
                    if redirect_urls:
                        current_url = clean_url(redirect_urls[0])
                        print(f"[DEBUG] Network URL: {current_url[:100]}...")
                        print(f"[INFO] Found new URL from network, following redirect...")
                        continue
                    else:
                        # Thử lấy từ page URL
                        page_url = page.url
                        if "videoplayback" in page_url and page_url != current_url:
                            current_url = clean_url(page_url)
                            print(f"[DEBUG] Page URL: {current_url[:100]}...")
                            print(f"[INFO] Using page URL as redirect...")
                            continue
                        
                        # Thử parse URL từ content của page
                        try:
                            content = page.content()
                            import re
                            # Tìm URL videoplayback trong HTML
                            matches = re.findall(r'(https://[^\s"\'<>]+videoplayback[^\s"\'<>]+mime=audio[^\s"\'<>]*)', content)
                            if matches:
                                # Decode HTML entities
                                new_url = matches[0].replace('\\u0026', '&').replace('&amp;', '&')
                                current_url = clean_url(new_url)
                                print(f"[DEBUG] HTML URL: {current_url[:100]}...")
                                print(f"[INFO] Found URL in page content, following...")
                                continue
                        except Exception as e:
                            print(f"[DEBUG] Parse content error: {e}")
                    
                    print(f"[ERROR] No redirect URL found!")
                    break
                elif result == True:
                    print(f"[INFO] Audio downloaded successfully!")
                    break
                else:
                    print(f"[ERROR] Audio download failed!")
                    break
        
        print("\n[INFO] Đóng browser...")
        browser.close()
    
    # ===== STEP 10: Gộp video + audio =====
    if video_url and audio_url:
        if os.path.exists(video_file) and os.path.exists(audio_file):
            merge_video_audio(video_file, audio_file, output_merged)
    
    # Output file path for web server to pick up
    if os.path.exists(output_merged):
        print(f"OUTPUT_FILE:{output_merged}")
    
    print("\n" + "=" * 60)
    print("HOÀN THÀNH!")
    print("=" * 60)

if __name__ == "__main__":
    main()


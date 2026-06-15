from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
import yt_dlp
import requests
import os
import re
import time
import base64
from pyDes import des, ECB, PAD_PKCS5

app = FastAPI(title="Anysong API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom yt-dlp logger to avoid console spam
class MyLogger:
    def debug(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        print(msg)

# Temporary directory for file downloads within workspace
DOWNLOADS_DIR = os.path.join(os.getcwd(), "downloads_temp")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Clean up leftover temporary files on server boot (prevents storage leaks)
def cleanup_temp_folder():
    try:
        if os.path.exists(DOWNLOADS_DIR):
            files_removed = 0
            for file in os.listdir(DOWNLOADS_DIR):
                file_path = os.path.join(DOWNLOADS_DIR, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    files_removed += 1
            if files_removed > 0:
                print(f"Janitor Cleanup: Removed {files_removed} orphaned temp files on boot.")
    except Exception as e:
        print(f"Janitor Cleanup Error: {e}")

cleanup_temp_folder()

def decrypt_saavn_url(encrypted_url: str) -> str:
    """Decrypts JioSaavn encrypted media URL using DES ECB decryption."""
    try:
        key = b"38346591"
        cipher = des(key, ECB, padmode=PAD_PKCS5)
        enc_data = base64.b64decode(encrypted_url.strip())
        dec_data = cipher.decrypt(enc_data)
        return dec_data.decode('utf-8')
    except Exception as e:
        print(f"JioSaavn decryption failed: {e}")
        return ""

def search_via_saavn(query: str) -> list:
    """Queries JioSaavn search API, decrypts URLs, and returns formatted results."""
    try:
        search_url = "https://www.jiosaavn.com/api.php"
        params = {
            "__call": "search.getResults",
            "_format": "json",
            "_marker": "0",
            "cc": "in",
            "includeMetaTags": "1",
            "q": query
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = requests.get(search_url, params=params, headers=headers, timeout=6)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            formatted = []
            for entry in results:
                song_id = entry.get("id")
                if not song_id:
                    continue
                    
                encrypted_url = entry.get("encrypted_media_url")
                stream_url = ""
                if encrypted_url:
                    decrypted = decrypt_saavn_url(encrypted_url)
                    if decrypted:
                        has_320 = str(entry.get("320kbps")).lower() == "true"
                        if has_320:
                            stream_url = decrypted.replace("_96.mp4", "_320.mp4")
                        else:
                            stream_url = decrypted.replace("_96.mp4", "_160.mp4")
                
                # Fetch image and convert 150x150 to high quality 500x500
                img = entry.get("image", "")
                if img and "150x150" in img:
                    img = img.replace("150x150", "500x500")
                elif img and "50x50" in img:
                    img = img.replace("50x50", "500x500")
                
                # Format duration
                duration_seconds = int(entry.get("duration", 0))
                duration_str = ""
                if duration_seconds:
                    minutes = int(duration_seconds // 60)
                    seconds = int(duration_seconds % 60)
                    duration_str = f"{minutes}:{seconds:02d}"
                else:
                    duration_str = "Unknown"
                
                # Uploader/Singers
                artists = entry.get("singers") or entry.get("primary_artists") or "Unknown Artist"
                
                formatted.append({
                    'id': song_id,
                    'source': 'saavn',
                    'title': entry.get('song', 'Unknown Title'),
                    'duration': duration_str,
                    'duration_seconds': duration_seconds,
                    'uploader': artists,
                    'views': f"{int(entry.get('play_count', 0)):,}" if entry.get('play_count') else "0",
                    'thumbnail': img,
                    'url': entry.get('perma_url', ''),
                    'stream_url': stream_url
                })
            return formatted
    except Exception as e:
        print(f"JioSaavn search failed: {e}")
    return []

# Standard static fallback list of Invidious instances
STATIC_INVIDIOUS_INSTANCES = [
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
    "https://invidious.tiekoetter.com",
    "https://invidious.f5.si",
    "https://yt.chocolatemoo53.com",
    "https://invidious.yewtu.be",
    "https://inv.tux.im",
    "https://vid.priv.au",
]

# Simple In-Memory TTL Cache Implementation for ultimate speed and efficiency
class TTLValue:
    def __init__(self, value, ttl: int):
        self.value = value
        self.expiry = time.time() + ttl

class InMemoryTTLCache:
    def __init__(self):
        self.store = {}

    def get(self, key: str):
        if key in self.store:
            item = self.store[key]
            if time.time() < item.expiry:
                return item.value
            else:
                del self.store[key]
        return None

    def set(self, key: str, value, ttl: int = 1800):
        self.store[key] = TTLValue(value, ttl)

# Instantiate caches
# Search Cache: 30 minutes TTL
search_cache = InMemoryTTLCache()
# Stream URL Cache: 1 hour TTL
stream_cache = InMemoryTTLCache()

def fetch_active_invidious_instances() -> list:
    """Dynamically retrieves active public Invidious instances to ensure maximum uptime."""
    try:
        r = requests.get("https://api.invidious.io/instances.json", timeout=5)
        if r.status_code == 200:
            instances_data = r.json()
            active_list = []
            for inst in instances_data:
                domain = inst[0]
                metadata = inst[1]
                
                # Exclude onion, i2p, yggdrasil and enforce active status monitor
                if (metadata.get('type') == 'https' and 
                    not domain.endswith('.onion') and 
                    not domain.endswith('.i2p') and 
                    not domain.endswith('.ygg')):
                    
                    monitor = metadata.get('monitor')
                    if monitor and not monitor.get('down') and monitor.get('last_status') == 200:
                        active_list.append(f"https://{domain}")
                        
            if active_list:
                return active_list
    except Exception as e:
        print(f"Failed to dynamically fetch Invidious instances: {e}. Falling back to static list.")
        
    return STATIC_INVIDIOUS_INSTANCES

def clean_filename(title: str) -> str:
    """Sanitizes the track title to make it a safe filename for headers."""
    clean = re.sub(r'[^a-zA-Z0-9 \-_().]', '', title)
    return clean.strip() or "song"

def search_via_invidious(query: str) -> list:
    """Failsafe search pipeline. Queries public Invidious instances if yt-dlp fails."""
    instances = fetch_active_invidious_instances()
    for instance in instances[:5]:
        try:
            api_url = f"{instance}/api/v1/search"
            r = requests.get(api_url, params={"q": query, "type": "video"}, timeout=6)
            if r.status_code == 200:
                results = []
                for entry in r.json():
                    video_id = entry.get('videoId')
                    if not video_id:
                        continue
                        
                    duration = entry.get('lengthSeconds', 0)
                    duration_str = ""
                    if duration:
                        minutes = int(duration // 60)
                        seconds = int(duration % 60)
                        duration_str = f"{minutes}:{seconds:02d}"
                    else:
                        duration_str = "Unknown"
                        
                    results.append({
                        'id': video_id,
                        'title': entry.get('title', 'Unknown Title'),
                        'duration': duration_str,
                        'duration_seconds': duration,
                        'uploader': entry.get('author', 'Unknown Artist'),
                        'views': f"{entry.get('viewCount', 0):,}",
                        'thumbnail': f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                        'url': f"https://www.youtube.com/watch?v={video_id}"
                    })
                return results
        except Exception as e:
            print(f"Invidious search fallback failed for {instance}: {e}")
            continue
    return []

def get_invidious_audio_stream(video_id: str):
    """Retrieves direct audio streams via healthy Invidious instances."""
    instances = fetch_active_invidious_instances()
    for instance in instances[:10]:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}?local=true"
            r = requests.get(api_url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                adaptive_formats = data.get("adaptiveFormats", [])
                
                audio_formats = [
                    f for f in adaptive_formats 
                    if f.get("mimeType", "").startswith("audio/") or f.get("type", "").startswith("audio/")
                ]
                
                if audio_formats:
                    audio_formats.sort(key=lambda x: (
                        1 if "audio/mp4" in x.get("mimeType", "") or "audio/mp4" in x.get("type", "") else 0,
                        int(x.get("bitrate", 0))
                    ), reverse=True)
                    
                    best_audio = audio_formats[0]
                    stream_url = best_audio.get("url")
                    
                    if stream_url:
                        if stream_url.startswith("/"):
                            stream_url = f"{instance}{stream_url}"
                            
                        mime = best_audio.get("mimeType", best_audio.get("type", ""))
                        ext = "m4a"
                        if "webm" in mime:
                            ext = "webm"
                        elif "ogg" in mime:
                            ext = "ogg"
                            
                        title = data.get("title", "song")
                        
                        return {
                            "url": stream_url,
                            "ext": ext,
                            "title": title
                        }
        except Exception as e:
            print(f"Invidious instance {instance} stream fetch failed: {e}")
            continue
            
    return None

@app.get("/api/search")
def search_songs(q: str = Query(..., min_length=1)):
    """Searches JioSaavn first, and falls back to YouTube/Invidious with built-in caching."""
    query_key = q.strip().lower()
    
    # Check cache first (returns in < 1ms)
    cached_val = search_cache.get(query_key)
    if cached_val:
        return {"results": cached_val, "cached": True}
        
    results = search_via_saavn(q)
    
    # Trigger YouTube fallback if JioSaavn returned no results
    if not results:
        print("JioSaavn search returned zero results. Falling back to YouTube...")
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'logger': MyLogger(),
            'extract_flat': 'in_playlist',
        }
        
        search_success = False
        
        # Attempt search via yt-dlp first
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                search_query = f"ytsearch10:{q}"
                info = ydl.extract_info(search_query, download=False)
                
                if info and 'entries' in info:
                    for entry in info['entries']:
                        if not entry:
                            continue
                        
                        video_id = entry.get('id')
                        if not video_id:
                            continue
                            
                        duration = entry.get('duration')
                        duration_str = ""
                        if duration:
                            minutes = int(duration // 60)
                            seconds = int(duration % 60)
                            duration_str = f"{minutes}:{seconds:02d}"
                        else:
                            duration_str = "Unknown"
                            
                        results.append({
                            'id': video_id,
                            'source': 'youtube',
                            'title': entry.get('title', 'Unknown Title'),
                            'duration': duration_str,
                            'duration_seconds': duration,
                            'uploader': entry.get('uploader') or entry.get('channel', 'Unknown Artist'),
                            'views': f"{entry.get('view_count', 0):,}" if entry.get('view_count') else "0",
                            'thumbnail': f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                            'url': f"https://www.youtube.com/watch?v={video_id}",
                            'stream_url': ''
                        })
                    search_success = True
            except Exception as e:
                print(f"Direct yt-dlp search failed: {e}. Trying Invidious search fallback...")
                
        # Trigger Invidious search fallback if yt-dlp failed
        if not search_success or not results:
            raw_invidious = search_via_invidious(q)
            if raw_invidious:
                for r in raw_invidious:
                    r['source'] = 'youtube'
                    r['stream_url'] = ''
                    results.append(r)
                print("Search successfully completed via Invidious search fallback.")
                
    if not results:
        return {"results": []}
        
    # Write to cache (30 minutes TTL)
    search_cache.set(query_key, results, ttl=1800)
    
    return {"results": results, "cached": False}

@app.get("/api/stream")
def stream_song(id: str = Query(..., min_length=1), source: str = Query("youtube")):
    """Proxies the audio stream from YouTube/Invidious or redirects for JioSaavn with stream URL caching."""
    if source == "saavn":
        cached_data = stream_cache.get(id)
        if cached_data:
            stream_url = cached_data["url"]
        else:
            stream_url = None
            try:
                search_url = "https://www.jiosaavn.com/api.php"
                params_details = {
                    "__call": "song.getDetails",
                    "_format": "json",
                    "pids": id
                }
                r_details = requests.get(search_url, params=params_details, timeout=6)
                if r_details.status_code == 200:
                    details_data = r_details.json()
                    song_details = details_data.get(id)
                    if song_details:
                        encrypted_url = song_details.get("encrypted_media_url")
                        if encrypted_url:
                            decrypted = decrypt_saavn_url(encrypted_url)
                            if decrypted:
                                has_320 = str(song_details.get("320kbps")).lower() == "true"
                                stream_url = decrypted.replace("_96.mp4", "_320.mp4") if has_320 else decrypted.replace("_96.mp4", "_160.mp4")
                                stream_cache.set(id, {"url": stream_url, "mime_type": "audio/mp4"}, ttl=3600)
            except Exception as e:
                print(f"JioSaavn stream details fetch failed: {e}")
                
        if not stream_url:
            raise HTTPException(status_code=404, detail="JioSaavn stream URL could not be resolved.")
            
        return RedirectResponse(url=stream_url)

    # Check cache first for YouTube
    cached_data = stream_cache.get(id)
    if cached_data:
        stream_url = cached_data["url"]
        mime_type = cached_data["mime_type"]
    else:
        stream_url = None
        mime_type = None
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'logger': MyLogger(),
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'android_music', 'web_embedded']
                }
            }
        }
        
        # Try resolving via yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=False)
                stream_url = info.get('url')
                mime_type = 'audio/mp4' if info.get('ext') == 'm4a' else 'audio/webm'
            except Exception as yt_err:
                print(f"yt-dlp stream extraction failed: {yt_err}. Trying Invidious fallback...")
                
        # Try resolving via Invidious
        if not stream_url:
            invidious_data = get_invidious_audio_stream(id)
            if invidious_data:
                stream_url = invidious_data["url"]
                ext = invidious_data["ext"]
                mime_type = 'audio/webm' if ext == 'webm' else 'audio/mp4'
                
        # If resolved successfully, store in cache (1 hour TTL)
        if stream_url and mime_type:
            stream_cache.set(id, {"url": stream_url, "mime_type": mime_type}, ttl=3600)
            
    if not stream_url:
        raise HTTPException(
            status_code=404, 
            detail="Audio stream not found. YouTube bot block active and all Invidious fallbacks exhausted."
        )
        
    try:
        r = requests.get(stream_url, stream=True, timeout=15)
        
        response_headers = {}
        headers_to_forward = ['Content-Type', 'Content-Length', 'Accept-Ranges']
        for h in headers_to_forward:
            if h in r.headers:
                response_headers[h] = r.headers[h]
                
        if 'Content-Type' not in response_headers and mime_type:
            response_headers['Content-Type'] = mime_type
            
        def iter_content():
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    yield chunk
            except Exception as stream_err:
                print(f"Streaming proxy interrupted: {stream_err}")
            finally:
                r.close()
                
        return StreamingResponse(iter_content(), headers=response_headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Streaming failed: {str(e)}")

@app.get("/api/download")
def download_song(id: str = Query(..., min_length=1), source: str = Query("youtube"), background_tasks: BackgroundTasks = BackgroundTasks()):
    """Downloads the audio file onto the server, and returns it as a direct download attachment."""
    if source == "saavn":
        stream_url = None
        title = "song"
        ext = "mp4"
        
        cached_data = stream_cache.get(id)
        if cached_data:
            stream_url = cached_data["url"]
            
        try:
            search_url = "https://www.jiosaavn.com/api.php"
            params_details = {
                "__call": "song.getDetails",
                "_format": "json",
                "pids": id
            }
            r_details = requests.get(search_url, params=params_details, timeout=6)
            if r_details.status_code == 200:
                details_data = r_details.json()
                song_details = details_data.get(id)
                if song_details:
                    title = song_details.get("song", "song")
                    encrypted_url = song_details.get("encrypted_media_url")
                    if encrypted_url:
                        decrypted = decrypt_saavn_url(encrypted_url)
                        if decrypted:
                            has_320 = str(song_details.get("320kbps")).lower() == "true"
                            stream_url = decrypted.replace("_96.mp4", "_320.mp4") if has_320 else decrypted.replace("_96.mp4", "_160.mp4")
        except Exception as e:
            print(f"JioSaavn download details fetch failed: {e}")
            
        if not stream_url:
            raise HTTPException(status_code=404, detail="JioSaavn download URL could not be resolved.")
            
        file_path = os.path.join(DOWNLOADS_DIR, f"{id}.{ext}")
        download_success = False
        try:
            r = requests.get(stream_url, stream=True, timeout=60)
            if r.status_code == 200:
                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                download_success = True
        except Exception as err:
            print(f"Saavn file download failed: {err}")
            
        if not download_success or not os.path.exists(file_path):
            if os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            raise HTTPException(status_code=500, detail="JioSaavn track download from CDN failed.")
            
        sanitized_title = clean_filename(title)
        filename = f"{sanitized_title}.mp3"
        
        def remove_file(path: str):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as clean_err:
                print(f"Error removing temp file {path}: {clean_err}")
                
        background_tasks.add_task(remove_file, file_path)
        
        return FileResponse(
            path=file_path,
            filename=filename,
            media_type='audio/mpeg',
            background=background_tasks
        )

    # YouTube download logic
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
        'quiet': True,
        'logger': MyLogger(),
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android_music', 'web_embedded']
            }
        }
    }
    
    file_path = None
    ext = 'm4a'
    title = 'song'
    download_success = False
    
    # Try downloading with yt-dlp first
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=True)
            ext = info.get('ext', 'm4a')
            file_path = os.path.join(DOWNLOADS_DIR, f"{id}.{ext}")
            title = info.get('title', 'song')
            if os.path.exists(file_path):
                download_success = True
        except Exception as yt_err:
            print(f"yt-dlp download failed: {yt_err}. Trying Invidious fallback...")
            
    # Try downloading with Invidious fallback
    if not download_success:
        invidious_data = get_invidious_audio_stream(id)
        if invidious_data:
            stream_url = invidious_data["url"]
            ext = invidious_data["ext"]
            title = invidious_data["title"]
            file_path = os.path.join(DOWNLOADS_DIR, f"{id}.{ext}")
            
            try:
                r = requests.get(stream_url, stream=True, timeout=60)
                if r.status_code == 200:
                    with open(file_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    download_success = True
            except Exception as inv_err:
                print(f"Invidious file download failed: {inv_err}")
                
    if not download_success or not file_path or not os.path.exists(file_path):
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        raise HTTPException(
            status_code=500, 
            detail="Download failed. YouTube bot block active and all Invidious fallbacks exhausted."
        )
        
    sanitized_title = clean_filename(title)
    filename = f"{sanitized_title}.{ext}"
    
    def remove_file(path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as clean_err:
            print(f"Error removing temp file {path}: {clean_err}")
            
    background_tasks.add_task(remove_file, file_path)
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/octet-stream',
        background=background_tasks
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

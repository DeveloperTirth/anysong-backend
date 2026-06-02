from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
import yt_dlp
import requests
import os
import re

app = FastAPI(title="Anysong API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

# Highly reliable static fallback list of Invidious instances
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

def fetch_active_invidious_instances() -> list:
    """
    Dynamically fetches the healthiest public HTTPS Invidious instances
    from the official Invidious API.
    """
    try:
        r = requests.get("https://api.invidious.io/instances.json", timeout=6)
        if r.status_code == 200:
            instances_data = r.json()
            active_list = []
            for inst in instances_data:
                domain = inst[0]
                metadata = inst[1]
                
                # Verify it is HTTPS, not darknet/isolated, and has active monitor success
                if (metadata.get('type') == 'https' and 
                    not domain.endswith('.onion') and 
                    not domain.endswith('.i2p') and 
                    not domain.endswith('.ygg')):
                    
                    monitor = metadata.get('monitor')
                    if monitor and monitor.get('statusClass') == 'success':
                        active_list.append(f"https://{domain}")
                        
            if active_list:
                print(f"Successfully fetched {len(active_list)} active Invidious instances dynamically.")
                return active_list
    except Exception as e:
        print(f"Failed to fetch dynamic Invidious instances: {e}. Using static fallbacks.")
        
    return STATIC_INVIDIOUS_INSTANCES

def clean_filename(title: str) -> str:
    """Sanitizes the track title to make it a safe filename for headers."""
    clean = re.sub(r'[^a-zA-Z0-9 \-_().]', '', title)
    return clean.strip() or "song"

def get_invidious_audio_stream(video_id: str):
    """
    Attempts to fetch the audio stream URL and format details for a video ID 
    by cycling through dynamic and static public Invidious instances.
    """
    instances = fetch_active_invidious_instances()
    
    # Try the top 10 instances to find a working stream
    for instance in instances[:10]:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}"
            # 8-second timeout to handle slow handshakes on free cloud tiers
            r = requests.get(api_url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                adaptive_formats = data.get("adaptiveFormats", [])
                
                # Filter for audio formats
                audio_formats = [
                    f for f in adaptive_formats 
                    if f.get("mimeType", "").startswith("audio/") or f.get("type", "").startswith("audio/")
                ]
                
                if audio_formats:
                    # Sort formats (prefer mp4/m4a, then webm; higher audio quality first)
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
            print(f"Invidious instance {instance} extraction failed: {e}")
            continue
            
    return None

@app.get("/api/search")
def search_songs(q: str = Query(..., min_length=1)):
    """Searches YouTube for tracks using yt-dlp and returns a list of results."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'logger': MyLogger(),
        'extract_flat': 'in_playlist',
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            search_query = f"ytsearch10:{q}"
            info = ydl.extract_info(search_query, download=False)
            
            results = []
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
                        'title': entry.get('title', 'Unknown Title'),
                        'duration': duration_str,
                        'duration_seconds': duration,
                        'uploader': entry.get('uploader') or entry.get('channel', 'Unknown Artist'),
                        'views': f"{entry.get('view_count', 0):,}" if entry.get('view_count') else "0",
                        'thumbnail': f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                        'url': f"https://www.youtube.com/watch?v={video_id}"
                    })
            return {"results": results}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.get("/api/stream")
def stream_song(id: str = Query(..., min_length=1)):
    """Proxies the audio stream from YouTube/Invidious to the client browser."""
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
    
    stream_url = None
    mime_type = None
    
    # Try resolving stream url using standard yt-dlp first
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=False)
            stream_url = info.get('url')
            mime_type = 'audio/mp4' if info.get('ext') == 'm4a' else 'audio/webm'
        except Exception as yt_err:
            print(f"Direct yt-dlp stream extraction failed: {yt_err}. Falling back to Invidious...")
            
    # Fallback to Invidious if yt-dlp failed
    if not stream_url:
        invidious_data = get_invidious_audio_stream(id)
        if invidious_data:
            stream_url = invidious_data["url"]
            ext = invidious_data["ext"]
            mime_type = 'audio/webm' if ext == 'webm' else 'audio/mp4'
            print("Successfully resolved stream URL via Invidious")
            
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
def download_song(id: str = Query(..., min_length=1), background_tasks: BackgroundTasks = BackgroundTasks()):
    """Downloads the audio file onto the server, and returns it as a direct download attachment."""
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
            print(f"Direct yt-dlp download failed: {yt_err}. Falling back to Invidious...")
            
    # Fallback to Invidious download
    if not download_success:
        invidious_data = get_invidious_audio_stream(id)
        if invidious_data:
            stream_url = invidious_data["url"]
            ext = invidious_data["ext"]
            title = invidious_data["title"]
            file_path = os.path.join(DOWNLOADS_DIR, f"{id}.{ext}")
            
            try:
                # 60-second timeout for slow file downloads over the network
                r = requests.get(stream_url, stream=True, timeout=60)
                if r.status_code == 200:
                    with open(file_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    download_success = True
                    print("Successfully downloaded file locally via Invidious")
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

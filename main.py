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

def clean_filename(title: str) -> str:
    """Sanitizes the track title to make it a safe filename for headers."""
    # Keep alphanumeric characters, spaces, dashes, and underscores
    clean = re.sub(r'[^a-zA-Z0-9 \-_().]', '', title)
    return clean.strip() or "song"

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
            # Limit search to 10 results for quick load
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
                    # Format duration to MM:SS
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
    """Proxies the audio stream from YouTube to the client browser."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'logger': MyLogger(),
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=False)
            stream_url = info.get('url')
            if not stream_url:
                raise HTTPException(status_code=404, detail="Audio stream not found")
            
            # Request the stream URL
            # Stream in chunks to the client to save server memory and proxy robustly
            r = requests.get(stream_url, stream=True, timeout=15)
            
            # Forward relevant headers
            response_headers = {}
            headers_to_forward = ['Content-Type', 'Content-Length', 'Accept-Ranges']
            for h in headers_to_forward:
                if h in r.headers:
                    response_headers[h] = r.headers[h]
                    
            if 'Content-Type' not in response_headers:
                # Fallback to audio/mp4 for m4a or webm if not present
                response_headers['Content-Type'] = 'audio/mp4' if info.get('ext') == 'm4a' else 'audio/webm'
                
            def iter_content():
                try:
                    for chunk in r.iter_content(chunk_size=65536):
                        yield chunk
                except Exception as stream_err:
                    print(f"Streaming interrupted: {stream_err}")
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
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Download audio file locally
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=True)
            ext = info.get('ext', 'm4a')
            file_path = os.path.join(DOWNLOADS_DIR, f"{id}.{ext}")
            
            if not os.path.exists(file_path):
                raise HTTPException(status_code=404, detail="File download failed")
                
            # Create a clean, human-readable file name for the user
            title = info.get('title', 'song')
            sanitized_title = clean_filename(title)
            filename = f"{sanitized_title}.{ext}"
            
            # Delete file after sending response
            def remove_file(path: str):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as clean_err:
                    print(f"Error removing temp file {path}: {clean_err}")
                    
            background_tasks.add_task(remove_file, file_path)
            
            # Return file
            return FileResponse(
                path=file_path,
                filename=filename,
                media_type='application/octet-stream',
                background=background_tasks
            )
        except Exception as e:
            # Clean up file in case of error during return
            file_path = os.path.join(DOWNLOADS_DIR, f"{id}.m4a")
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

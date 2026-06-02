# Anysong - Backend API

The backend engine for **Anysong**, a premium music streaming and downloading web application. This service is built using **FastAPI** and **yt-dlp** to fetch, stream, and download audio on-demand.

## Features

- **On-Demand Flat Search**: Uses `yt-dlp`'s flat search capability to retrieve search results (title, artist, duration, views, thumbnails) in under 2 seconds.
- **Audio Streaming Proxy**: Proxies the direct high-quality audio stream (AAC/Opus) from YouTube to the client browser to bypass CORS policies and IP address restrictions.
- **Async Temporary Downloader**: Downloads audio files locally and returns them as a direct download attachment. Automatically cleans up server storage using FastAPI's background tasks immediately after transmission.
- **No FFmpeg Required**: Natively handles high-quality `.m4a` and `.webm` files directly, offering compatibility across all modern web browsers and devices.

## API Documentation

### 1. Search Songs
Retrieves matching tracks for a given query.
- **Endpoint**: `/api/search`
- **Method**: `GET`
- **Query Parameters**:
  - `q` (string, required): Search query.
- **Response**:
  ```json
  {
    "results": [
      {
        "id": "video_id",
        "title": "Song Title",
        "duration": "MM:SS",
        "duration_seconds": 240,
        "uploader": "Artist Name",
        "views": "1,234,567",
        "thumbnail": "https://i.ytimg.com/vi/video_id/hqdefault.jpg",
        "url": "https://www.youtube.com/watch?v=video_id"
      }
    ]
  }
  ```

### 2. Stream Audio
Streams direct audio chunks for in-browser playback.
- **Endpoint**: `/api/stream`
- **Method**: `GET`
- **Query Parameters**:
  - `id` (string, required): YouTube video ID.
- **Response**: Streams binary audio data (`audio/webm` or `audio/mp4`). Supports byte-range seeking.

### 3. Download Audio File
Downloads the high-quality audio file to the local machine.
- **Endpoint**: `/api/download`
- **Method**: `GET`
- **Query Parameters**:
  - `id` (string, required): YouTube video ID.
- **Response**: Standardized attachment file response (`application/octet-stream`) with automatic server cleanup.

## Getting Started

### Prerequisites
- Python 3.8 or higher

### Installation

1. Create a virtual environment:
   ```bash
   python -m venv venv
   ```

2. Activate the virtual environment:
   - **Windows (CMD/PowerShell)**:
     ```powershell
     .\venv\Scripts\activate
     ```
   - **macOS/Linux**:
     ```bash
     source venv/bin/activate
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Start the development server:
   ```bash
   python main.py
   ```
   The API will be available at `http://127.0.0.1:8000`.

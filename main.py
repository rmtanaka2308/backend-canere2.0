import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
import re
from fastapi import FastAPI, File, UploadFile, Form, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from supabase_client import SUPABASE_URL, supabase

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # durante o dev, pode deixar assim
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def sanitize_filename(filename: str) -> str:
    return re.sub(r"[^\w\-.]", "_", filename)


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    filename: str = Form(...)  # nome digitado pelo usuário no frontend
):
    contents = await file.read()
    file_id = str(uuid.uuid4())

    # filename original (real do .mp3) usado apenas para storage
    safe_filename = sanitize_filename(file.filename)
    path = f"{file_id}-{safe_filename}/{safe_filename}"

    # Upload para Supabase Storage
    supabase.storage.from_("karaoke-songs").upload(path, contents)

    # URL pública
    original_url = f"{SUPABASE_URL}/storage/v1/object/public/karaoke-songs/{path}"

    # Insere no banco usando o NOME CUSTOMIZADO DO USUÁRIO
    supabase.table("songs").insert({
        "id": file_id,
        "filename": filename,  # ← esse é o nome que o usuário digitou!
        "original_url": original_url,
        "instrumental_url": None,
        "lyrics_json": None,
        "created_at": datetime.utcnow().isoformat(),
        "storage_path": path
    }).execute()

    return {"status": "ok", "id": file_id}


@app.get("/songs")
def get_songs(page: int = Query(1, ge=1), limit: int = Query(15, le=100)):
    start = (page - 1) * limit
    end = start + limit - 1
    response = supabase \
        .table("songs") \
        .select("*") \
        .order("created_at", desc=True) \
        .range(start, end) \
        .execute()

    return response.data

@app.get("/song-by-id")
def get_song_by_id(id:str = Query(...)):
    try: 
        song = supabase.table("songs").select().match({"id":id}).execute()
        return {"status" : "ok", "song": song}
    except Exception as e:
        raise HTTPException(status_code=404, detail= str(e))


@app.delete("/delete")
def delete_song(id: str = Query(...)):
    try:
        supabase.table("songs").delete().match({"id": id}).execute()
        return {"status": "ok", "id": id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/separate")
async def separate_song(id: str = Form(...), filename: str = Form(...)):
    # 1. Get original audio path from DB
    row = supabase.table("songs").select("audio_original_path").eq("id", id).single().execute()
    if not row.data or not row.data.get("audio_original_path"):
        raise HTTPException(status_code=404, detail="Song or audio path not found")

    original_audio_path = row.data["audio_original_path"]
    original_audio_path_p = Path(original_audio_path)
    
    # Define local and remote paths
    local_original_path = f"/tmp/{id}_{original_audio_path_p.name}"
    local_output_dir = f"/tmp/{id}_out"
    
    # 2. Download original audio from Storage
    try:
        audio_bytes = supabase.storage.from_("audio").download(original_audio_path)
        with open(local_original_path, "wb") as f:
            f.write(audio_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download audio: {e}")

    # 3. Run Spleeter
    spleeter_python = os.path.join(os.getcwd(), "spleeter-env", "bin", "python")
    spleeter_script = os.path.join(os.getcwd(), "run_spleeter.py")
    spleeter_result = subprocess.run(
        [spleeter_python, spleeter_script, local_original_path, local_output_dir],
        capture_output=True, text=True
    )

    if spleeter_result.returncode != 0:
        print("❌ Spleeter error:")
        print(spleeter_result.stderr)
        raise HTTPException(status_code=500, detail=f"Spleeter failed: {spleeter_result.stderr}")

    # 4. Convert instrumental to MP3
    local_instrumental_wav_path = Path(local_output_dir) / Path(Path(local_original_path).stem).name / "accompaniment.wav"
    if not local_instrumental_wav_path.exists():
        raise HTTPException(status_code=500, detail="Instrumental WAV file not found after spleeter.")

    local_instrumental_mp3_path = local_instrumental_wav_path.with_suffix(".mp3")
    
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(local_instrumental_wav_path),
            "-b:a", "192k", # Standard MP3 bitrate
            str(local_instrumental_mp3_path)
        ], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Failed to convert instrumental to MP3: {e.stderr}")

    # 5. Upload instrumental MP3
    remote_instrumental_path = str(original_audio_path_p.with_name(f"{original_audio_path_p.stem}-instrumental.mp3"))

    try:
        with open(local_instrumental_mp3_path, "rb") as f:
            supabase.storage.from_("audio").upload(
                path=remote_instrumental_path,
                file=f,
                file_options={"content-type": "audio/mpeg", "upsert": "true"}
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload instrumental: {e}")

    # 5. Update database
    try:
        supabase.table("songs").update({
            "audio_instrumental_path": remote_instrumental_path
        }).match({"id": id}).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update song record: {e}")

    # 6. Cleanup
    # ... (add cleanup logic for local files)
    
    return {"status": "ok", "instrumental_path": remote_instrumental_path}

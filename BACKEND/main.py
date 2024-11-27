

from fastapi import FastAPI, Depends, File, UploadFile, HTTPException
from fastapi.security import  OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pathlib import Path
import zipfile
import os
import json
from database import SessionLocal, init_db, User, Project,VerseFile,Chapter,Job
import logging
import requests
from fastapi import BackgroundTasks
import time
import auth
import shutil


logging.basicConfig(level=logging.DEBUG)


# Initialize the database
init_db()

# FastAPI app initialization
app = FastAPI()

# Directory for extracted files
UPLOAD_DIR = "extracted_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Dependency to get the database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()





def transcribe_verses(file_paths: list[str], db_session: Session):
    """
    Background task to transcribe verses and update the database.
    """
    try:
        for file_path in file_paths:
            # Retrieve the VerseFile entry based on the file path
            verse = db_session.query(VerseFile).filter(VerseFile.path == file_path).first()

            if not verse:
                logging.error(f"Verse file not found for path: {file_path}")
                continue

            # Create a job entry linked to the verse
            job = Job(verse_id=verse.verse_id, ai_jobid=None, status="pending")
            db_session.add(job)
            db_session.commit()
            db_session.refresh(job)

            try:
                # Call AI API for transcription
                result = call_ai_api(file_path)
                if "error" in result:
                    # Update job and verse statuses in case of an error
                    job.status = "failed"
                    verse.stt = False
                    verse.stt_msg = result.get("error", "Unknown error")
                else:
                    # Update the job with the AI job ID
                    ai_jobid = result.get("data", {}).get("jobId")
                    job.ai_jobid = ai_jobid
                    job.status = "in_progress"
                    db_session.add(job)
                    db_session.commit()

                    # Poll AI job status until it's finished
                    while True:
                        transcription_result = check_ai_job_status(ai_jobid)
                        job_status = transcription_result.get("data", {}).get("status")

                        if job_status == "job finished":
                            # Extract transcription results
                            transcriptions = transcription_result["data"]["output"]["transcriptions"]
                            for transcription in transcriptions:
                                audio_file = transcription["audioFile"]
                                transcribed_text = transcription["transcribedText"]

                                # Update the verse text and mark as successful
                                if os.path.basename(file_path) == audio_file:
                                    verse.text = transcribed_text
                                    verse.stt = True
                                    verse.stt_msg = "Transcription successful"
                                    break

                            job.status = "completed"
                            break

                        elif job_status == "job failed":
                            job.status = "failed"
                            verse.stt = False
                            verse.stt_msg = "AI transcription failed"
                            break

                        # Wait for a few seconds before polling again
                        time.sleep(5)

                # Save the updated job and verse statuses
                db_session.add(job)
                db_session.add(verse)
                db_session.commit()

            except Exception as e:
                # Handle errors during transcription
                job.status = "failed"
                verse.stt = False
                verse.stt_msg = f"Error during transcription: {str(e)}"
                db_session.add(job)
                db_session.add(verse)
                db_session.commit()
                logging.error(f"Error during transcription for verse {verse.verse_id}: {str(e)}")

    except Exception as e:
        logging.error(f"Error in transcribe_verses: {str(e)}")

    finally:
        db_session.close()



def check_ai_job_status(ai_jobid: str) -> dict:
    """
    Check the status of an AI transcription job.
    """
    job_status_url = f"https://api.vachanengine.org/v2/ai/model/job?job_id={ai_jobid}"
    headers = {"Authorization": "Bearer ory_st_mby05AoClJAHhX9Xlnsg1s0nn6Raybb3"}
    response = requests.get(job_status_url, headers=headers)

    if response.status_code == 200:
        return response.json()
    else:
        logging.error(f"Failed to fetch AI job status: {response.status_code} - {response.text}")
        return {"error": "Failed to fetch job status"}





def call_ai_api(file_path: str) -> dict:
    """
    Calls the AI API to transcribe the given audio file.
    """
    ai_api_url = "https://api.vachanengine.org/v2/ai/model/audio/transcribe?model_name=mms-1b-all"
    transcription_language = "hin"
    file_name = os.path.basename(file_path)
    api_token = "ory_st_mby05AoClJAHhX9Xlnsg1s0nn6Raybb3"

    try:
        with open(file_path, "rb") as audio_file:
            files_payload = {"files": (file_name, audio_file, "audio/wav")}
            data_payload = {"transcription_language": transcription_language}
            headers = {"Authorization": f"Bearer {api_token}"}

            # Make the API request
            response = requests.post(ai_api_url, files=files_payload, data=data_payload, headers=headers)
        logging.info(f"AI API Response: {response.status_code} - {response.text}")
        if response.status_code == 201:
            return response.json()  # {"data": {"jobId": "123", "status": "created"}}
        else:
            logging.error(f"AI API Error: {response.status_code} - {response.text}")
            return {"error": "Failed to transcribe", "status_code": response.status_code}
    except Exception as e:
        logging.error(f"Error in call_ai_api: {str(e)}")
        return {"error": "Exception occurred", "details": str(e)}



def generate_speech_for_verses(project_id: int, book_code: str, verses, audio_lang: str, db):
    """
    Generate speech for each verse and update the database.
    """
    db_session = SessionLocal()
    edited_audios_base_folder = f"{UPLOAD_DIR}/edited_audios"
    os.makedirs(edited_audios_base_folder, exist_ok=True)

    try:
        for verse in verses:
            try:
                # Create a job entry linked to the verse
                job = Job(verse_id=verse.verse_id, ai_jobid=None, status="pending")
                db_session.add(job)
                db_session.commit()
                db_session.refresh(job)

                # Call AI API for text-to-speech
                result = call_tts_api([verse.text], audio_lang)
                if "error" in result:
                    # Handle API error
                    job.status = "failed"
                    verse.tts = False
                    verse.tts_msg = result.get("error", "Unknown error")
                else:
                    # Update the job with the AI job ID
                    ai_jobid = result.get("data", {}).get("jobId")
                    job.ai_jobid = ai_jobid
                    job.status = "in_progress"
                    db_session.add(job)
                    db_session.commit()

                    # Poll AI job status until it's finished
                    while True:
                        job_result = check_ai_job_status(ai_jobid)
                        job_status = job_result.get("data", {}).get("status")

                        if job_status == "job finished":
                            # Download and extract the audio ZIP file
                            audio_zip_url = f"https://api.vachanengine.org/v2/ai/assets?job_id={ai_jobid}"
                            extracted_folder = download_and_extract_audio_zip(audio_zip_url)
                            if extracted_folder:
                                # Find and move the audio file to the proper chapter folder
                                for root, _, files in os.walk(extracted_folder):
                                    for file in files:
                                        if file == "audio_0.wav":
                                            # Rename and move the audio file to the chapter folder
                                            chapter_folder = os.path.join(
                                                edited_audios_base_folder, str(verse.chapter_id)
                                            )
                                            os.makedirs(chapter_folder, exist_ok=True)
                                            
                                            new_audio_path = os.path.join(chapter_folder, verse.name)
                                            shutil.move(os.path.join(root, file), new_audio_path)

                                            # Update verse information
                                            verse.tts_path = new_audio_path
                                            verse.tts = True
                                            verse.tts_msg = "Text-to-speech completed"
                                            job.status = "completed"
                                            break
                            else:
                                verse.tts = False
                                verse.tts_msg = "Failed to download or extract audio ZIP"
                                job.status = "failed"
                            break

                        elif job_status == "job failed":
                            job.status = "failed"
                            verse.tts = False
                            verse.tts_msg = "AI TTS job failed"
                            break
                        time.sleep(5)

                # Save the updated job and verse statuses
                db_session.add(job)
                db_session.add(verse)
                db_session.commit()

            except Exception as e:
                # Handle errors during TTS
                job.status = "failed"
                verse.tts = False
                verse.tts_msg = f"Error during TTS: {str(e)}"
                db_session.add(job)
                db_session.add(verse)
                db_session.commit()
                logging.error(f"Error during TTS for verse {verse.verse_id}: {str(e)}")

    except Exception as e:
        logging.error(f"Error in generate_speech_for_verses: {str(e)}")

    finally:
        db_session.close()


def download_and_extract_audio_zip(audio_zip_url: str) -> str:
    """
    Downloads the audio ZIP file, extracts it, and returns the folder path where files are extracted.
    """
    headers = {"Authorization": "Bearer ory_st_mby05AoClJAHhX9Xlnsg1s0nn6Raybb3"}
    response = requests.get(audio_zip_url, stream=True,headers=headers)
    if response.status_code == 200:
        # Save the ZIP file locally
        zip_file_path = f"{UPLOAD_DIR}/audio_temp.zip"
        with open(zip_file_path, "wb") as zip_file:
            for chunk in response.iter_content(chunk_size=1024):
                zip_file.write(chunk)
        # Extract the ZIP file
        extract_path = f"{UPLOAD_DIR}/temp_audio"
        os.makedirs(extract_path, exist_ok=True)
        with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
            zip_ref.extractall(extract_path)
        os.remove(zip_file_path)
        return extract_path
    else:
        logging.error(f"Failed to download audio ZIP file: {response.status_code} - {response.text}")
        return None



def find_audio_file(folder_path: str, verse_name: str) -> str:
    """
    Match the verse with an audio file in the extracted folder. 
    If files are generic (e.g., 'audio_0.wav'), use order to map.
    """
    for root, dirs, files in os.walk(folder_path):
        logging.info(f"Searching in folder: {root}, Files: {files}")
        # If there is a single audio file, assume it's for the current verse
        if len(files) == 1:
            return os.path.join(root, files[0])
        # If multiple files exist, attempt exact or approximate matches
        for file in files:
            if file == verse_name:
                return os.path.join(root, file)  # Exact match
            elif file.startswith("audio_") and file.endswith(".wav"):
                # Handle generic audio files (map based on verse order)
                return os.path.join(root, file)
    logging.error(f"Audio file not found for verse: {verse_name} in folder: {folder_path}")
    return None




def call_tts_api(text: str, audio_lang: str) -> dict:
    """
    Call the AI API for text-to-speech.
    """
    base_url = "https://api.vachanengine.org/v2/ai/model/audio/generate"
    model_name = "seamless-m4t-large"  # Model to be used
    api_token = "ory_st_mby05AoClJAHhX9Xlnsg1s0nn6Raybb3"
    print("AUDIO_LAN",audio_lang)
    # Query parameters
    params = {
        "model_name": model_name,
        "language": audio_lang,  # Include language as a query parameter
    }
    # API expects the entire payload to be a list
    data_payload = [  text ]
    headers = {"Authorization": f"Bearer {api_token}"}

    try:
        # Send the POST request with query parameters and JSON body
        response = requests.post(base_url, params=params, json=data_payload, headers=headers)
        logging.info(f"AI API Response: {response.status_code} - {response.text}")
        if response.status_code == 201:
            return response.json()  # Successfully created a TTS job
        else:
            logging.error(f"AI API Error: {response.status_code} - {response.text}")
            return {"error": response.text, "status_code": response.status_code}
    except Exception as e:
        logging.error(f"Error in call_tts_api: {str(e)}")
        return {"error": str(e)}





# Create User API
@app.post("/create-user/")
def create_user(username: str, password: str, db: Session = Depends(get_db)):
    hashed_password = auth.get_password_hash(password)
    user = User(username=username, password=hashed_password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "User created successfully", "user_id": user.user_id}

# Login API
@app.post("/token/")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    access_token = auth.create_access_token(data={"sub": str(user.user_id)})
    return {"access_token": access_token, "token_type": "bearer"}





@app.post("/upload-zip/{owner_id}")
async def upload_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
):
    try:
        owner_id = current_user.user_id

        # Ensure the uploaded file is a ZIP file
        if not file.filename.endswith(".zip"):
            raise HTTPException(status_code=400, detail="Uploaded file is not a ZIP file")

        # Save the uploaded ZIP file temporarily
        zip_path = Path(UPLOAD_DIR) / file.filename.replace(" ", "_")
        with open(zip_path, "wb") as buffer:
            buffer.write(await file.read())

        # Extract the ZIP file
        extract_path = Path(UPLOAD_DIR) / zip_path.stem
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)

        # Remove the ZIP file after extraction
        os.remove(zip_path)

        # Search for metadata.json recursively
        metadata_path = None
        for root, dirs, files in os.walk(extract_path):
            if "metadata.json" in files:
                metadata_path = Path(root) / "metadata.json"
                break

        if not metadata_path:
            raise HTTPException(status_code=400, detail="metadata.json not found in the ZIP file")

        # Read metadata.json
        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
            metadata_content = json.load(metadata_file)
        
        # Extract project name
        name = metadata_content.get("identification", {}).get("name", {}).get("en", "Unknown Project")
        # Extract language field from metadata.json
        # language_data = metadata_content.get("languages", [{}])[0]
        # language = language_data.get("name", {}).get(language_data.get("tag", "unknown"), "unknown")


        # Convert the full metadata.json content back to a JSON string
        metadata_info = json.dumps(metadata_content)

        # Create a new project entry
        project = Project(
            name=name,
            owner_id=owner_id,
            script_lang="",  # Empty field
            audio_lang="",   # Empty field
            metadata_info=metadata_info
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        # Search for ingredients folder recursively
        ingredients_path = None
        for root, dirs, files in os.walk(extract_path):
            if "ingredients" in dirs:
                ingredients_path = Path(root) / "ingredients"
                break

        if not ingredients_path:
            raise HTTPException(status_code=400, detail="Ingredients folder not found in the ZIP file")

        logging.debug(f"Found ingredients folder: {ingredients_path}")

        for book_dir in ingredients_path.iterdir():
            if book_dir.is_dir():
                book = book_dir.name
                for chapter_dir in book_dir.iterdir():
                    if chapter_dir.is_dir() and chapter_dir.name.isdigit():
                        chapter_number = int(chapter_dir.name)
                        chapter = Chapter(project_id=project.project_id, book=book, chapter=chapter_number, approved=False)
                        db.add(chapter)
                        db.commit()
                        db.refresh(chapter)

                        for verse_file in chapter_dir.iterdir():
                            if verse_file.is_file() and "_" in verse_file.stem:
                                try:
                                    verse_number = int(verse_file.stem.split("_")[1])
                                    verse = VerseFile(
                                        chapter_id=chapter.chapter_id,
                                        verse=verse_number,
                                        name=verse_file.name,
                                        path=str(verse_file),
                                        size=verse_file.stat().st_size,
                                        format=verse_file.suffix.lstrip("."),
                                        stt=False,
                                        text="",
                                        text_modified=False,
                                        tts=False,
                                        tts_path="",
                                        stt_msg="",
                                        tts_msg=""
                                    )
                                    db.add(verse)
                                except ValueError:
                                    logging.warning(f"Invalid file name format: {verse_file.name}")
                                    continue

        db.commit()

        return {
            "message": "ZIP file extracted, project and verses created successfully",
            "project_id": project.project_id,
        }

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="The file is not a valid ZIP archive")
    except Exception as e:
        logging.error(f"An error occurred: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/update-script-lang/{project_id}")
async def update_script_lang(
    project_id: int, 
    script_lang: str, 
    db: Session = Depends(get_db)
):
    """
    Update the script_lang field in the Project table for a given project_id.
    """
    # Fetch the project
    project = db.query(Project).filter(Project.project_id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update the script_lang field
    project.script_lang = script_lang
    db.commit()
    db.refresh(project)

    return {
        "message": "Script language updated successfully",
        "project_id": project_id,
        "script_lang": project.script_lang,
    }



@app.put("/update-audio-lang/{project_id}")
async def update_audio_lang(
    project_id: int,
    audio_lang: str,
    db: Session = Depends(get_db)
):
    """
    Update the audio_lang field in the Project table for a given project_id.
    """
    # Fetch the project
    project = db.query(Project).filter(Project.project_id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update the audio_lang field
    project.audio_lang = audio_lang
    db.commit()
    db.refresh(project)

    return {
        "message": "Audio language updated successfully",
        "project_id": project_id,
        "audio_lang": project.audio_lang,
    }




@app.post("/transcribe")
async def transcribe_book(
    project_id: int,
    book_code: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
):
    # Step 1: Validate Project and Book
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    verses = (
        db.query(VerseFile)
        .join(Chapter, VerseFile.chapter_id == Chapter.chapter_id)
        .filter(Chapter.project_id == project_id, Chapter.book == book_code)
        .all()
    )
    if not verses:
        raise HTTPException(status_code=404, detail="No verses found for the given book")

    # Step 2: Get file paths
    file_paths = [verse.path for verse in verses]  # Collect file paths from VerseFile entries
    print("@@@@@VERSES", verses)  # Debugging
    print("@@@@@File Paths", file_paths)  # Debugging

    # Step 3: Start Background Task
    background_tasks.add_task(transcribe_verses, file_paths, db)

    return {"message": "Transcription started for all verses"}


@app.get("/job-status/{jobid}")
async def get_job_status(jobid: int, db: Session = Depends(get_db),current_user: User = Depends(auth.get_current_user)):
    """
    API to check the status of a job using the job ID.
    """
    # Query the jobs table for the given jobid
    job = db.query(Job).filter(Job.jobid == jobid).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Fetch status from the local jobs table
    job_status = {
        "jobid": job.jobid,
        "ai_jobid": job.ai_jobid,
        "status": job.status,
    }

    return {"message": "Job status retrieved successfully", "data": job_status}



@app.get("/chapter-status/{project_id}/{book_code}/{chapter_number}")
async def get_chapter_status(
    project_id: int,
    book_code: str,
    chapter_number: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user)
):
    """
    Get the status of each verse in a chapter.
    """
    # Validate project and chapter
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(
            Chapter.project_id == project_id,
            Chapter.book == book_code,
            Chapter.chapter == chapter_number,
        )
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # Retrieve all verses for the chapter
    verses = db.query(VerseFile).filter(VerseFile.chapter_id == chapter.chapter_id).all()

    if not verses:
        return {"message": "No verses found for the chapter", "data": []}

    # Prepare the response with verse statuses
    verse_statuses = [
        {
            "verse_id": verse.verse_id,
            "verse_number": verse.verse,
            "stt": verse.stt,
            "stt_msg": verse.stt_msg,
            "text": verse.text,
        }
        for verse in verses
    ]

    return {
        "message": "Chapter status retrieved successfully",
        "chapter_info": {
            "project_id": project_id,
            "book_code": book_code,
            "chapter_number": chapter_number,
        },
        "data": verse_statuses,
    }



@app.put("/chapter/approve")
async def update_chapter_approval(
    project_id: int,
    book: str,
    chapter: int,
    approve: bool,
    db: Session = Depends(get_db)
):
    """
    Update the approved column in the Chapter table for a given project_id, book, and chapter.
    """
    # Fetch the chapter record
    chapter_record = (
        db.query(Chapter)
        .filter(
            Chapter.project_id == project_id,
            Chapter.book == book,
            Chapter.chapter == chapter
        )
        .first()
    )

    if not chapter_record:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # Update the approved column
    chapter_record.approved = approve
    db.commit()

    return {
        "message": "Chapter approval status updated",
        "project_id": project_id,
        "book": book,
        "chapter": chapter,
        "approved": chapter_record.approved
    }


@app.put("/verse/update-text")
async def update_verse_text(
    verse_id: int,
    modified_text: str,
    db: Session = Depends(get_db)
):
    """
    Update the text and set text_modified to True in the VerseFile table for a given verse_id.
    """
    # Fetch the verse record
    verse_record = db.query(VerseFile).filter(VerseFile.verse_id == verse_id).first()

    if not verse_record:
        raise HTTPException(status_code=404, detail="Verse not found")

    # Update the text and set text_modified to True
    verse_record.text = modified_text
    verse_record.text_modified = True
    db.commit()

    return {
        "message": "Verse text updated successfully",
        "verse_id": verse_id,
        "text": verse_record.text,
        "text_modified": verse_record.text_modified
    }




@app.post("/convert-to-speech")
async def convert_to_speech(
    project_id: int,
    book_code: str,
    # chapter:str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    API to convert text to speech for all verses in a chapter.
    """
    # Step 1: Validate Project and Book
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Fetch the chapters for the given book
    chapters = db.query(Chapter).filter(
        Chapter.project_id == project_id, Chapter.book == book_code
    ).all()

    if not chapters:
        raise HTTPException(status_code=404, detail="No chapters found for the given book")

    # Step 2: Gather verses with modified text
    verses = db.query(VerseFile).join(
        Chapter, VerseFile.chapter_id == Chapter.chapter_id
    ).filter(
        Chapter.project_id == project_id,
        Chapter.book == book_code,
        VerseFile.text_modified == True
    ).all()

    if not verses:
        return {"message": "No verses with modified text found"}

    # Step 3: Start Background Task
    background_tasks.add_task(
        generate_speech_for_verses, project_id, book_code, verses, project.audio_lang, db
    )

    return {
        "message": "Text-to-speech conversion started",
        "book_code": book_code,
        "project_id": project_id,
    }


 




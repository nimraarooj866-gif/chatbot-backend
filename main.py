import os
import io
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
import pdfplumber
import docx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    api_key=OPENAI_API_KEY,
    temperature=0.7
)

system_prompt = """You are a helpful AI assistant. You love programming and robotics, 
and you are an expert in deep learning and natural language processing."""

conversation_history = []

# Store uploaded file content in memory
uploaded_file_content = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), session_id: str = "default"):
    filename = file.filename.lower()
    content = await file.read()

    try:
        if filename.endswith(".pdf"):
            text = extract_pdf(content)
        elif filename.endswith(".docx"):
            text = extract_docx(content)
        elif filename.endswith(".txt"):
            text = content.decode("utf-8")
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type. Use PDF, DOCX, or TXT.")

        if not text.strip():
            raise HTTPException(status_code=400, detail="File is empty or could not be read.")

        # Save extracted text linked to session
        uploaded_file_content[session_id] = {
            "filename": file.filename,
            "text": text[:15000]  # limit to avoid token overflow
        }

        return {
            "message": f"File '{file.filename}' uploaded successfully!",
            "preview": text[:300] + "..." if len(text) > 300 else text
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


def extract_pdf(content: bytes) -> str:
    text = ""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def extract_docx(content: bytes) -> str:
    doc = docx.Document(io.BytesIO(content))
    return "\n".join([para.text for para in doc.paragraphs if para.text.strip()])


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id = request.session_id

    # Build system prompt — inject file content if uploaded
    if session_id in uploaded_file_content:
        file_info = uploaded_file_content[session_id]
        active_system = f"""{system_prompt}

The user has uploaded a file named '{file_info["filename"]}'. 
Answer all questions based on the content of this file.
If the answer is not in the file, say so clearly.

FILE CONTENT:
{file_info["text"]}"""
    else:
        active_system = system_prompt

    messages = [SystemMessage(content=active_system)]

    # Add conversation history for this session
    session_history = [m for m in conversation_history if m.get("session") == session_id]
    for msg in session_history[-10:]:  # last 10 messages only
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        else:
            from langchain_core.messages import AIMessage
            messages.append(AIMessage(content=msg["content"]))

    messages.append(HumanMessage(content=request.message))

    try:
        response = llm.invoke(messages)
        reply = response.content

        conversation_history.append({"session": session_id, "role": "user", "content": request.message})
        conversation_history.append({"session": session_id, "role": "assistant", "content": reply})

        return {"reply": reply}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/clear/{session_id}")
async def clear_session(session_id: str):
    global conversation_history
    conversation_history = [m for m in conversation_history if m.get("session") != session_id]
    uploaded_file_content.pop(session_id, None)
    return {"message": "Session cleared"}

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import os
import hashlib
import httpx
from dotenv import load_dotenv
import pdfplumber
import docx
import io

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SUPABASE_URL = "https://jizieprrymxrtjnxdewy.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImppemllcHJyeW14cnRqbnhkZXd5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIzODY2MTgsImV4cCI6MjA5Nzk2MjYxOH0.pvDT5l7fFWtsEpsZXtp8gmH39YQSWimKLJM2h6sRYUo"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

conversation_history = {}
uploaded_files = {}

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"

@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}

@app.post("/signup")
async def signup(req: SignupRequest):
    hashed = hash_password(req.password)
    async with httpx.AsyncClient() as c:
        # Check if user exists
        check = await c.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}",
            headers=SUPABASE_HEADERS
        )
        if check.json():
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Insert new user
        res = await c.post(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**SUPABASE_HEADERS, "Prefer": "return=representation"},
            json={"email": req.email, "password": hashed}
        )
        if res.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail="Signup failed")
        
        user = res.json()[0]
        return {"message": "Account created!", "user_id": user["id"], "email": user["email"]}

@app.post("/login")
async def login(req: LoginRequest):
    hashed = hash_password(req.password)
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}&password=eq.{hashed}",
            headers=SUPABASE_HEADERS
        )
        users = res.json()
        if not users:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        user = users[0]
        return {"message": "Login successful!", "user_id": user["id"], "email": user["email"]}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), user_id: str = "default"):
    content = await file.read()
    text = ""
    
    if file.filename.endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
    elif file.filename.endswith(".docx"):
        doc = docx.Document(io.BytesIO(content))
        text = "\n".join([p.text for p in doc.paragraphs])
    elif file.filename.endswith(".txt"):
        text = content.decode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, TXT supported")
    
    uploaded_files[user_id] = text[:5000]
    return {"message": f"File '{file.filename}' uploaded successfully!"}

@app.post("/chat")
async def chat(req: ChatRequest):
    if req.user_id not in conversation_history:
        conversation_history[req.user_id] = []

    system_prompt = "You are a helpful AI assistant expert in programming, robotics, deep learning and NLP. Always respond in the same language the user writes in."
    
    if req.user_id in uploaded_files:
        system_prompt += f"\n\nThe user has uploaded a file. Here is its content:\n\n{uploaded_files[req.user_id]}\n\nAnswer questions based on this file content. If the user asks something unrelated to the file, you can still help them."

    conversation_history[req.user_id].append({"role": "user", "content": req.message})

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system_prompt}] + conversation_history[req.user_id]
    )

    reply = response.choices[0].message.content
    conversation_history[req.user_id].append({"role": "assistant", "content": reply})

    return {"reply": reply}

@app.delete("/clear-file/{user_id}")
def clear_file(user_id: str):
    uploaded_files.pop(user_id, None)
    return {"message": "File cleared"}

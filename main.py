from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import os, io, httpx
from dotenv import load_dotenv
import pdfplumber
import docx

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
SUPABASE_AUTH = f"{SUPABASE_URL}/auth/v1"
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}

conversation_history = {}
uploaded_files = {}

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"

# ── Root ────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}

# ── SIGNUP — Supabase sends OTP email automatically ─────
@app.post("/signup")
async def signup(body: dict):
    email = body.get("email")
    password = body.get("password")
    if not email or not password:
        raise HTTPException(400, "Email and password required")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/signup",
            headers=SUPABASE_HEADERS,
            json={"email": email, "password": password}
        )
        data = res.json()
        if res.status_code not in [200, 201]:
            raise HTTPException(400, data.get("msg", data.get("message", "Signup failed")))
        # If email confirmation required, user gets OTP email from Supabase
        return {"message": "OTP sent to your email. Please verify.", "email": email}

# ── VERIFY OTP (email + token from Supabase email) ──────
@app.post("/verify-otp")
async def verify_otp(body: dict):
    email = body.get("email")
    token = body.get("token")
    otp_type = body.get("type", "signup")  # "signup" or "email"

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/verify",
            headers=SUPABASE_HEADERS,
            json={"email": email, "token": token, "type": otp_type}
        )
        data = res.json()
        if res.status_code != 200 or "error" in data:
            raise HTTPException(400, data.get("msg", data.get("error_description", "Invalid OTP")))
        user = data.get("user", {})
        return {
            "message": "Verified!",
            "user_id": user.get("id"),
            "email": user.get("email"),
            "access_token": data.get("access_token")
        }

# ── LOGIN — Supabase sends OTP email automatically ──────
@app.post("/login/send-otp")
async def login_send_otp(body: dict):
    email = body.get("email")
    if not email:
        raise HTTPException(400, "Email required")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/otp",
            headers=SUPABASE_HEADERS,
            json={"email": email, "create_user": False}
        )
        if res.status_code not in [200, 201, 204]:
            data = res.json()
            raise HTTPException(400, data.get("msg", "Failed to send OTP"))
        return {"message": "OTP sent to your email"}

# ── LOGIN VERIFY OTP ─────────────────────────────────────
@app.post("/login/verify-otp")
async def login_verify_otp(body: dict):
    email = body.get("email")
    token = body.get("token")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/verify",
            headers=SUPABASE_HEADERS,
            json={"email": email, "token": token, "type": "magiclink"}
        )
        data = res.json()
        if res.status_code != 200 or "error" in data:
            raise HTTPException(400, data.get("msg", data.get("error_description", "Invalid OTP")))
        user = data.get("user", {})
        return {
            "message": "Login successful!",
            "user_id": user.get("id"),
            "email": user.get("email"),
            "access_token": data.get("access_token")
        }

# ── FILE UPLOAD ──────────────────────────────────────────
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
        raise HTTPException(400, "Only PDF, DOCX, TXT supported")
    uploaded_files[user_id] = text[:5000]
    return {"message": f"File '{file.filename}' uploaded successfully!"}

# ── CHAT ─────────────────────────────────────────────────
@app.post("/chat")
async def chat(req: ChatRequest):
    if req.user_id not in conversation_history:
        conversation_history[req.user_id] = []

    system_prompt = "You are a helpful AI assistant expert in programming, robotics, deep learning and NLP. Always respond in the same language the user writes in."
    if req.user_id in uploaded_files:
        system_prompt += f"\n\nThe user has uploaded a file. Here is its content:\n\n{uploaded_files[req.user_id]}\n\nAnswer questions based on this file content."

    conversation_history[req.user_id].append({"role": "user", "content": req.message})
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system_prompt}] + conversation_history[req.user_id]
    )
    reply = response.choices[0].message.content
    conversation_history[req.user_id].append({"role": "assistant", "content": reply})
    return {"reply": reply}

# ── CLEAR FILE ───────────────────────────────────────────
@app.delete("/clear-file/{user_id}")
def clear_file(user_id: str):
    uploaded_files.pop(user_id, None)
    return {"message": "File cleared"}

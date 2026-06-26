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
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImppemllcHJyeW14cnRqbnhkZXd5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIzODY2MTgsImV4cCI6MjA5Nzk2MjYxOH0.pvDT5l7fFWtsEpsZXtp8gmH39YQSWimKLJM2h6sRYUo"
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # Add this in Railway
SUPABASE_AUTH = f"{SUPABASE_URL}/auth/v1"

ANON_HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "Content-Type": "application/json",
}

SERVICE_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

SUPABASE_REST = f"{SUPABASE_URL}/rest/v1"

uploaded_files = {}

# ── CHAT HISTORY HELPERS (Supabase DB) ──────────────────
async def save_message(user_id: str, role: str, content: str):
    async with httpx.AsyncClient() as c:
        await c.post(
            f"{SUPABASE_REST}/chat_messages",
            headers=SERVICE_HEADERS,
            json={"user_id": user_id, "role": role, "content": content}
        )

async def get_history(user_id: str, limit: int = 50):
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_REST}/chat_messages",
            headers=SERVICE_HEADERS,
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.asc",
                "limit": str(limit),
            }
        )
        if res.status_code != 200:
            return []
        return res.json()

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"

@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}

# ── SIGNUP ──────────────────────────────────────────────
@app.post("/signup")
async def signup(body: dict):
    email = body.get("email")
    password = body.get("password")
    if not email or not password:
        raise HTTPException(400, "Email and password required")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/signup",
            headers=ANON_HEADERS,
            json={"email": email, "password": password}
        )
        data = res.json()
        if res.status_code not in [200, 201]:
            raise HTTPException(400, data.get("msg", data.get("message", "Signup failed")))
        return {"message": "OTP sent to your email.", "email": email}

# ── VERIFY SIGNUP OTP ───────────────────────────────────
@app.post("/verify-otp")
async def verify_otp(body: dict):
    email = body.get("email")
    token = body.get("token")
    otp_type = body.get("type", "signup")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/verify",
            headers=ANON_HEADERS,
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

# ── LOGIN STEP 1: Verify password, then send OTP via Supabase Admin ──
@app.post("/login/send-otp")
async def login_send_otp(body: dict):
    email = body.get("email")
    password = body.get("password")
    if not email or not password:
        raise HTTPException(400, "Email and password required")

    async with httpx.AsyncClient() as c:
        # Step 1: Verify password
        res = await c.post(
            f"{SUPABASE_AUTH}/token?grant_type=password",
            headers=ANON_HEADERS,
            json={"email": email, "password": password}
        )
        data = res.json()
        if res.status_code != 200:
            raise HTTPException(400, data.get("error_description", "Invalid email or password"))

        # Step 2: Send OTP using Supabase Admin API (reauthentication OTP)
        service_headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
        }
        otp_res = await c.post(
            f"{SUPABASE_AUTH}/otp",
            headers=service_headers,
            json={"email": email, "create_user": False}
        )
        if otp_res.status_code not in [200, 201, 204]:
            otp_data = otp_res.json()
            raise HTTPException(400, otp_data.get("msg", otp_data.get("message", "Failed to send OTP")))

        return {"message": "OTP sent to your email"}

# ── LOGIN STEP 2: Verify OTP ────────────────────────────
@app.post("/login/verify-otp")
async def login_verify_otp(body: dict):
    email = body.get("email")
    token = body.get("token")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/verify",
            headers=ANON_HEADERS,
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
    # Load past messages from Supabase
    past_messages = await get_history(req.user_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in past_messages]

    system_prompt = (
        "You are a helpful AI assistant expert in programming, robotics, deep learning and NLP. "
        "You must only respond in English or Urdu (Roman Urdu or Urdu script, matching whatever the user uses). "
        "If the user writes in any other language, politely reply in English and tell them you only support English and Urdu. "
        "Never respond in any language other than English or Urdu, under any circumstances."
    )
    if req.user_id in uploaded_files:
        system_prompt += f"\n\nThe user has uploaded a file. Here is its content:\n\n{uploaded_files[req.user_id]}\n\nAnswer questions based on this file content."

    messages.append({"role": "user", "content": req.message})
    await save_message(req.user_id, "user", req.message)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system_prompt}] + messages
    )
    reply = response.choices[0].message.content

    await save_message(req.user_id, "assistant", reply)

    return {"reply": reply}

# ── GET CHAT HISTORY ─────────────────────────────────────
@app.get("/history/{user_id}")
async def history(user_id: str):
    past_messages = await get_history(user_id)
    return {
        "history": [
            {"role": m["role"], "content": m["content"]} for m in past_messages
        ]
    }

# ── CLEAR CHAT HISTORY ───────────────────────────────────
@app.delete("/clear-history/{user_id}")
async def clear_history(user_id: str):
    async with httpx.AsyncClient() as c:
        await c.delete(
            f"{SUPABASE_REST}/chat_messages",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}"}
        )
    return {"message": "Chat history cleared"}

# ── CLEAR FILE ───────────────────────────────────────────
@app.delete("/clear-file/{user_id}")
def clear_file(user_id: str):
    uploaded_files.pop(user_id, None)
    return {"message": "File cleared"}

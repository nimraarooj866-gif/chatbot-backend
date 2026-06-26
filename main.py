from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import os, hashlib, httpx, random, smtplib, time, io
from email.mime.text import MIMEText
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
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# In-memory OTP store: { email: { otp, expires, purpose, password_hash } }
otp_store = {}
conversation_history = {}
uploaded_files = {}

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()

def send_otp_email(to_email: str, otp: str, purpose: str):
    subject = "Your OTP Code - AI Chatbot"
    body = f"""
Hi,

Your OTP for {purpose} is:

  {otp}

This code expires in 5 minutes. Do not share it with anyone.

— AI Chatbot Team
"""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, to_email, msg.as_string())

# ── Models ──────────────────────────────────────────────
class EmailRequest(BaseModel):
    email: str

class SignupRequest(BaseModel):
    email: str
    password: str

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str

class LoginEmailRequest(BaseModel):
    email: str
    password: str

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"

# ── Root ────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}

# ── SIGNUP: Step 1 — send OTP ────────────────────────────
@app.post("/signup/send-otp")
async def signup_send_otp(req: SignupRequest):
    async with httpx.AsyncClient() as c:
        check = await c.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}",
            headers=SUPABASE_HEADERS
        )
        if check.json():
            raise HTTPException(400, "Email already registered")

    otp = str(random.randint(100000, 999999))
    otp_store[req.email] = {
        "otp": otp,
        "expires": time.time() + 300,
        "purpose": "Signup",
        "password_hash": hash_password(req.password)
    }
    send_otp_email(req.email, otp, "Signup")
    return {"message": "OTP sent to your email"}

# ── SIGNUP: Step 2 — verify OTP & create account ────────
@app.post("/signup/verify-otp")
async def signup_verify_otp(req: VerifyOTPRequest):
    entry = otp_store.get(req.email)
    if not entry or entry["purpose"] != "Signup":
        raise HTTPException(400, "No OTP found. Please request again.")
    if time.time() > entry["expires"]:
        otp_store.pop(req.email, None)
        raise HTTPException(400, "OTP expired. Please request again.")
    if entry["otp"] != req.otp:
        raise HTTPException(400, "Invalid OTP")

    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**SUPABASE_HEADERS, "Prefer": "return=representation"},
            json={"email": req.email, "password": entry["password_hash"]}
        )
        if res.status_code not in [200, 201]:
            raise HTTPException(500, "Account creation failed")
        user = res.json()[0]

    otp_store.pop(req.email, None)
    return {"message": "Account created!", "user_id": user["id"], "email": user["email"]}

# ── LOGIN: Step 1 — verify password & send OTP ──────────
@app.post("/login/send-otp")
async def login_send_otp(req: LoginEmailRequest):
    hashed = hash_password(req.password)
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}&password=eq.{hashed}",
            headers=SUPABASE_HEADERS
        )
        if not res.json():
            raise HTTPException(401, "Invalid email or password")

    otp = str(random.randint(100000, 999999))
    otp_store[req.email] = {
        "otp": otp,
        "expires": time.time() + 300,
        "purpose": "Login"
    }
    send_otp_email(req.email, otp, "Login")
    return {"message": "OTP sent to your email"}

# ── LOGIN: Step 2 — verify OTP ──────────────────────────
@app.post("/login/verify-otp")
async def login_verify_otp(req: VerifyOTPRequest):
    entry = otp_store.get(req.email)
    if not entry or entry["purpose"] != "Login":
        raise HTTPException(400, "No OTP found. Please request again.")
    if time.time() > entry["expires"]:
        otp_store.pop(req.email, None)
        raise HTTPException(400, "OTP expired. Please request again.")
    if entry["otp"] != req.otp:
        raise HTTPException(400, "Invalid OTP")

    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}",
            headers=SUPABASE_HEADERS
        )
        user = res.json()[0]

    otp_store.pop(req.email, None)
    return {"message": "Login successful!", "user_id": user["id"], "email": user["email"]}

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

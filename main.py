from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os, io, httpx, uuid
from dotenv import load_dotenv

# LangChain imports
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import SystemMessage, HumanMessage, AIMessage

# File parsing
import pdfplumber
import docx as python_docx

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── LangChain LLM + Embeddings ───────────────────────────
llm = ChatOpenAI(
    model="gpt-4o-mini",
    api_key=os.getenv("OPENAI_API_KEY"),
    temperature=0.7
)

embeddings_model = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=os.getenv("OPENAI_API_KEY")
)

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50
)

# ── Supabase Config ──────────────────────────────────────
SUPABASE_URL = "https://jizieprrymxrtjnxdewy.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImppemllcHJyeW14cnRqbnhkZXd5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIzODY2MTgsImV4cCI6MjA5Nzk2MjYxOH0.pvDT5l7fFWtsEpsZXtp8gmH39YQSWimKLJM2h6sRYUo"
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
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


# ── Models ───────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    chat_id: Optional[str] = None
    docs_enabled: bool = True


# ── Root ─────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}


# ── Chat History Helpers ──────────────────────────────────
async def save_message(user_id: str, chat_id: str, role: str, content: str):
    async with httpx.AsyncClient() as c:
        await c.post(
            f"{SUPABASE_REST}/chat_messages",
            headers=SERVICE_HEADERS,
            json={"user_id": user_id, "chat_id": chat_id, "role": role, "content": content}
        )

async def get_messages_for_chat(chat_id: str, limit: int = 50):
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_REST}/chat_messages",
            headers=SERVICE_HEADERS,
            params={
                "chat_id": f"eq.{chat_id}",
                "order": "created_at.asc",
                "limit": str(limit),
            }
        )
        if res.status_code != 200:
            return []
        return res.json()

async def get_or_create_chat(user_id: str, chat_id: str = None, title: str = "New Chat"):
    async with httpx.AsyncClient() as c:
        if chat_id:
            res = await c.get(
                f"{SUPABASE_REST}/chats",
                headers=SERVICE_HEADERS,
                params={"id": f"eq.{chat_id}", "user_id": f"eq.{user_id}"}
            )
            if res.status_code == 200 and res.json():
                return res.json()[0]
        new_id = str(uuid.uuid4())
        res = await c.post(
            f"{SUPABASE_REST}/chats",
            headers=SERVICE_HEADERS,
            json={"id": new_id, "user_id": user_id, "title": title}
        )
        if res.status_code not in [200, 201]:
            raise HTTPException(500, "Failed to create chat")
        return res.json()[0]

async def update_chat_title(chat_id: str, title: str):
    async with httpx.AsyncClient() as c:
        await c.patch(
            f"{SUPABASE_REST}/chats",
            headers=SERVICE_HEADERS,
            params={"id": f"eq.{chat_id}"},
            json={"title": title}
        )


# ── RAG: Store Chunks with Embeddings ────────────────────
async def store_chunks(user_id: str, filename: str, text: str):
    chunks = text_splitter.split_text(text)
    async with httpx.AsyncClient(timeout=60.0) as c:
        for chunk in chunks:
            embedding = embeddings_model.embed_query(chunk)
            await c.post(
                f"{SUPABASE_REST}/doc_chunks",
                headers=SERVICE_HEADERS,
                json={
                    "user_id": user_id,
                    "filename": filename,
                    "content": chunk,
                    "embedding": embedding
                }
            )

async def retrieve_relevant_chunks(user_id: str, query: str, top_k: int = 5) -> str:
    query_embedding = embeddings_model.embed_query(query)
    async with httpx.AsyncClient(timeout=30.0) as c:
        res = await c.post(
            f"{SUPABASE_URL}/rest/v1/rpc/match_doc_chunks",
            headers=SERVICE_HEADERS,
            json={
                "query_embedding": query_embedding,
                "match_user_id": user_id,
                "match_count": top_k
            }
        )
        if res.status_code != 200 or not res.json():
            return ""
        chunks = res.json()
        # Group by filename for context
        parts = []
        seen_files = {}
        for chunk in chunks:
            fname = chunk.get("filename", "document")
            if fname not in seen_files:
                seen_files[fname] = []
            seen_files[fname].append(chunk["content"])
        for fname, contents in seen_files.items():
            parts.append(f"--- From: {fname} ---\n" + "\n".join(contents))
        return "\n\n".join(parts)


# ── AUTH: Signup ─────────────────────────────────────────
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


# ── AUTH: Verify Signup OTP ───────────────────────────────
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


# ── AUTH: Login Step 1 ────────────────────────────────────
@app.post("/login/send-otp")
async def login_send_otp(body: dict):
    email = body.get("email")
    password = body.get("password")
    if not email or not password:
        raise HTTPException(400, "Email and password required")
    async with httpx.AsyncClient() as c:
        res = await c.post(
            f"{SUPABASE_AUTH}/token?grant_type=password",
            headers=ANON_HEADERS,
            json={"email": email, "password": password}
        )
        data = res.json()
        if res.status_code != 200:
            raise HTTPException(400, data.get("error_description", "Invalid email or password"))
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


# ── AUTH: Login Step 2 ────────────────────────────────────
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


# ── Chats: New ────────────────────────────────────────────
@app.post("/chats/new")
async def new_chat(body: dict):
    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(400, "user_id required")
    chat = await get_or_create_chat(user_id, title="New Chat")
    return {"chat": chat}


# ── Chats: List ───────────────────────────────────────────
@app.get("/chats/{user_id}")
async def list_chats(user_id: str):
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_REST}/chats",
            headers=SERVICE_HEADERS,
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": "50"
            }
        )
        if res.status_code != 200:
            return {"chats": []}
        return {"chats": res.json()}


# ── Chats: Delete ─────────────────────────────────────────
@app.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str):
    async with httpx.AsyncClient() as c:
        await c.delete(
            f"{SUPABASE_REST}/chat_messages",
            headers=SERVICE_HEADERS,
            params={"chat_id": f"eq.{chat_id}"}
        )
        await c.delete(
            f"{SUPABASE_REST}/chats",
            headers=SERVICE_HEADERS,
            params={"id": f"eq.{chat_id}"}
        )
    return {"message": "Chat deleted"}


# ── Messages: Get ─────────────────────────────────────────
@app.get("/messages/{chat_id}")
async def get_chat_messages(chat_id: str):
    msgs = await get_messages_for_chat(chat_id)
    return {"messages": [{"role": m["role"], "content": m["content"]} for m in msgs]}


# ── Chat: Send Message (LangChain) ────────────────────────
@app.post("/chat")
async def chat(req: ChatRequest):
    chat_obj = await get_or_create_chat(req.user_id, req.chat_id)
    chat_id = chat_obj["id"]

    past_messages = await get_messages_for_chat(chat_id)

    system_prompt = (
        "You are a helpful AI assistant expert in programming, robotics, deep learning and NLP. "
        "You must only respond in English or Urdu (Roman Urdu or Urdu script, matching whatever the user uses). "
        "If the user writes in any other language, politely reply in English and tell them you only support English and Urdu. "
        "Never respond in any language other than English or Urdu, under any circumstances."
    )

    # RAG: retrieve relevant chunks if docs enabled
    if req.docs_enabled:
        relevant_context = await retrieve_relevant_chunks(req.user_id, req.message)
        if relevant_context:
            system_prompt += (
                f"\n\nThe user has uploaded documents. Here are the most relevant excerpts for their question:\n\n"
                f"{relevant_context}\n\n"
                f"Answer based on this content when relevant."
            )

    # Build LangChain messages
    lc_messages = [SystemMessage(content=system_prompt)]
    for m in past_messages:
        if m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        else:
            lc_messages.append(AIMessage(content=m["content"]))
    lc_messages.append(HumanMessage(content=req.message))

    # Save user message
    await save_message(req.user_id, chat_id, "user", req.message)

    # LangChain LLM call
    response = llm.invoke(lc_messages)
    reply = response.content

    await save_message(req.user_id, chat_id, "assistant", reply)

    # Auto-title from first message
    if len(past_messages) == 0:
        title = req.message[:50] + ("..." if len(req.message) > 50 else "")
        await update_chat_title(chat_id, title)

    return {"reply": reply, "chat_id": chat_id}


# ── File Upload (LangChain chunking + embeddings) ─────────
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), user_id: str = "default"):
    content = await file.read()
    text = ""

    if file.filename.endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
    elif file.filename.endswith(".docx"):
        d = python_docx.Document(io.BytesIO(content))
        text = "\n".join([p.text for p in d.paragraphs])
    elif file.filename.endswith(".txt"):
        text = content.decode("utf-8")
    else:
        raise HTTPException(400, "Only PDF, DOCX, TXT supported")

    if not text.strip():
        raise HTTPException(400, "Could not extract text from file")

    # Delete old chunks for this file (replace on re-upload)
    async with httpx.AsyncClient() as c:
        await c.delete(
            f"{SUPABASE_REST}/doc_chunks",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}", "filename": f"eq.{file.filename}"}
        )

    # Also update user_docs table (for file list)
    async with httpx.AsyncClient() as c:
        await c.delete(
            f"{SUPABASE_REST}/user_docs",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}", "filename": f"eq.{file.filename}"}
        )
        await c.post(
            f"{SUPABASE_REST}/user_docs",
            headers=SERVICE_HEADERS,
            json={
                "user_id": user_id,
                "filename": file.filename,
                "content": text[:500]  # preview only
            }
        )

    # Store chunks with embeddings
    await store_chunks(user_id, file.filename, text)

    return {"message": f"File '{file.filename}' uploaded and indexed successfully!"}


# ── Docs: List ────────────────────────────────────────────
@app.get("/docs/{user_id}")
async def get_user_docs(user_id: str):
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_REST}/user_docs",
            headers=SERVICE_HEADERS,
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.asc",
                "select": "id,filename,created_at"
            }
        )
        if res.status_code != 200:
            return {"docs": []}
        return {"docs": res.json()}


# ── Docs: Delete One ──────────────────────────────────────
@app.delete("/docs/{user_id}/{filename}")
async def delete_doc(user_id: str, filename: str):
    async with httpx.AsyncClient() as c:
        await c.delete(
            f"{SUPABASE_REST}/user_docs",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}", "filename": f"eq.{filename}"}
        )
        await c.delete(
            f"{SUPABASE_REST}/doc_chunks",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}", "filename": f"eq.{filename}"}
        )
    return {"message": f"Doc '{filename}' deleted"}


# ── Clear All Files ───────────────────────────────────────
@app.delete("/clear-file/{user_id}")
async def clear_file(user_id: str):
    async with httpx.AsyncClient() as c:
        await c.delete(
            f"{SUPABASE_REST}/user_docs",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}"}
        )
        await c.delete(
            f"{SUPABASE_REST}/doc_chunks",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}"}
        )
    return {"message": "All files cleared"}


# ── Legacy: History ───────────────────────────────────────
@app.get("/history/{user_id}")
async def history(user_id: str):
    async with httpx.AsyncClient() as c:
        res = await c.get(
            f"{SUPABASE_REST}/chats",
            headers=SERVICE_HEADERS,
            params={"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": "1"}
        )
        chats = res.json() if res.status_code == 200 else []
        if not chats:
            return {"history": []}
        msgs = await get_messages_for_chat(chats[0]["id"])
        return {"history": [{"role": m["role"], "content": m["content"]} for m in msgs]}

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    api_key=os.getenv("OPENAI_API_KEY"),
    temperature=0.7
)

SYSTEM_PROMPT = """You are a professional AI assistant — helpful, concise, and knowledgeable.
You specialize in programming, robotics, deep learning, and natural language processing.
Always respond clearly and in the same language the user writes in."""

class Message(BaseModel):
    role: str  # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]

@app.get("/")
def root():
    return {"status": "AI Chatbot API is running"}

@app.post("/chat")
def chat(req: ChatRequest):
    history = []
    for msg in req.messages:
        if msg.role == "user":
            history.append(HumanMessage(content=msg.content))
        else:
            history.append(AIMessage(content=msg.content))

    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history

    try:
        response = llm.invoke(messages)
        return {"reply": response.content}
    except Exception as e:
        return {"error": str(e)}, 500

import os

from dotenv import load_dotenv

load_dotenv()

OPEN_AI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPEN_AI_API_KEY:
    raise ValueError("OPENAI_API_KEY is not set in the environment variables.")
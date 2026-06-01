from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging

configure_logging()

app = FastAPI(title="AWS Twilio Voice Assistant")


@app.get("/health")
async def health() -> dict[str, str]:
    get_settings()
    return {"status": "healthy"}


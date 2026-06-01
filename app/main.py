from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging
from app.twilio.webhook import router as twilio_router

configure_logging()

app = FastAPI(title="AWS Twilio Voice Assistant")
app.include_router(twilio_router)


@app.get("/health")
async def health() -> dict[str, str]:
    get_settings()
    return {"status": "healthy"}

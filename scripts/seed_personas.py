import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.personas import DynamoPersonaRepository, Persona


DEFAULT_PERSONAS = [
    Persona(
        persona_id="warm_clinical_followup",
        display_name="Warm Clinical Follow-up",
        system_prompt=(
            "You are a warm, concise clinical follow-up voice assistant. "
            "Speak naturally on the phone, ask one question at a time, and keep responses brief. "
            "Do not diagnose or provide medical advice. If the caller reports urgent symptoms, advise them to contact emergency services or their clinician."
        ),
    ),
    Persona(
        persona_id="appointment_reminder",
        display_name="Appointment Reminder",
        system_prompt=(
            "You are a concise appointment reminder voice assistant. "
            "Confirm the caller is available, remind them about their upcoming appointment, and ask whether they need help preparing. "
            "Keep the conversation short, polite, and practical."
        ),
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed or update voice-agent personas in DynamoDB.")
    parser.add_argument("--table-name", default=None, help="DynamoDB personas table name. Defaults to PERSONAS_TABLE_NAME.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    table_name = args.table_name or settings.personas_table_name
    repository = DynamoPersonaRepository(table_name=table_name)

    for persona in DEFAULT_PERSONAS:
        repository.put_persona(persona)
        print(f"upserted persona_id={persona.persona_id} table={table_name}")


if __name__ == "__main__":
    main()

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.transcripts import DynamoTranscriptRepository, TranscriptRepository, format_transcript


def get_transcript_text(*, repository: TranscriptRepository, session_id: str) -> str:
    return format_transcript(repository.list_turns(session_id))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retrieve an ordered call transcript from DynamoDB.")
    parser.add_argument("--session-id", required=True, help="Internal voice-agent session_id.")
    parser.add_argument(
        "--table-name",
        default=None,
        help="DynamoDB transcript_turns table name. Defaults to TRANSCRIPT_TURNS_TABLE_NAME.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    table_name = args.table_name or settings.transcript_turns_table_name
    repository = DynamoTranscriptRepository(table_name=table_name)
    print(get_transcript_text(repository=repository, session_id=args.session_id))


if __name__ == "__main__":
    main()

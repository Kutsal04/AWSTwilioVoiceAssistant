from scripts.nova_sonic_spike import print_event_summary


def test_print_event_summary_for_unknown_event_shows_keys_without_payload(capsys) -> None:
    print_event_summary(
        "unknown",
        role=None,
        content_name=None,
        audio_bytes=None,
        raw_event={"event": {"textOutput": {"content": "do not print"}}},
    )

    output = capsys.readouterr().out

    assert "event=unknown" in output
    assert "raw_event_keys=textOutput" in output
    assert "do not print" not in output

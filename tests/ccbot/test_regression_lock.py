"""Compatibility lock — tripwire for accidental regression during the Codex refactor.

This file pins the import surface of out-of-scope flows listed in
`doc/ontology.md`. The Codex refactor introduces an `Agent` abstraction
(see T3) and moves Claude-specific logic behind it. The modules below should
remain importable and expose the public symbols they expose today, regardless
of internal reorganisation.

If a future refactor renames or relocates these symbols, update the imports
here AND update `doc/ontology.md`'s "Out of scope" section in the same commit.
"""

from __future__ import annotations


def test_voice_transcription_public_api() -> None:
    from ccbot.transcribe import transcribe_voice  # noqa: F401


def test_telegram_sender_public_api() -> None:
    from ccbot.telegram_sender import split_message  # noqa: F401


def test_markdown_conversion_public_api() -> None:
    from ccbot.markdown_v2 import convert_markdown  # noqa: F401


def test_hook_installer_public_api() -> None:
    from ccbot.hook import hook_main  # noqa: F401


def test_message_queue_public_api() -> None:
    from ccbot.handlers.message_queue import (  # noqa: F401
        MessageTask,
        get_or_create_queue,
    )


def test_status_polling_public_api() -> None:
    from ccbot.handlers.status_polling import update_status_message  # noqa: F401


def test_response_builder_public_api() -> None:
    from ccbot.handlers.response_builder import build_response_parts  # noqa: F401

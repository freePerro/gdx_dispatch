"""An absent optional dependency is an expected condition, not an error.

`routers/voice.py::_transcribe` logged `log.exception` for the Whisper
ImportError, so every voice note wrote a full stack trace: whisper is not in
the image. The absent package now logs one INFO line with no exc_info, and
the fallback text is still returned (GDXA-1554). A whisper that is installed
but cannot import its own dependencies is a broken image, so that case keeps
its traceback.
"""
from __future__ import annotations

import logging
import sys

FALLBACK = "[Voice note — audio saved as note.webm, transcription unavailable]"


def _audio(tmp_path):
    audio = tmp_path / "note.webm"
    audio.write_bytes(b"\x00")
    return audio


def test_transcribe_without_whisper_logs_no_traceback(tmp_path, monkeypatch, caplog):
    from gdx_dispatch.routers import voice

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # None in sys.modules makes `import whisper` raise ModuleNotFoundError
    # (name="whisper") whether or not the package is installed.
    monkeypatch.setitem(sys.modules, "whisper", None)

    with caplog.at_level(logging.DEBUG, logger=voice.log.name):
        text = voice._transcribe(_audio(tmp_path))

    assert text == FALLBACK
    assert [r for r in caplog.records if r.exc_info] == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "local_whisper_unavailable"),
    ]


def test_transcribe_with_broken_whisper_keeps_the_traceback(tmp_path, monkeypatch, caplog):
    from gdx_dispatch.routers import voice

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pkg = tmp_path / "pkgs" / "whisper"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("import gdxa1554_missing_whisper_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
    monkeypatch.delitem(sys.modules, "whisper", raising=False)

    with caplog.at_level(logging.DEBUG, logger=voice.log.name):
        text = voice._transcribe(_audio(tmp_path))

    assert text == FALLBACK
    assert [(r.getMessage(), bool(r.exc_info)) for r in caplog.records] == [
        ("local_whisper_import_failed", True),
    ]

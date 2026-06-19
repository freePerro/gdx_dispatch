"""Sprint 1.x-S5 — tenant-scoped Anthropic client factory.

Tests stay below the network: ``get_key`` is patched to control whether a
key exists. Real-key integration is the work-order's lab verify step.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from anthropic import Anthropic
from sqlalchemy.orm import Session

from gdx_dispatch.core.llm.anthropic_client import LLMNotConfigured, get_client


def test_get_client_raises_when_no_key():
    db = MagicMock(spec=Session)
    tid = uuid4()
    with patch("gdx_dispatch.core.llm.anthropic_client.get_key", return_value=None):
        with pytest.raises(LLMNotConfigured, match=str(tid)):
            get_client(db, tid)


def test_get_client_returns_configured_anthropic_instance():
    db = MagicMock(spec=Session)
    tid = uuid4()
    with patch("gdx_dispatch.core.llm.anthropic_client.get_key", return_value="sk-ant-test-12345"):
        client = get_client(db, tid)
    assert isinstance(client, Anthropic)
    assert client.api_key == "sk-ant-test-12345"

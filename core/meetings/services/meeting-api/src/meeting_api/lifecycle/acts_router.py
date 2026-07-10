"""The acts.v1 speak/config control-plane routes — the HTTP wrappers for the voice commands
(``POST``/``DELETE /bots/{platform}/{native_meeting_id}/speak`` + ``PUT .../config``).

The gateway ALREADY forwards these paths to meeting-api (gateway app.py routes speak/config to
``_meeting(...)``); this router supplies the missing handlers, mirroring ``stop_router.py``:

  1. Resolve the caller (``x-user-id`` the gateway injects after it validates ``x-api-key``).
  2. Validate the platform against the sealed api.v1 ``Platform`` enum (422 like the stop route).
  3. ``find_active`` the user's non-terminal meeting for ``(platform, native_id)`` — 404 if none.
  4. PUBLISH the acts.v1 act on ``bot_commands:meeting:{id}`` (the SAME channel the leave command
     uses — the bot's acts consumer, ``adapters/acts-redis.ts``, tees speak/speak_stop to the
     SpeakController and reconfigure to the caption reconfigurer).

Speak/speak_stop are gated on the meeting having been spawned with ``voice_agent_enabled`` (the
bot ignores them otherwise — ``SpeakController`` checks ``inv.voiceAgentEnabled``), so a speak
against a non-voice bot is a 409 here instead of a silent no-op at the bot.

The redis side is the same ``CommandPublisher`` port as the stop route, so tests drive it with
``InMemoryCommandPublisher`` and prod injects the real ``redis_client.publish``.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from ..bot_spawn.ports import MeetingRepo
from .stop import leave_command_channel as command_channel
from .stop_router import _SUPPORTED_PLATFORMS, CommandPublisher, _resolve_user_id


# ── acts.v1 payload builders (conform to acts.schema.json — additionalProperties:false) ──────────


def speak_act(text: str, voice: Optional[str] = None) -> dict:
    """The acts.v1 ``Speak`` act: ``{"action":"speak","text":...}`` (+ optional ``voice``)."""
    act: dict = {"action": "speak", "text": text}
    if voice:
        act["voice"] = voice
    return act


def speak_stop_act() -> dict:
    """The acts.v1 ``SpeakStop`` act — interrupt any in-progress TTS playback."""
    return {"action": "speak_stop"}


def reconfigure_act(language: Optional[str] = None, task: Optional[str] = None) -> dict:
    """The acts.v1 ``Reconfigure`` act (``language``/``task``; absent keys stripped)."""
    act: dict = {"action": "reconfigure"}
    if language is not None:
        act["language"] = language
    if task is not None:
        act["task"] = task
    return act


# ── router ────────────────────────────────────────────────────────────────────────────────────────


def build_acts_router(repo: MeetingRepo, publisher: CommandPublisher) -> APIRouter:
    """The speak/config routes over the injected ``MeetingRepo`` + ``CommandPublisher`` ports."""
    router = APIRouter()

    async def _resolve_meeting(platform: str, native_meeting_id: str, x_user_id: Optional[str]) -> dict:
        """Shared prologue: identity → platform guard (422) → active-meeting lookup (404)."""
        user_id = _resolve_user_id(x_user_id)
        if platform not in _SUPPORTED_PLATFORMS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unsupported platform '{platform}' — "
                    f"must be one of: {', '.join(sorted(_SUPPORTED_PLATFORMS))}"
                ),
            )
        meeting = await repo.find_active(user_id, platform, native_meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="No active meeting for this bot")
        return meeting

    def _require_voice_agent(meeting: dict) -> None:
        """Speak against a non-voice bot is a 409 — the bot's SpeakController would silently
        ignore the act (``voiceAgentEnabled`` false), so fail loud at the API instead."""
        data = meeting.get("data") if isinstance(meeting.get("data"), dict) else {}
        if not data.get("voice_agent_enabled"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This meeting's bot was not started with voice_agent_enabled — "
                    "speak commands would be ignored. Start the bot with "
                    '{"voice_agent_enabled": true} in POST /bots to use the voice agent.'
                ),
            )

    @router.post("/bots/{platform}/{native_meeting_id}/speak", status_code=202)
    async def speak(
        platform: str,
        native_meeting_id: str,
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ):
        meeting = await _resolve_meeting(platform, native_meeting_id, x_user_id)
        _require_voice_agent(meeting)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be an object")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="'text' is required (non-empty string)")
        voice = body.get("voice")
        if voice is not None and not isinstance(voice, str):
            raise HTTPException(status_code=422, detail="'voice' must be a string")
        meeting_id = meeting["id"]
        await publisher.publish(
            command_channel(meeting_id), json.dumps(speak_act(text, voice))
        )
        return {
            "status": "speaking_requested",
            "meeting_id": meeting_id,
            "native_meeting_id": native_meeting_id,
        }

    @router.delete("/bots/{platform}/{native_meeting_id}/speak", status_code=202)
    async def speak_stop(
        platform: str,
        native_meeting_id: str,
        x_user_id: Optional[str] = Header(default=None),
    ):
        meeting = await _resolve_meeting(platform, native_meeting_id, x_user_id)
        _require_voice_agent(meeting)
        meeting_id = meeting["id"]
        await publisher.publish(command_channel(meeting_id), json.dumps(speak_stop_act()))
        return {
            "status": "speak_stop_requested",
            "meeting_id": meeting_id,
            "native_meeting_id": native_meeting_id,
        }

    @router.put("/bots/{platform}/{native_meeting_id}/config", status_code=202)
    async def update_config(
        platform: str,
        native_meeting_id: str,
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ):
        """Publish an acts.v1 ``reconfigure`` (``language``/``task``) to the bot.

        API parity with the 0.10 line (the gateway already forwards this path): the v0.12 bot
        subscribes to the acts bus but does not act on ``reconfigure`` yet — the act is published
        for forward-compatibility, and the 202 means "accepted for delivery", not "applied".
        """
        meeting = await _resolve_meeting(platform, native_meeting_id, x_user_id)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be an object")
        language = body.get("language")
        task = body.get("task")
        if language is not None and not isinstance(language, str):
            raise HTTPException(status_code=422, detail="'language' must be a string")
        if task is not None and not isinstance(task, str):
            raise HTTPException(status_code=422, detail="'task' must be a string")
        meeting_id = meeting["id"]
        await publisher.publish(
            command_channel(meeting_id), json.dumps(reconfigure_act(language, task))
        )
        return {
            "status": "reconfigure_requested",
            "meeting_id": meeting_id,
            "native_meeting_id": native_meeting_id,
        }

    return router

"""WhatsApp-Versand ueber Twilio - der einzige Ort, der mit Twilio spricht.

WhatsApp erlaubt Nachrichten, die das Unternehmen von sich aus schickt, nur als
von Meta freigegebene Vorlage. Freier Text geht nur innerhalb von 24 Stunden,
nachdem der Empfaenger selbst geschrieben hat - und in der Twilio-Sandbox.

* Mit ``TWILIO_CONTENT_SID`` geht die Nachricht als Vorlage raus. Der Plan steckt
  dann einzeilig in den Variablen, weil WhatsApp Zeilenumbrueche in Variablen
  ablehnt.
* Ohne Vorlage wird der mehrzeilige Text als ``Body`` gesendet.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.config import Settings, get_settings

TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
TIMEOUT_SECONDS = 15


class MessagingNotConfiguredError(RuntimeError):
    """TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN oder TWILIO_WHATSAPP_FROM fehlen."""


class MessageSendError(RuntimeError):
    """Twilio hat die Nachricht nicht angenommen oder war nicht erreichbar."""


@dataclass(frozen=True)
class OutgoingMessage:
    #: Empfaenger in E.164, z.B. "+491711234567"
    to: str
    #: Freier, mehrzeiliger Text
    body: str
    #: Vorlagenvariablen {"1": ..., "2": ..., "3": ...}, jeweils einzeilig
    variables: dict[str, str] = field(default_factory=dict)
    #: "plan" oder "update" - waehlt die Vorlage
    template: str = "plan"


@dataclass(frozen=True)
class SentMessage:
    provider_message_id: str


Sender = Callable[[OutgoingMessage], SentMessage]


def whatsapp_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_whatsapp_from
    )


def _address(number: str) -> str:
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def send_whatsapp(
    message: OutgoingMessage,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> SentMessage:
    settings = settings or get_settings()
    if not whatsapp_configured(settings):
        raise MessagingNotConfiguredError(
            "WhatsApp-Versand ist nicht eingerichtet "
            "(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM)."
        )

    data = {"From": _address(settings.twilio_whatsapp_from), "To": _address(message.to)}
    content_sid = settings.twilio_content_sid
    if message.template == "update" and settings.twilio_update_content_sid:
        content_sid = settings.twilio_update_content_sid
    if content_sid:
        data["ContentSid"] = content_sid
        data["ContentVariables"] = json.dumps(message.variables, ensure_ascii=False)
    else:
        data["Body"] = message.body

    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = client.post(
            TWILIO_MESSAGES_URL.format(sid=settings.twilio_account_sid),
            data=data,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        )
    except httpx.HTTPError as error:
        raise MessageSendError(f"Twilio nicht erreichbar ({error.__class__.__name__})") from error
    finally:
        if own_client:
            client.close()

    if response.status_code >= 400:
        raise MessageSendError(_describe_error(response))
    return SentMessage(provider_message_id=str(response.json().get("sid", "")))


def _describe_error(response: httpx.Response) -> str:
    """Twilio liefert Code und Klartext, z.B. 63016 = ausserhalb des 24-Stunden-Fensters."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("code")
    detail = payload.get("message") or response.reason_phrase
    return f"Twilio {response.status_code}" + (f" (Code {code})" if code else "") + f": {detail}"

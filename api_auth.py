"""Personal InNoHassle sport credentials; never log tokens or credential-bearing errors."""

import base64
import json
from pathlib import Path
import re
import time
from typing import Optional

ACCOUNTS_ORIGIN = "https://api.innohassle.ru/accounts/v0"


class APIAuthError(Exception):
    pass


class APINetworkError(Exception):
    pass


def token_claims(token: str) -> dict:
    """Inspect metadata only. The remote APIs verify signatures and authorize requests."""
    try:
        if not isinstance(token, str) or len(token) > 20000:
            raise ValueError()
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError()
        claims = json.loads(base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True))
        if not isinstance(claims, dict):
            raise ValueError()
        return claims
    except (ValueError, TypeError, UnicodeError):
        raise APIAuthError("Некорректный API-ключ. Подключите API заново командой login-api.") from None


def service_metadata(token: str, now: Optional[float] = None) -> dict:
    claims = token_claims(token)
    scope = claims.get("scope", "")
    match = re.fullmatch(r"sport:([0-9a-fA-F]{24})", scope) if isinstance(scope, str) else None
    if not match:
        raise APIAuthError("Нужен персональный ключ только с правом sport:me. Другие права не принимаются.")
    expires = claims.get("exp")
    if type(expires) not in (float, int) or expires <= (time.time() if now is None else now):
        raise APIAuthError("Персональный API-ключ истёк. Требуется однократное подключение login-api.")
    return {"user_id": match.group(1), "expires": expires}


def load_service_token(path: Path) -> tuple:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        token = raw["access_token"]
    except (OSError, ValueError, KeyError, TypeError):
        raise APIAuthError("Не удалось прочитать API-ключ. Выполните login-api.") from None
    return token, service_metadata(token)


def accounts_get(request, path: str, timeout_seconds: int, params=None, token=None) -> dict:
    from playwright.sync_api import Error as PlaywrightError
    headers = {"Accept": "application/json", "Cache-Control": "no-store"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        response = request.get(ACCOUNTS_ORIGIN + path, params=params, headers=headers,
                               timeout=timeout_seconds * 1000, max_redirects=0)
    except PlaywrightError:
        raise APINetworkError("API InNoHassle временно недоступен. Проверьте интернет/VPN.") from None
    try:
        if response.status in (401, 403) or 300 <= response.status < 400:
            raise APIAuthError("InNoHassle отказал в доступе. Войдите в свой аккаунт через login-api.")
        if response.status == 404:
            raise APIAuthError("InNoHassle не нашёл связанный аккаунт. Проверьте привязку университетского SSO.")
        if response.status != 200:
            raise APINetworkError("API InNoHassle вернул HTTP %s." % response.status)
        if "application/json" not in response.headers.get("content-type", "").lower():
            raise APIAuthError("Вместо ответа API получена страница входа. Выполните login-api.")
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise APIAuthError("Не удалось прочитать ответ API InNoHassle.") from None
    finally:
        response.dispose()


def generate_service_token(request, timeout_seconds: int) -> tuple:
    # This call creates a long-lived credential and requires the user's explicit approval.
    data = accounts_get(request, "/tokens/generate-service-token", timeout_seconds, params={
        "sub": "badminton-roster-local", "scopes": "sport:me", "expiration": "next-14-august",
    })
    return data["access_token"], service_metadata(data["access_token"])


def exchange_sport_token(request, path: Path, timeout_seconds: int) -> str:
    token, metadata = load_service_token(path)
    data = accounts_get(request, "/tokens/generate-sport-token", timeout_seconds,
                        params={"innohassle_id": metadata["user_id"]}, token=token)
    short = data["access_token"]
    claims = token_claims(short)
    expires = claims.get("exp")
    if (claims.get("aud") != "sport" or type(expires) not in (float, int)
            or expires <= time.time() or not isinstance(claims.get("email"), str)):
        raise APIAuthError("InNoHassle вернул неподходящий токен для спортсайта.")
    return short

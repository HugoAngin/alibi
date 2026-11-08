"""HTTP transport and outcome helpers for black-box Alibi probes."""

from __future__ import annotations

import json
import re
from importlib import import_module
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from agents.execution.api import probe_ids

BLOCKED = "blocked"
NOT_BLOCKED = "not_blocked"
NOT_RUN = "not_run"
CAPTURE_LIMIT = 32_768


class ProbeUnavailable(Exception):
    """The probe could not reach its required boundary or establish its setup."""


def _credential_pattern(tokens: dict) -> re.Pattern[str] | None:
    forms = set()
    for token in tokens.values():
        if not isinstance(token, str) or not token:
            continue
        forms.add((token, False))
        for ensure_ascii in (False, True):
            forms.add((json.dumps(token, ensure_ascii=ensure_ascii)[1:-1], False))
        for encode in (quote, quote_plus):
            forms.add((encode(token, safe="", errors="surrogatepass"), True))
    if not forms:
        return None

    alternatives = []
    for value, percent_encoded in sorted(forms, key=lambda form: (-len(form[0]), form)):
        escaped = re.escape(value)
        if percent_encoded:
            escaped = re.sub(r"%[0-9A-F]{2}", lambda match: f"(?i:{match[0]})", escaped)
        alternatives.append(escaped)
    return re.compile("|".join(alternatives))


class ProbeSession:
    def __init__(self, base_urls: dict, tokens: dict):
        self.base_urls = base_urls
        self.tokens = tokens
        self.fixtures = base_urls.get("fixtures", {})
        self.routes = base_urls.get("routes", {})
        self.exchanges: list[dict] = []
        self._credentials = _credential_pattern(tokens)
        self.client = httpx.Client(timeout=8, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self.client.close()

    def require(self, key: str):
        value = self.fixtures.get(key)
        if value is None:
            raise ProbeUnavailable(f"missing fixture: {key}")
        return value

    def route(self, key: str, default: str | None = None, **values) -> str:
        template = self.routes.get(key, default)
        if not isinstance(template, str) or not template.startswith("/") or template.startswith("//"):
            raise ProbeUnavailable(f"missing route: {key}")
        try:
            return template.format(**{name: quote(str(value), safe="") for name, value in values.items()})
        except (AttributeError, IndexError, KeyError, ValueError):
            raise ProbeUnavailable(f"invalid route: {key}") from None

    def _redact(self, text: str) -> str:
        if self._credentials is not None:
            text = self._credentials.sub("[REDACTED]", text)
        return text[:CAPTURE_LIMIT]

    def http(self, method: str, service: str, path: str, actor: str | None = None, *,
             json=None, data=None, content=None, headers=None) -> httpx.Response:
        base = self.base_urls.get(service)
        if not isinstance(base, str):
            raise ProbeUnavailable(f"missing service URL: {service}")
        try:
            parsed = urlsplit(base)
            port = parsed.port
        except ValueError:
            raise ProbeUnavailable(f"invalid service URL: {service}") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or port == 0
                or parsed.username is not None or parsed.password is not None
                or any(char in base for char in ("?", "#", "\\"))
                or any(char.isspace() for char in base)):
            raise ProbeUnavailable(f"invalid service URL: {service}")
        if not path.startswith("/") or path.startswith("//") or "#" in path or "\\" in path:
            raise ProbeUnavailable("HTTP probe paths must be relative to their service origin")
        if actor is not None:
            token = self.tokens.get(actor)
            if not isinstance(token, str) or not token:
                raise ProbeUnavailable(f"missing credential for {actor}")
            if not token.isascii() or not token.isprintable():
                raise ProbeUnavailable(f"invalid credential for {actor}")
        try:
            auth = httpx.Headers(headers)
            if actor is not None:
                auth["Authorization"] = f"Bearer {token}"
            req = self.client.build_request(method, base.rstrip("/") + path, headers=auth,
                                            json=json, data=data, content=content)
        except httpx.InvalidURL:
            raise ProbeUnavailable(f"invalid HTTP probe URL: {service}") from None
        except UnicodeError:
            raise ProbeUnavailable(f"invalid HTTP probe encoding: {service}") from None
        exchange = {"method": self._redact(method), "url": self._redact(str(req.url)),
                    "actor": self._redact(actor) if actor is not None else None,
                    "request_body": self._redact(req.content.decode("utf-8", errors="replace"))}
        self.exchanges.append(exchange)
        try:
            response = self.client.send(req)
        except httpx.HTTPError as exc:
            exchange["error"] = type(exc).__name__
            raise ProbeUnavailable(f"{service} transport failed: {type(exc).__name__}") from None
        exchange.update(status=response.status_code, response_body=self._redact(response.text))
        if "content-disposition" in response.headers:
            exchange["content_disposition"] = self._redact(response.headers["content-disposition"])
        return response

    @staticmethod
    def payload(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def code(cls, response: httpx.Response) -> str | None:
        payload = cls.payload(response)
        for item in (payload, payload.get("error"), payload.get("detail")):
            if isinstance(item, dict) and isinstance(item.get("code"), str):
                return item["code"]
        return None

    @staticmethod
    def require_success(response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise ProbeUnavailable(f"probe setup returned HTTP {response.status_code}")

    @classmethod
    def denial(cls, response: httpx.Response, codes, *, statuses=(400, 401, 403, 404, 409, 410, 422)) -> str:
        if response.status_code in statuses and cls.code(response) in codes:
            return BLOCKED
        if 200 <= response.status_code < 300:
            return NOT_BLOCKED
        return NOT_RUN


def _handler_for(probe_id: str):
    number = int(probe_id.split("-")[1])
    if 25 <= number <= 32:
        from agents.execution import api as execution

        factory = getattr(execution, "verifier_http_handlers", None)
        return factory().get(probe_id) if factory else None

    if number <= 16 or number == 20:
        lane = "consent"
    elif number in {17, 18, 19, 21, 22, 23, 24, 33, 34, 35, 36, 37, 39}:
        lane = "routes"
    else:
        lane = "threats"
    name = f"agents.eval.http_probe_{lane}"
    try:
        module = import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None
    return module.HANDLERS.get(probe_id)


def attempt(probe_id: str, base_urls: dict, tokens: dict) -> dict:
    """Run one catalogue attempt and return its outcome and redacted HTTP exchanges."""
    if probe_id not in probe_ids():
        raise ValueError("unknown catalogue probe")
    ctx = ProbeSession(base_urls, tokens)
    result = {"probe_id": probe_id, "outcome": NOT_RUN, "exchanges": ctx.exchanges}
    try:
        handler = _handler_for(probe_id)
        if handler is None:
            raise ProbeUnavailable("probe handler has not landed")
        outcome = handler(ctx)
        if outcome not in {BLOCKED, NOT_BLOCKED, NOT_RUN}:
            raise ValueError("invalid HTTP probe outcome")
        if outcome != NOT_RUN and not any("status" in exchange for exchange in ctx.exchanges):
            raise ProbeUnavailable("a probe outcome requires an HTTP response")
        result["outcome"] = outcome
    except ProbeUnavailable as exc:
        result["reason"] = ctx._redact(str(exc))
    finally:
        ctx.close()
    return result

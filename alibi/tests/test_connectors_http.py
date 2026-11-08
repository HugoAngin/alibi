"""The shared refusal handlers: every error a route can raise on a loosely typed body reaches
the client as a 4xx ``{code, reason}`` JSON body, never as FastAPI's or Starlette's own shape."""

from fastapi import Body, FastAPI
from fastapi.testclient import TestClient

from alibi.connectors.case_source import MappingError
from alibi.connectors.http import CODES, Refusal, install_refusals


def _app() -> TestClient:
    app = FastAPI()
    install_refusals(app)

    @app.post("/typed")
    def typed(body: dict = Body(...)):
        return {"ids": list(body["ids"])}          # list(1) -> TypeError

    @app.post("/valued")
    def valued(body: dict = Body(...)):
        return {"n": int(body["n"])}               # int("x") -> ValueError

    @app.post("/mapped")
    def mapped():
        raise MappingError("secret field value 12345 does not fit")

    @app.post("/refused")
    def refused():
        raise Refusal("unknown_record", "no record rec-1", extra_key=1)

    return TestClient(app, raise_server_exceptions=False)


def test_type_and_value_errors_from_a_route_are_400_invalid_request_naming_the_type_only():
    client = _app()
    bad_type = client.post("/typed", json={"ids": 1})
    assert bad_type.status_code == 400 and bad_type.headers["content-type"].startswith("application/json")
    assert bad_type.json() == {"code": "invalid_request",
                               "reason": "the request could not be processed (TypeError)"}
    bad_value = client.post("/valued", json={"n": "x"})
    assert bad_value.status_code == 400
    assert bad_value.json() == {"code": "invalid_request",
                                "reason": "the request could not be processed (ValueError)"}
    assert "x" not in bad_value.json()["reason"].replace("ValueError", "")


def test_mapping_errors_refusals_and_starlette_errors_keep_the_shape():
    client = _app()
    mapped = client.post("/mapped")
    assert mapped.status_code == 400 and mapped.json()["code"] == "invalid_request"
    assert "12345" in mapped.json()["reason"]      # a MappingError names fields, so its text is safe
    refused = client.post("/refused")
    assert refused.status_code == CODES["unknown_record"] == 404
    assert refused.json() == {"code": "unknown_record", "reason": "no record rec-1", "extra_key": 1}
    missing = client.get("/nope")
    assert missing.status_code == 404 and missing.json() == {"code": "not_found", "reason": "no such route"}
    wrong = client.get("/typed")
    assert wrong.status_code == 405 and wrong.json()["code"] == "method_not_allowed"
    invalid = client.post("/typed", content=b"not json", headers={"content-type": "application/json"})
    assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_request"

"""The admin routes of both simulated services take one shared credential and loopback only
(execution's BACKEND_SECURITY_REVIEW.md, F1 and F2): no or a wrong bearer is ``401
unauthenticated``, the right bearer from a non-loopback client is ``403 forbidden``, the right
bearer from loopback works, the token never shows up in a response or audit, and
``ALIBI_ADMIN_TOKEN`` wins over the tokens file. F4 (open lookup routes) is documented, not
changed, so the lookup routes and ``GET /`` stay open here."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alibi.bank.service import create_bank_app
from alibi.connectors import keys
from alibi.connectors.clients import BankClient, ClientError, MerchantClient
from alibi.connectors.http import (
    ADMIN,
    CODES,
    TOKEN_ENV,
    is_loopback_client,
    load_tokens,
    resolve_admin_token,
)
from alibi.connectors import merchant as merchant_module
from alibi.connectors.merchant import create_merchant_app

TOKENS = {"bank_a": "tok-a-admin-tests", "bank_b": "tok-b-admin-tests", "feed": "tok-feed-admin-tests"}
ADMIN_TOKEN = "tok-admin-admin-tests"
AS_OF = "2026-11-04T14:00:00Z"
LATER = "2026-11-05T09:00:00Z"
RFC_A = "rfc-0001-01"
PUBLIC_CLIENT = ("203.0.113.9", 44321)    # TEST-NET-3: never loopback

# Every admin route of each service, with a request that is valid once the clock is seeded.
BANK_ADMIN = [("post", "/admin/seed", {"params": {"case": 1, "as_of": AS_OF}}),
              ("post", "/admin/advance", {"params": {"as_of": LATER}})]
MERCHANT_ADMIN = [
    ("post", "/admin/seed", {"params": {"case": 1, "as_of": AS_OF}}),
    ("post", "/admin/advance", {"params": {"as_of": LATER}}),
    ("post", "/admin/retry", {"json": {"event_id": "evt-0001-001"}}),
    ("post", "/admin/correct", {"json": {"record_id": "rec-0001-021", "changes": {"status": "failed"},
                                         "correction_reason": "SYNTHETIC admin test"}}),
    ("get", "/admin/lookups", {}),
    ("get", "/admin/deliveries", {}),
    ("get", "/admin/state", {}),
]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def keystore(tmp_path_factory):
    return keys.connector_keystore(tmp_path_factory.mktemp("admin-keys") / "keys", seed="admin-tests")


def bank_app(keystore, **kwargs):
    kwargs.setdefault("tokens", TOKENS)
    return create_bank_app(keystore=keystore, registry=keystore.registry(), **kwargs)


def merchant_app(keystore, **kwargs):
    return create_merchant_app(None, keystore=keystore, **kwargs)


def seeded(app, token: str, **client_kwargs) -> TestClient:
    client = TestClient(app, **client_kwargs)
    response = client.post("/admin/seed", params={"case": 1, "as_of": AS_OF}, headers=bearer(token))
    assert response.status_code == 200, response.json()
    return client


# --------------------------------------------------------------------------- the credential


def test_apps_take_an_explicit_admin_token_or_the_tokens_entry_or_make_one(keystore):
    explicit_bank = bank_app(keystore, admin_token=ADMIN_TOKEN)
    assert explicit_bank.state.admin_token == ADMIN_TOKEN
    from_dict = bank_app(keystore, tokens={**TOKENS, ADMIN: "tok-from-dict"})
    assert from_dict.state.admin_token == "tok-from-dict"
    # An explicit token wins over the dict's entry; the admin entry is never a bank identity.
    both = bank_app(keystore, tokens={**TOKENS, ADMIN: "tok-from-dict"}, admin_token=ADMIN_TOKEN)
    assert both.state.admin_token == ADMIN_TOKEN
    assert both.state.bank.identity("tok-from-dict") is None and both.state.bank.identity(ADMIN_TOKEN) is None
    made = bank_app(keystore)
    other = bank_app(keystore)
    assert len(made.state.admin_token) >= 32 and made.state.admin_token != other.state.admin_token
    assert merchant_app(keystore, admin_token=ADMIN_TOKEN).state.admin_token == ADMIN_TOKEN
    assert merchant_app(keystore, tokens={ADMIN: "tok-m"}).state.admin_token == "tok-m"
    assert merchant_app(keystore, tokens=TOKENS).state.admin_token not in TOKENS.values()
    assert len(merchant_app(keystore).state.admin_token) >= 32


def test_a_random_admin_token_still_guards_the_routes(keystore):
    """A tokens dict without ``admin`` and no ``admin_token`` does not leave the routes open."""
    bank = TestClient(bank_app(keystore))
    merchant = TestClient(merchant_app(keystore, tokens=TOKENS))
    assert bank.post("/admin/seed", params={"case": 1}).status_code == 401
    assert merchant.post("/admin/seed", params={"case": 1}).status_code == 401
    assert merchant.get("/admin/state").status_code == 401
    for client in (bank, merchant):
        for token in TOKENS.values():
            assert client.post("/admin/seed", params={"case": 1}, headers=bearer(token)).status_code == 401
    # The generated token, read from app.state, works from the TestClient.
    assert bank.post("/admin/seed", params={"case": 1},
                     headers=bearer(bank.app.state.admin_token)).status_code == 200
    assert merchant.post("/admin/seed", params={"case": 1},
                         headers=bearer(merchant.app.state.admin_token)).status_code == 200


# --------------------------------------------------------------------------- 401 and 403


@pytest.mark.parametrize("headers", [{}, bearer("wrong"), bearer(ADMIN_TOKEN + "x"),
                                     bearer(TOKENS["bank_a"]), {"Authorization": f"Basic {ADMIN_TOKEN}"},
                                     {"Authorization": f"Token {ADMIN_TOKEN}"}])
def test_every_admin_route_refuses_a_missing_or_wrong_token(keystore, headers):
    bank = seeded(bank_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    merchant = seeded(merchant_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    for client, routes in ((bank, BANK_ADMIN), (merchant, MERCHANT_ADMIN)):
        for method, path, kwargs in routes:
            response = client.request(method, path, headers=headers, **kwargs)
            assert response.status_code == 401, (path, headers, response.json())
            assert response.json() == {"code": "unauthenticated",
                                       "reason": "a valid admin bearer token is required"}
    # Nothing changed behind the refusals: the clocks still stand at the seed time.
    assert bank.get("/").json()["as_of"] == AS_OF and merchant.get("/").json()["as_of"] == AS_OF
    assert merchant.app.state.merchant.corrections == []


def test_the_refusal_comes_before_body_validation(keystore):
    merchant = seeded(merchant_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    assert merchant.post("/admin/retry", json=[1]).status_code == 401
    assert merchant.post("/admin/correct", json={"nope": 1}).status_code == 401
    assert merchant.post("/admin/advance").status_code == 401        # as_of missing, token missing
    bank = seeded(bank_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    assert bank.post("/admin/seed", params={"case": "one"}).status_code == 401
    assert bank.post("/admin/seed", params={"case": "one"}, headers=bearer(ADMIN_TOKEN)).status_code == 400


def test_the_right_token_from_a_public_client_is_forbidden(keystore):
    """A leaked token through a public proxy still cannot seed or dump state."""
    assert CODES["forbidden"] == 403
    bank_local = seeded(bank_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    merchant_local = seeded(merchant_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    bank = TestClient(bank_local.app, client=PUBLIC_CLIENT)
    merchant = TestClient(merchant_local.app, client=PUBLIC_CLIENT)
    for client, routes in ((bank, BANK_ADMIN), (merchant, MERCHANT_ADMIN)):
        for method, path, kwargs in routes:
            response = client.request(method, path, headers=bearer(ADMIN_TOKEN), **kwargs)
            assert response.status_code == 403, (path, response.json())
            assert response.json() == {"code": "forbidden",
                                       "reason": "admin routes are served to loopback clients only"}
            # And without the token the public client is told nothing more than 401.
            assert client.request(method, path, **kwargs).status_code == 401
    assert bank.get("/").json()["as_of"] == AS_OF and merchant.get("/").json()["as_of"] == AS_OF
    # The non-admin routes are unaffected by the client address.
    assert bank.get("/whoami", headers=bearer(TOKENS["bank_a"])).json()["recipient"] == "bank_a"
    assert merchant.get("/settlements", params={"payout_reference": "PO-0001-01"}).status_code == 200


@pytest.mark.parametrize("client_address", [("127.0.0.1", 5000), ("127.8.8.8", 5000), ("::1", 5000),
                                            ("::ffff:127.0.0.1", 5000), ("testclient", 50000)])
def test_the_right_token_from_loopback_works_on_every_route(keystore, client_address):
    bank = TestClient(bank_app(keystore, admin_token=ADMIN_TOKEN), client=client_address)
    merchant = TestClient(merchant_app(keystore, admin_token=ADMIN_TOKEN), client=client_address)
    for client, routes in ((bank, BANK_ADMIN), (merchant, MERCHANT_ADMIN)):
        for method, path, kwargs in routes:
            response = client.request(method, path, headers=bearer(ADMIN_TOKEN), **kwargs)
            assert response.status_code == 200, (path, client_address, response.json())
    assert bank.get("/").json()["as_of"] == LATER and merchant.get("/").json()["as_of"] == LATER
    assert merchant.get("/admin/state", headers=bearer(ADMIN_TOKEN)).json()["corrections"][0]["record_id"] == "rec-0001-021"


@pytest.mark.parametrize("host, local", [("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
                                         ("testclient", True), ("10.0.0.1", False), ("203.0.113.9", False),
                                         ("::ffff:10.0.0.1", False), ("fe80::1", False), ("localhost", False),
                                         ("", False), (None, False)])
def test_is_loopback_client_rule(host, local):
    class _Client:
        def __init__(self, host):
            self.host = host

    class _Request:
        def __init__(self, host):
            self.client = None if host is None else _Client(host)

    assert is_loopback_client(_Request(host)) is local


# --------------------------------------------------------------------------- never exposed


def test_the_admin_token_never_appears_in_a_response_or_audit(keystore):
    bank = seeded(bank_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    merchant = seeded(merchant_app(keystore, admin_token=ADMIN_TOKEN), ADMIN_TOKEN)
    texts = []
    texts.append(bank.post("/admin/advance", params={"as_of": LATER}, headers=bearer(ADMIN_TOKEN)).text)
    texts.append(bank.post("/rfc", json={"request_id": RFC_A}, headers=bearer(TOKENS["bank_a"])).text)
    for path in ("/", "/whoami", "/rfc", "/inbox", "/audit"):
        texts.append(bank.get(path, headers=bearer(TOKENS["bank_a"])).text)
    texts.append(bank.post("/admin/seed", params={"case": 1}).text)                 # 401 body
    texts.append(bank.post("/admin/seed", params={"case": 1}, headers=bearer("wrong")).text)
    for method, path, kwargs in MERCHANT_ADMIN:
        texts.append(merchant.request(method, path, headers=bearer(ADMIN_TOKEN), **kwargs).text)
    texts.append(merchant.get("/").text)
    texts.append(merchant.get("/records/rec-0001-021").text)
    texts.append(merchant.get("/admin/state").text)                                    # 401 body
    public = TestClient(merchant.app, client=PUBLIC_CLIENT)
    texts.append(public.get("/admin/state", headers=bearer(ADMIN_TOKEN)).text)         # 403 body
    joined = "\n".join(texts)
    assert ADMIN_TOKEN not in joined
    for token in TOKENS.values():
        assert token not in joined
    # The audit names the actions, never the credential.
    audit = bank.get("/audit", headers=bearer(TOKENS["bank_a"])).json()["entries"]
    assert audit and ADMIN_TOKEN not in json.dumps(audit)
    assert "Bearer" not in repr(MerchantClient(TestClient(merchant.app), admin_token=ADMIN_TOKEN))
    assert ADMIN_TOKEN not in repr(BankClient(bank, TOKENS["bank_a"], admin_token=ADMIN_TOKEN))


# --------------------------------------------------------------------------- the file and the env


def test_load_tokens_includes_admin_and_the_environment_wins(tmp_path, monkeypatch):
    for var in TOKEN_ENV.values():
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "tokens.json"
    created = load_tokens(path)
    assert set(created) == {"bank_a", "bank_b", "feed", "admin"} and set(TOKEN_ENV) == set(created)
    assert len(set(created.values())) == 4 and all(len(v) >= 32 for v in created.values())
    assert json.loads(path.read_text(encoding="utf-8")) == created
    monkeypatch.setenv("ALIBI_ADMIN_TOKEN", "env-admin")
    overridden = load_tokens(path)
    assert overridden["admin"] == "env-admin"
    assert {k: v for k, v in overridden.items() if k != "admin"} == {k: v for k, v in created.items() if k != "admin"}
    assert json.loads(path.read_text(encoding="utf-8"))["admin"] == created["admin"]   # the file is untouched
    # A bank built from the loaded tokens takes the environment's admin token.
    keystore = keys.connector_keystore(tmp_path / "keys", seed="admin-env")
    app = create_bank_app(keystore=keystore, registry=keystore.registry(), tokens=overridden)
    client = TestClient(app)
    assert client.post("/admin/seed", params={"case": 1}, headers=bearer(created["admin"])).status_code == 401
    assert client.post("/admin/seed", params={"case": 1}, headers=bearer("env-admin")).status_code == 200


# --------------------------------------------------------------------------- the clients


def test_clients_send_the_admin_bearer_on_admin_calls_only(keystore):
    merchant_client = TestClient(merchant_app(keystore, admin_token=ADMIN_TOKEN))
    bank_client = TestClient(bank_app(keystore, admin_token=ADMIN_TOKEN))
    without = MerchantClient(merchant_client)
    with pytest.raises(ClientError) as info:
        without.seed(1, AS_OF)
    assert (info.value.status, info.value.code) == (401, "unauthenticated")
    merchant = MerchantClient(merchant_client, admin_token=ADMIN_TOKEN)
    assert merchant.seed(1, AS_OF)["case_number"] == 1
    assert merchant.advance(LATER)["as_of"] == LATER
    assert merchant.state()["case_number"] == 1 and len(merchant.deliveries()) == 20
    assert merchant.retry("evt-0001-001")["same_bytes"] is True
    assert merchant.correct("rec-0001-021", {"status": "pending"}, "SYNTHETIC")["revision"] == 2
    assert merchant.lookup(record_id="rec-0001-021")                   # lookups still open (F4)
    assert merchant.identity()["case_number"] == 1
    # The lookup and identity calls carry no Authorization header at all.
    seen = []
    original = merchant_client.get

    def spy(path, **kwargs):
        seen.append((path, kwargs.get("headers") or {}))
        return original(path, **kwargs)

    merchant_client.get = spy
    merchant.lookup(payout_reference="PO-0001-01")
    merchant.identity()
    merchant.state()
    assert [("Authorization" in h) for _, h in seen] == [False, False, True]

    bank_without = BankClient(bank_client, TOKENS["bank_a"], recipient="bank_a")
    with pytest.raises(ClientError) as info:
        bank_without.seed(1, AS_OF)
    assert (info.value.status, info.value.code) == (401, "unauthenticated")
    bank = BankClient(bank_client, TOKENS["bank_a"], recipient="bank_a", admin_token=ADMIN_TOKEN)
    assert bank.seed(1, AS_OF)["case_number"] == 1
    assert bank.advance(LATER) == {"as_of": LATER}
    assert bank.whoami() == {"recipient": "bank_a", "analyst": "analyst_a1"}
    # The admin token is not a recipient token, and the recipient token is not an admin token.
    assert bank_client.get("/whoami", headers=bearer(ADMIN_TOKEN)).status_code == 401
    assert bank_client.post("/admin/advance", params={"as_of": LATER}, headers=bearer(TOKENS["bank_a"])).status_code == 401


# --------------------------------------------------------------------------- the documentation


def test_resolve_admin_token_precedence_is_keyword_then_tokens_entry_then_random(monkeypatch):
    """Regression for the SERVICES.md conventions bullet: the factory precedence is the explicit
    keyword, else ``tokens["admin"]``, else a random token. The environment is read only by
    ``load_tokens``, never by the resolver, so a hand-built tokens dict ignores ``ALIBI_ADMIN_TOKEN``."""
    monkeypatch.setenv("ALIBI_ADMIN_TOKEN", "env-admin-must-not-win")
    assert resolve_admin_token("kw", {"admin": "from-tokens"}) == "kw"
    assert resolve_admin_token(None, {"admin": "from-tokens"}) == "from-tokens"
    assert resolve_admin_token("", {"admin": "from-tokens"}) == "from-tokens"
    generated = {resolve_admin_token(None, dict(TOKENS)), resolve_admin_token(None, None),
                 resolve_admin_token(None, {"admin": ""})}
    assert len(generated) == 3 and all(len(t) >= 32 for t in generated)
    assert "env-admin-must-not-win" not in generated


def test_services_doc_and_merchant_docstring_state_the_admin_credential_correctly():
    """The conventions bullet must describe the real order (keyword, tokens entry, random; the
    environment wins only inside ``load_tokens``), and the merchant module docstring must show
    the factory signature with its ``admin_token`` and ``tokens`` keywords."""
    doc = (Path(merchant_module.__file__).parent / "SERVICES.md").read_text(encoding="utf-8")
    bullet = doc[doc.index("- Admin routes (`/admin/*` on both services)"):doc.index("## Record envelope")]
    assert "`alibi.connectors.http.resolve_admin_token`" in bullet
    order = [bullet.index(piece) for piece in ("`admin_token=` keyword", "`tokens[\"admin\"]`", "random token")]
    assert order == sorted(order), "the bullet lists the precedence in the wrong order"
    assert "`ALIBI_ADMIN_TOKEN` from the" in bullet and "`alibi.connectors.http.load_tokens`" in bullet
    assert "else the factory's `admin_token=` keyword" not in bullet   # the refuted old wording
    signature_line = merchant_module.__doc__.split("builds a FastAPI app")[0]
    assert "admin_token=None" in signature_line and "tokens=None" in signature_line
    import inspect
    params = inspect.signature(create_merchant_app).parameters
    assert params["admin_token"].kind is inspect.Parameter.KEYWORD_ONLY and params["admin_token"].default is None
    assert params["tokens"].kind is inspect.Parameter.KEYWORD_ONLY and params["tokens"].default is None

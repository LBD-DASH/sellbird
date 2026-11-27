"""Free-router brain, Resend sending, DNC, cadence, and free discovery."""

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from sellbird.brain import Brain
from sellbird.cadence import is_south_africa, send_ceiling, step_allowed
from sellbird.config import BrainConfig, ComplianceConfig, SellbirdConfig, load_config
from sellbird.copy_6hn import unexpected_prices
from sellbird.inbound_http import create_app
from sellbird.integrations.mail_provider import MailProvider, SendResult
from sellbird.integrations.resend_mail import ResendProvider
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.replies import classify_reply
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "t.db"))
    await sm.init_db()
    return sm


class _Env:
    instantly_api_key = ""
    resend_api_key = "re_test"


def _cfg(**email):
    class persona:
        name = "Ada"
        email = "ada@example.com"
        company = "Example"
        role = "Guide"

    class mail:
        enabled = True
        provider = "smtp"
        max_daily_sends = email.get("max_daily_sends", 50)
        hard_daily_cap = email.get("hard_daily_cap", 75)
        cadence = email.get("cadence", "")
        send_to_risky = False
        require_approval = email.get("require_approval", True)
        max_bounce_rate = 0.05
        follow_ups = True

    class channels:
        pass

    channels.email = mail

    class cfg:
        pass

    cfg.channels = channels
    cfg.persona = persona
    cfg.writer = type("W", (), {"allowed_prices": email.get("allowed_prices", [])})()
    return cfg


class _Fake(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent = []

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         headers=(), idempotency_key=""):
        self.sent.append(to_email)
        return SendResult(ok=True, message_id="m")

    async def get_replies(self, limit=50):
        return []

    async def test_connection(self):
        return True, "ok"


def _sender(state, cfg, provider=None):
    from sellbird.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=cfg, env=_Env())
    sender.provider = provider or _Fake()
    sender.send_pacing = False
    return sender


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _person(state, email="ada@example.com", status="new"):
    return await state.add_prospect(Prospect(
        first_name="Ada", last_name="Example", title="Coach", company="Example",
        email=email, email_status="verified", email_verified=True, status=status,
    ))


# ── config the box actually loads ──


def test_shipped_config_is_the_free_router_and_resend():
    cfg = load_config()
    assert cfg.brain.provider == "openai"
    assert cfg.channels.email.provider == "resend"
    assert cfg.channels.email.max_daily_sends == 75
    assert cfg.channels.email.hard_daily_cap == 75
    assert cfg.channels.email.cadence == "sa_one_and_day_3_7"
    assert cfg.discovery.provider == "ddg"
    assert cfg.writer.allowed_prices == [29, 59, 149]
    assert cfg.persona.email == "hello@6humanneeds.online"
    assert "PO Box 1673" in cfg.compliance.postal_address
    assert "STOP" in cfg.compliance.opt_out_line
    assert "unsubscribe" in cfg.compliance.opt_out_line.lower()


def test_only_the_three_prices_are_allowed():
    assert unexpected_prices("A $29 Personal report and a $59 Relationship report.", [29, 59, 149]) == []
    assert unexpected_prices("It is $99 a month.", [29, 59, 149]) == ["$99"]
    assert unexpected_prices("$149.00", [29, 59, 149]) == []


# ── free model router ──


@pytest.mark.asyncio
async def test_router_retries_429_then_returns_text_with_no_second_host(state, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:3010/v1")
    monkeypatch.setenv("LLM_API_KEY", "local-test")
    monkeypatch.setenv("LLM_X_AGENT", "Sellbird")
    calls = []

    async def transport(url, json, headers, timeout):
        calls.append((url, headers, json["model"]))
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "slow"}})
        return httpx.Response(200, json={
            "model": "free-test",
            "choices": [{"message": {"content": "hello from the router"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        })

    async def no_sleep(_delay):
        return None

    brain = Brain(state, BrainConfig(provider="openai", model="free-test"))
    brain.llm.transport = transport
    brain.llm.sleep = no_sleep
    assert await brain.think("ping", agent="writer", task="draft") == "hello from the router"
    assert {url for url, _, _ in calls} == {"http://127.0.0.1:3010/v1/chat/completions"}
    assert calls[0][1]["X-Agent"] == "Sellbird"
    assert calls[0][1]["Authorization"] == "Bearer local-test"
    assert await state.get_usage_today("openai") == 1
    assert await brain.preflight() == ""


@pytest.mark.asyncio
async def test_router_failure_is_final_and_does_not_call_claude(state, monkeypatch):
    monkeypatch.setattr("sellbird.brain.asyncio.create_subprocess_exec", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("claude was started")))
    calls = []

    async def transport(url, json, headers, timeout):
        calls.append(url)
        return httpx.Response(503, json={"error": {"message": "down"}})

    async def no_sleep(_delay):
        return None

    brain = Brain(state, BrainConfig(provider="openai", model="free-test"))
    brain.llm.transport = transport
    brain.llm.sleep = no_sleep
    assert await brain.think("ping") == ""
    assert len(calls) == 4
    assert len(set(calls)) == 1


@pytest.mark.asyncio
async def test_missing_model_fails_before_any_request(state, monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    brain = Brain(state, BrainConfig(provider="openai", model=""))
    assert "LLM_MODEL" in await brain.preflight()
    assert await brain.think("ping") == ""


# ── Resend ──


@pytest.mark.asyncio
async def test_resend_send_is_mocked_with_footer_and_idempotency_key():
    seen = {}

    async def transport(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"id": "email_123"})

    cfg = SellbirdConfig(
        persona={"name": "Ada", "company": "Example", "role": "Guide",
                 "email": "ada@example.com", "linkedin": "", "tone": "plain"},
        product={"name": "P", "description": "D", "pricing": "$29", "key_benefits": [],
                 "objection_responses": {}},
        icp={"industries": ["hr"], "company_size": "", "titles": [], "geography": []},
        compliance=ComplianceConfig(
            postal_address="PO Box 1, Example Town",
            opt_out_line="To unsubscribe, reply STOP and I will not email you again.",
        ),
        channels={"email": {"provider": "resend"}},
    )
    provider = ResendProvider(cfg, _Env(), transport=transport)
    result = await provider.send_email(
        "pat@example.net", "a note", "The free 10-minute assessment is open.",
        idempotency_key="sellbird-item-1",
    )
    assert result.ok and result.message_id == "email_123"
    assert seen["url"] == "https://api.resend.com/emails"
    body = seen["json"]
    assert body["from"] == "Ada <ada@example.com>"
    assert body["to"] == ["pat@example.net"]
    assert "PO Box 1, Example Town" in body["text"]
    assert "reply STOP" in body["text"]
    assert body["headers"]["List-Unsubscribe"].startswith("<mailto:ada@example.com")
    assert seen["headers"]["Idempotency-Key"] == "sellbird-item-1"
    assert seen["headers"]["Authorization"] == "Bearer re_test"


# ── DNC, cadence, cap ──


@pytest.mark.asyncio
async def test_stop_writes_dnc_and_blocks_a_later_send(state, monkeypatch):
    monkeypatch.setenv("SENDING_ENABLED", "true")
    from sellbird.agents.handler import Handler
    pid = await _person(state, "pat@example.net", status="contacted")
    handler = Handler(brain=None, state=state, config=_cfg(), env=_Env())
    await handler._process_reply("pat@example.net", "STOP", "reply-1")
    assert await state.is_dnc("pat@example.net") == "stop"
    assert await state.is_suppressed("pat@example.net")
    await state.add_outbox_item(
        prospect_id=pid, to_email="pat@example.net", subject="hello",
        body="Short and fine. Question?", send_at=_now(), status="approved", step=1,
    )
    sender = _sender(state, _cfg())
    await sender._drain_due()
    assert sender.provider.sent == []
    [row] = await state.get_outbox(status="cancelled")
    assert row["error"].startswith("dnc (stop)")


@pytest.mark.asyncio
async def test_unsubscribe_and_bounce_are_dnc(state):
    from sellbird.agents.handler import Handler
    await _person(state, "pat@example.net", status="contacted")
    handler = Handler(brain=None, state=state, config=_cfg(), env=_Env())
    await handler._process_reply("pat@example.net", "Please unsubscribe me", "u1")
    assert await state.is_dnc("pat@example.net") == "unsubscribe"
    other = await _person(state, "bounced@example.net", status="contacted")
    assert classify_reply("mailbox full", from_email="mailer-daemon@example.net").kind == "bounce"
    await handler._suppress("bounced@example.net", "bounced", "bounce")
    assert await state.is_dnc("bounced@example.net") == "bounce"
    assert other


def test_sa_rule_and_follow_up_stop():
    assert is_south_africa("a@firm.co.za")
    assert is_south_africa("Cape Town")
    assert not is_south_africa("a@example.com", "London")
    assert step_allowed(2, "a@firm.co.za", cadence="sa_one_and_day_3_7") == (
        False, "south africa: one email only")
    assert step_allowed(4, "a@example.com", cadence="sa_one_and_day_3_7")[0] is False
    assert step_allowed(3, "a@example.com", "London", cadence="sa_one_and_day_3_7")[0] is True
    assert send_ceiling(type("E", (), {"max_daily_sends": 200, "hard_daily_cap": 75})()) == 75


@pytest.mark.asyncio
async def test_sa_contact_is_staged_once(state):
    pid = await _person(state, "owner@firm.co.za")
    campaign = Campaign(
        id="", name="sa", channel="email", prospect_ids=[pid], status="draft",
        sequence=[
            EmailStep(step=1, subject="hello", body="Short and fine. Question?", delay_days=0),
            EmailStep(step=2, subject="again", body="Another note for you.", delay_days=3),
            EmailStep(step=3, subject="last", body="Leaving this here.", delay_days=4),
        ],
    )
    campaign.id = await state.add_campaign(campaign)
    sender = _sender(state, _cfg(cadence="sa_one_and_day_3_7"))
    await sender._run_native()
    rows = await state.get_outbox()
    assert [r["step"] for r in rows] == [1]


@pytest.mark.asyncio
async def test_follow_ups_are_day_3_and_day_7_then_stop(state):
    pid = await _person(state, "pat@example.net")
    await state.add_company(Company(name="Example", domain="example.net", location="London"))
    campaign = Campaign(
        id="", name="row", channel="email", prospect_ids=[pid], status="draft",
        sequence=[
            EmailStep(step=n, subject="hello", body="Short and fine. Question?", delay_days=1)
            for n in range(1, 5)
        ],
    )
    campaign.id = await state.add_campaign(campaign)
    sender = _sender(state, _cfg(cadence="sa_one_and_day_3_7"))
    await sender._run_native()
    rows = sorted(await state.get_outbox(), key=lambda r: r["step"])
    assert [r["step"] for r in rows] == [1, 2, 3]
    start = datetime.fromisoformat(rows[0]["send_at"])
    assert datetime.fromisoformat(rows[1]["send_at"]) - start == timedelta(days=3)
    assert datetime.fromisoformat(rows[2]["send_at"]) - start == timedelta(days=7)


@pytest.mark.asyncio
async def test_daily_cap_stops_the_second_send(state, monkeypatch):
    monkeypatch.setenv("SENDING_ENABLED", "true")
    pid = await _person(state)
    cfg = _cfg(hard_daily_cap=1, max_daily_sends=50, require_approval=False)
    for n in (1, 2):
        await state.add_outbox_item(
            prospect_id=pid, to_email="ada@example.com", subject="hello",
            body="Short and fine. Question?", send_at=_now(), status="approved",
            step=n, campaign_id=f"c{n}",
        )
    sender = _sender(state, cfg)
    await sender._drain_due()
    assert sender.provider.sent == ["ada@example.com"]
    assert len(await state.get_outbox(status="sent")) == 1
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_sending_flag_off_queues_and_sends_nothing(state, monkeypatch):
    monkeypatch.setenv("SENDING_ENABLED", "false")
    pid = await _person(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="ada@example.com", subject="hello",
        body="Short and fine. Question?", send_at=_now(), status="approved", step=1,
    )
    sender = _sender(state, _cfg())
    await sender._drain_due()
    assert sender.provider.sent == []
    assert len(await state.get_outbox(status="approved")) == 1


# ── discovery ──


def test_audience_queries_cover_four_groups_and_keep_coaches_out_of_sa():
    from sellbird.collectors.audience import scout_queries

    class icp:
        industries = [
            "individuals and couples", "team leaders", "hr",
            "coaches outside South Africa",
        ]
        geography = ["Cape Town", "London"]

    class cfg:
        pass

    cfg.icp = icp
    queries = scout_queries(cfg)
    assert len(queries) == 4
    coach = [q for q in queries if "life coach" in q][0]
    assert "-site:.za" in coach
    assert "Cape Town" not in coach
    assert "London" in coach


@pytest.mark.asyncio
async def test_web_discovery_keeps_real_hosts_and_drops_sa_coaches():
    from sellbird.collectors.discover import DiscoveryQuery
    from sellbird.collectors.web_search import WebSearch

    html = """
    <li class="b_algo"><h2><a href="https://www.bing.com/ck/a">Example Coaching</a></h2>
      <cite>https://example.com/coaching</cite></li>
    <li class="b_algo"><h2><a href="https://www.bing.com/ck/a">Cape Coach</a></h2>
      <cite>https://coach.example.za/</cite></li>
    <li class="b_algo"><h2><a href="https://directory.example/list">Best coaches in London</a></h2>
      <cite>https://directory.example/list</cite></li>
    """

    class Client:
        async def get(self, url, headers=None, timeout=None):
            if "duckduckgo" in url:
                return httpx.Response(202, text="anomaly-modal")
            return httpx.Response(200, text=html)

    found = await WebSearch().fetch(
        Client(), {}, DiscoveryQuery(term="life coach", location="London"))
    assert [b.domain for b in found] == ["example.com"]


@pytest.mark.asyncio
async def test_web_discovery_does_not_invent_when_search_is_blocked():
    from sellbird.collectors.discover import DiscoveryQuery
    from sellbird.collectors.web_search import WebSearch

    class Client:
        async def get(self, url, headers=None, timeout=None):
            return httpx.Response(202, text="captcha")

    with pytest.raises(RuntimeError, match="Nothing was invented"):
        await WebSearch().fetch(Client(), {}, DiscoveryQuery(term="HR consultant", location="London"))


# ── webhook ──


def _sign(secret: str, body: bytes, msg_id="msg_1", ts="1700000000"):
    raw = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    key = base64.b64decode(raw)
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": msg_id,
        "svix-timestamp": ts,
        "svix-signature": "v1," + base64.b64encode(mac).decode(),
        "content-type": "application/json",
    }


@pytest.mark.asyncio
async def test_webhook_stores_a_signed_reply_and_rejects_a_bad_signature(tmp_path, monkeypatch):
    secret = "whsec_" + base64.b64encode(b"supersecretkey12").decode()
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
    sm = StateManager(str(tmp_path / "in.db"))
    await sm.init_db()
    app = create_app(state_factory=lambda: sm)
    body = b'{"type":"email.received","data":{"email_id":"em_1","from":"Pat <pat@example.net>","subject":"Re: hello","text":"Tell me more"}}'
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bad = await client.post("/resend/inbound", content=body, headers={"content-type": "application/json"})
        assert bad.status_code == 401
        ok = await client.post(
            "/resend/inbound", content=body,
            headers=_sign(secret, body, ts=str(int(datetime.now(timezone.utc).timestamp()))),
        )
        assert ok.status_code == 200
    rows = await sm.list_inbound_mail()
    assert len(rows) == 1
    assert rows[0]["from_email"] == "pat@example.net"
    assert rows[0]["provider_id"] == "em_1"

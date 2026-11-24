"""Beslutslagret: frågar Claude och tar emot ett strikt JSON-svar.

Modellen får bara föreslå. Ingenting härifrån rör portföljen — svaret går
vidare till valideraren, som avgör vad som faktiskt blir en affär.

Svarsformatet tvingas fram med structured outputs (`output_config.format`)
i stället för fritext, så att ett felformat svar upptäcks direkt i stället för
att behöva tolkas ur prosa.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

log = logging.getLogger(__name__)

MODEL_DEFAULT = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class DecisionUnavailable(Exception):
    """Modellen gick inte att nå, eller svarade obrukbart. Rundan hoppas över."""


# Schemat svaret tvingas till. Nivåerna är alltid tal; vid sälj sätts de till 0
# och ignoreras av valideraren, som ändå inte kräver nivåer för sälj.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {
            "type": "string",
            "description": "Kort lägesbild, max fem meningar.",
        },
        "proposals": {
            "type": "array",
            "description": "Noll eller fler förslag. Tom lista är ett giltigt svar.",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["buy", "sell"]},
                    "ticker": {"type": "string"},
                    "amount_sek": {"type": "number"},
                    "stop_loss": {"type": "number"},
                    "take_profit": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "action",
                    "ticker",
                    "amount_sek",
                    "stop_loss",
                    "take_profit",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_view", "proposals"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Decision:
    """Modellens råa svar, innan någon validering."""

    market_view: str
    proposals: Sequence
    model: str
    raw_text: str
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "market_view": self.market_view,
            "proposals": list(self.proposals),
            "model": self.model,
            "usage": self.usage,
        }


# ------------------------------------------------------------- underlag


def summarize_history(bars: Sequence) -> dict:
    """Enkel statistik per aktie, räknad på historiken fram till körningen."""
    closes = [bar.close for bar in bars]
    if not closes:
        return {}

    last = closes[-1]

    def change(offset):
        if len(closes) <= offset:
            return None
        return round((last / closes[-1 - offset] - 1.0) * 100.0, 2)

    window = closes[-60:]
    high = max(window)
    low = min(window)

    return {
        "pris": round(last, 4),
        "dag_pct": change(1),
        "vecka_pct": change(5),
        "manad_pct": change(20),
        "kvartal_pct": change(60),
        "hogsta_60d": round(high, 4),
        "lagsta_60d": round(low, 4),
        "fran_hogsta_pct": round((last / high - 1.0) * 100.0, 2) if high else None,
        "over_lagsta_pct": round((last / low - 1.0) * 100.0, 2) if low else None,
    }


@dataclass(frozen=True)
class DecisionContext:
    slot: str
    local_time: str
    cash: float
    total_value: float
    start_capital: float
    index_value: float
    index_level: float
    performance: dict
    positions: Sequence
    market: dict
    names: dict
    recent_trades: Sequence
    previous_rejections: Sequence
    max_position_pct: float
    min_trade_sek: float
    max_proposals: int
    commission_pct: float
    commission_min_sek: float
    spread_pct: float


def build_system_prompt(ctx: DecisionContext) -> str:
    cap = int(round(ctx.max_position_pct * ctx.total_value))
    return (
        "Du förvaltar en simulerad aktieportfölj på Stockholmsbörsen. Inga "
        "riktiga order läggs — allt är på papper. Målet är att slå OMXS30 "
        "över tid.\n\n"
        "Du lämnar förslag. Koden avgör. Varje förslag prövas mot kassa, "
        "positionstak och nivåer innan det bokförs, och ett förslag som bryter "
        "mot en regel kastas utan att du får veta det förrän nästa runda.\n\n"
        "Reglerna koden upprätthåller:\n"
        "- Endast bolag i OMXS30. Ticker måste skrivas exakt som i underlaget.\n"
        "- Köp kräver både stop_loss under och take_profit över dagens pris.\n"
        "- En enskild aktie får efter affären utgöra högst {cap_pct:.0f} % av "
        "portföljens totala värde, just nu cirka {cap} kr.\n"
        "- Minsta affär är {min_trade:.0f} kr. Köp måste rymmas i kassan "
        "inklusive courtage.\n"
        "- Högst {max_proposals} förslag bokförs per körning. Lägg de "
        "viktigaste först.\n"
        "- Vid sälj: sätt stop_loss och take_profit till 0, de används inte.\n"
        "- Courtage är {commission_pct:.2f} % med {commission_min:.0f} kr minimum, "
        "och spreaden {spread_pct:.2f} % äter in på varje affär. Småaffärer "
        "kostar mer än de smakar.\n\n"
        "Stopp och mål kontrolleras automatiskt vid varje körning, så du behöver "
        "inte föreslå sälj för att en nivå är nära.\n\n"
        "Skriv varje motivering på svenska, konkret och i en eller två meningar: "
        "vad i underlaget som talar för affären. Motiveringen sparas och visas "
        "publikt tillsammans med utfallet.\n\n"
        "Att inte göra någonting är ett fullgott svar. Lämna en tom lista när "
        "inget läge är tillräckligt bra."
    ).format(
        cap_pct=ctx.max_position_pct * 100,
        cap=cap,
        min_trade=ctx.min_trade_sek,
        max_proposals=ctx.max_proposals,
        commission_pct=ctx.commission_pct * 100,
        commission_min=ctx.commission_min_sek,
        spread_pct=ctx.spread_pct * 100,
    )


def build_user_prompt(ctx: DecisionContext) -> str:
    lines = []
    lines.append("# Körning")
    lines.append("Tidpunkt: {} (slot: {})".format(ctx.local_time, ctx.slot))
    lines.append("")

    lines.append("# Ställning")
    lines.append("Startkapital: {:.0f} kr".format(ctx.start_capital))
    lines.append("Portföljvärde: {:.2f} kr".format(ctx.total_value))
    lines.append("Varav kassa: {:.2f} kr".format(ctx.cash))
    lines.append(
        "OMXS30 omräknat till startkapitalet: {:.2f} kr (indexnivå {:.2f})".format(
            ctx.index_value, ctx.index_level
        )
    )
    perf = ctx.performance
    lines.append(
        "Sedan start: portfölj {:+.2f} %, index {:+.2f} %, skillnad {:+.2f} "
        "procentenheter".format(
            perf.get("total_return_pct") or 0.0,
            perf.get("index_return_pct") or 0.0,
            perf.get("excess_pct") or 0.0,
        )
    )
    lines.append("")

    lines.append("# Öppna positioner")
    if not ctx.positions:
        lines.append("Inga. Hela kapitalet ligger i kassa.")
    else:
        for pos in ctx.positions:
            lines.append(
                "- {ticker} ({name}): {shares} st, snitt {avg:.2f} kr, "
                "pris {price:.2f} kr, värde {value:.0f} kr ({weight:.1f} % av "
                "portföljen), orealiserat {pnl:+.0f} kr. "
                "Stopp {stop:.2f} / mål {target:.2f}. "
                "Motivering vid köp: {why}".format(**pos)
            )
    lines.append("")

    lines.append("# OMXS30, kurser och utveckling")
    lines.append(
        "ticker | bolag | pris | dag % | vecka % | månad % | kvartal % | "
        "från 60d-högsta %"
    )
    for ticker in sorted(ctx.market):
        stats = ctx.market[ticker]
        if not stats:
            continue
        lines.append(
            "{} | {} | {} | {} | {} | {} | {} | {}".format(
                ticker,
                ctx.names.get(ticker, ticker),
                _fmt(stats.get("pris")),
                _fmt(stats.get("dag_pct")),
                _fmt(stats.get("vecka_pct")),
                _fmt(stats.get("manad_pct")),
                _fmt(stats.get("kvartal_pct")),
                _fmt(stats.get("fran_hogsta_pct")),
            )
        )
    lines.append("")

    if ctx.recent_trades:
        lines.append("# Senaste avslutade affärer")
        for trade in ctx.recent_trades:
            lines.append(
                "- {date} {action} {ticker}: {result}. Orsak: {reason}. {why}".format(
                    **trade
                )
            )
        lines.append("")

    if ctx.previous_rejections:
        lines.append("# Förslag som avvisades förra körningen")
        lines.append(
            "Läs dem som rättelser. Samma misstag ger samma avslag igen."
        )
        for rejection in ctx.previous_rejections:
            lines.append(
                "- {action} {ticker}: {reason} ({detail})".format(**rejection)
            )
        lines.append("")

    lines.append("# Uppgift")
    lines.append(
        "Bedöm läget och lämna noll till {} förslag. Svara enligt schemat."
        .format(ctx.max_proposals)
    )
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "-"
    return "{:.2f}".format(value)


# -------------------------------------------------------------- anropet


def ask(
    ctx: DecisionContext,
    model_cfg: dict,
    client=None,
) -> Decision:
    """Fråga modellen. Allt som går fel blir DecisionUnavailable."""
    import anthropic

    model = str(model_cfg.get("name", MODEL_DEFAULT))
    max_tokens = int(model_cfg.get("max_tokens", 8000))
    effort = str(model_cfg.get("effort", "medium"))
    use_fallback = bool(model_cfg.get("server_side_fallback", True))

    if client is None:
        import os

        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            # Vanligt lokalt, och inget att visa en stacktrace för.
            raise DecisionUnavailable(
                "ANTHROPIC_API_KEY saknas i miljon, inget beslut gar att hamta"
            )
        client = anthropic.Anthropic()

    system = build_system_prompt(ctx)
    user = build_user_prompt(ctx)

    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
        },
    }

    try:
        response = _send(client, request, use_fallback)
    except anthropic.NotFoundError as exc:
        raise DecisionUnavailable("okand modell {}: {}".format(model, exc)) from exc
    except anthropic.RateLimitError as exc:
        raise DecisionUnavailable("hastighetsbegransad: {}".format(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise DecisionUnavailable(
            "API svarade {}: {}".format(exc.status_code, _reason(exc))
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise DecisionUnavailable("nade inte API:et: {}".format(exc)) from exc

    return _parse(response, model)


def _send(client, request: dict, use_fallback: bool):
    """Skicka anropet, med serverside-fallback om den är påslagen.

    Ett 400-svar på anropet med fallback kan bero på beta-flaggan, men lika
    gärna på något helt annat — tomt kreditsaldo, fel modellnamn, ogiltigt
    schema. Därför görs ett vanligt anrop utan flaggan, och orsaken fastställs
    först av hur det går:

    - går det vanliga anropet igenom var det flaggan, och det loggas så
    - avvisas det också ligger felet någon annanstans, och det felet går vidare

    Loggen påstår alltså aldrig att flaggan var orsaken innan det är visat.
    """
    import anthropic

    if not use_fallback:
        return client.messages.create(**request)

    try:
        return client.beta.messages.create(
            betas=[FALLBACK_BETA], fallbacks="default", **request
        )
    except anthropic.BadRequestError as exc:
        log.warning(
            "anropet med serverside-fallback avvisades (%s), provar utan",
            _reason(exc),
        )

    try:
        response = client.messages.create(**request)
    except anthropic.BadRequestError as exc:
        log.warning(
            "anropet utan serverside-fallback avvisades ocksa, sa felet ligger "
            "inte i fallback-flaggan: %s",
            _reason(exc),
        )
        raise

    log.warning(
        "anropet gick igenom utan serverside-fallback: flaggan var orsaken till "
        "avslaget och ar avstangd den har rundan. Satt server_side_fallback: "
        "false i config.yaml om det upprepas."
    )
    return response


def _reason(exc) -> str:
    """Felets egentliga meddelande, utan hela svarsdumpen runt omkring."""
    return getattr(exc, "api_message", None) or str(exc)


def _parse(response, model: str) -> Decision:
    stop_reason = getattr(response, "stop_reason", None)

    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise DecisionUnavailable(
            "modellen avbojde att svara ({})".format(
                getattr(details, "category", None) or "okand orsak"
            )
        )
    if stop_reason == "max_tokens":
        raise DecisionUnavailable("svaret kapades av max_tokens")

    text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = block.text
            break

    if not text.strip():
        raise DecisionUnavailable("tomt svar fran modellen")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecisionUnavailable("svaret var inte giltig JSON: {}".format(exc)) from exc

    if not isinstance(data, dict):
        raise DecisionUnavailable("svaret var inte ett objekt")

    proposals = data.get("proposals", [])
    if not isinstance(proposals, list):
        raise DecisionUnavailable("proposals var inte en lista")

    usage = {}
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            value = getattr(raw_usage, key, None)
            if value is not None:
                usage[key] = value

    return Decision(
        market_view=str(data.get("market_view", "")).strip(),
        proposals=proposals,
        model=model,
        raw_text=text,
        usage=usage,
    )

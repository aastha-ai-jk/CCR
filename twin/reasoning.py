"""Operator-facing "why" text for each advisor recommendation (ADVISORY ONLY).

Every advisory gets a TEMPLATE explanation built from the optimiser's own
numbers. When REASONING_PROVIDER is anthropic (Claude Haiku 5.5, chosen
27 Oct 2026) or openai (gpt-5-nano), a small LLM rewrites those same facts into
plainer sentences in the background; its text replaces the template only if
every number in it matches a fact. The LLM never sees anything but process
numbers and never changes a recommendation.

    .env.secrets
    REASONING_PROVIDER=off | anthropic | openai   (default off)
    REASONING_MODEL=claude-haiku-5-5              (default per provider)
    REASONING_EFFORT=low                          (anthropic: low..max; openai: minimal..)
    REASONING_TIMEOUT_SEC=20
    ANTHROPIC_API_KEY=...                         (provider anthropic)
    OPENAI_API_KEY=...                            (provider openai)

attach(result) sets result["reasoning"] = {"source", "ts", "rows": {param_id: text}}
and, when enabled, starts one background LLM call per changed recommendation.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading

import config  # noqa: F401  (loads .env into the environment)

PROVIDER = os.getenv("REASONING_PROVIDER", "off").strip().lower()
_DEFAULTS = {"anthropic": ("claude-haiku-5-5", "low"), "openai": ("gpt-5-nano", "minimal")}
MODEL = os.getenv("REASONING_MODEL") or _DEFAULTS.get(PROVIDER, ("", ""))[0]
EFFORT = os.getenv("REASONING_EFFORT") or _DEFAULTS.get(PROVIDER, ("", ""))[1]
if PROVIDER == "anthropic" and EFFORT not in ("low", "medium", "high", "xhigh", "max"):
    EFFORT = "low"                       # short text from given facts: least thinking
TIMEOUT = float(os.getenv("REASONING_TIMEOUT_SEC", "20"))

CV = config.CALCINER_TEMP_TAG
# advisor screen row id -> optimiser tag, display name
LEVERS = {                               # the advisor's recommended levers (2026-10-23)
    "calc_coal": ("467RL400_TPH", "calciner coal"),
    "safr": ("261WF080_TPH", "SAFR"),
}
UNITS = {"467RL400_TPH": ("t/h", 2), "LAFR_FT": ("l/h", 0), "K3V10_F1": ("l/h", 0),
         "261WF080_TPH": ("t/h", 2)}
HOLD_FRAC = 0.005                       # a move under 0.5 % of the lever range is "hold"

_state = {"key": None, "rows": None, "ts": None, "busy": False}
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
def facts(r: dict) -> dict:
    """The optimiser's numbers, rounded as shown on screen. Numbers only."""
    base = (r.get("baseline") or {}).get("levers") or {}
    rec = r.get("recommended") or {}
    lim = r.get("lever_limits") or {}
    tb = r.get("target_band") or {}
    pt = ((r.get("predicted") or {}).get("temp") or {}).get("mean")
    eco = r.get("economics") or {}
    levers = {}
    for pid, (tag, name) in LEVERS.items():
        cur, new = base.get(tag), rec.get(tag)
        if cur is None or new is None:
            continue
        unit, dp = UNITS[tag]
        span = (lim.get(tag) or {}).get("max", 0) - (lim.get(tag) or {}).get("min", 0)
        d = new - cur
        levers[pid] = {"name": name, "unit": unit, "current": round(cur, dp),
                       "recommended": round(new, dp), "change": round(d, dp),
                       "hold": bool(span > 0 and abs(d) < HOLD_FRAC * span)}
    failing = [f'{i.get("name")} {i.get("rule")}' for i in (r.get("interlocks") or [])
               if i.get("pass") is False]
    g = r.get("clinker_guard") or {}
    return {
        "horizon_min": r.get("horizon_min") or 5,
        "operating_state": r.get("operating_state"),
        "band_low": tb.get("low"), "band_high": tb.get("high"),
        "predicted_temp_c": None if pt is None else round(pt, 1),
        "in_band": bool(r.get("in_band")),
        "reaches_band_in_min": r.get("reach_min"),
        "tsr_now_pct": _r((eco.get("baseline") or {}).get("tsr_pct"), 1),
        "tsr_recommended_pct": _r((eco.get("recommended") or {}).get("tsr_pct"), 1),
        "tsr_is_estimate": bool((eco.get("recommended") or {}).get("is_estimate")),
        "interlocks_failing": failing,
        "all_limits_met": r.get("all_limits_met") is not False,
        "clinker_guard": g.get("status") if not g.get("active") else f'active: {g.get("direction")}',
        "objective": "least calciner coal, then as much AFR as the limits allow",
        "levers": levers,
    }


def _r(v, dp):
    return None if v is None else round(float(v), dp)


def _num(v, dp):
    return f"{v:.{dp}f}"


def template(f: dict) -> dict:
    """Fixed-wording explanation per screen row, from the facts only."""
    h, st = f["horizon_min"], f.get("operating_state") or "current state"
    band = (f"{f['band_low']:.0f}–{f['band_high']:.0f} °C"
            if f.get("band_low") is not None else "the band")
    pt = f.get("predicted_temp_c")
    if pt is None:
        pos = "no temperature forecast"
    elif f["in_band"]:
        pos = f"inside the {st} band ({band})"
    elif f.get("band_low") is not None and pt < f["band_low"]:
        pos = f"{f['band_low'] - pt:.1f} °C below the {st} band ({band})"
    else:
        pos = f"{pt - f['band_high']:.1f} °C above the {st} band ({band})"
    pred = f"Predicted {pt:.1f} °C in {h} min, {pos}." if pt is not None else ""
    safety = ("Predicted limit risk: " + ", ".join(f["interlocks_failing"]) + "."
              if f["interlocks_failing"] else "All predicted O₂/CO limits are met.")
    limits = "" if f["all_limits_met"] else " No setting meets every limit — this is the closest option."
    tsr = (f"TSR {f['tsr_now_pct']:.1f} → {f['tsr_recommended_pct']:.1f} %"
           + (" (estimated)" if f["tsr_is_estimate"] else "")
           if f.get("tsr_now_pct") is not None and f.get("tsr_recommended_pct") is not None else "")

    rows = {}
    reach = f.get("reaches_band_in_min")
    rows["calc_out"] = (
        (f"With the recommended settings the twin predicts {pt:.1f} °C in {h} min, {pos}. "
         if pt is not None else "No temperature forecast this cycle. ")
        + ("It holds the band. " if f["in_band"] else
           f"It reaches the band in about {reach} min. " if reach else "")
        + safety + limits).strip()
    for pid, lv in f["levers"].items():
        dp = UNITS[LEVERS[pid][0]][1]
        name, u = lv["name"], lv["unit"]
        if lv["hold"]:
            move, aim = f"Hold {name} near {_num(lv['current'], dp)} {u}.", "No change needed on this lever."
        else:
            up = lv["change"] > 0
            move = (f"{'Raise' if up else 'Lower'} {name} from {_num(lv['current'], dp)} to "
                    f"{_num(lv['recommended'], dp)} {u} ({lv['change']:+.{dp}f}).")
            if pid == "calc_coal":
                aim = ("Less calciner coal while the temperature stays on course." if not up
                       else "More heat is needed to keep the calciner on course.")
            else:
                aim = (f"More AFR in place of coal: {tsr}." if up and tsr else
                       f"Less AFR on this stream to keep temperature and limits on course"
                       + (f"; {tsr}." if tsr else "."))
        rows[pid] = f"{move} {aim} {pred} {safety}{limits}".strip()
    return rows


# --------------------------------------------------------------------------- #
_NUM = re.compile(r"(?<![A-Za-z₀-₉])[-+−]?\d[\d,]*(?:\.\d+)?")


def _allowed(f: dict) -> list[float]:
    vals = [f["horizon_min"], 5, 10, 15, 30]
    for k in ("band_low", "band_high", "predicted_temp_c", "reaches_band_in_min",
              "tsr_now_pct", "tsr_recommended_pct"):
        if f.get(k) is not None:
            vals.append(float(f[k]))
    if f.get("predicted_temp_c") is not None:
        for k in ("band_low", "band_high"):
            if f.get(k) is not None:
                vals.append(abs(f["predicted_temp_c"] - f[k]))
    if f.get("tsr_now_pct") is not None and f.get("tsr_recommended_pct") is not None:
        vals.append(abs(f["tsr_recommended_pct"] - f["tsr_now_pct"]))
    for lv in f["levers"].values():
        vals += [lv["current"], lv["recommended"], abs(lv["change"])]
    st = re.findall(r"\d+", f.get("operating_state") or "")
    vals += [float(x) for x in st]
    return vals


def numbers_ok(text: str, f: dict) -> bool:
    """Every number in the text must be a rounding of a fact (no invented numbers)."""
    allowed = _allowed(f)
    for tok in _NUM.findall(text):
        s = tok.replace(",", "").replace("−", "-").lstrip("+")
        try:
            n = abs(float(s))
        except ValueError:
            return False
        dp = len(s.split(".")[1]) if "." in s else 0
        tol = 0.5 * 10 ** -dp + 1e-9
        if not any(abs(n - abs(v)) <= tol for v in allowed):
            return False
    return True


_PROMPT = """You explain a calciner fuel advisor's recommendations to a cement-kiln
CCR operator. Write 1-3 short, plain sentences per row. Use ONLY the facts in
the JSON: never invent a cause, value, trend or equipment state, and only use
numbers that appear in the facts (you may round them). Units as given.
Advisory only - never tell the operator the system changed anything.

Rows: "calc_out" explains the predicted calciner outlet temperature versus the
band; each lever row ("calc_coal", "safr") explains why
that lever is raised, lowered or held, linking it to the objective, the
temperature and the O2/CO limits where relevant.

Reply with a JSON object only: {"calc_out": "...", "calc_coal": "...", ...}
with exactly the row keys present in the facts (calc_out plus every lever).

Facts:
"""


def _rows(text: str, f: dict) -> dict | None:
    """The LLM's JSON reply -> {row: text}, or None unless every row is present
    and every number in it is a fact."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    rows = json.loads(m.group(0) if m else text)
    want = {"calc_out", *f["levers"].keys()}
    if not isinstance(rows, dict) or set(rows) != want:
        return None
    rows = {k: str(v).strip() for k, v in rows.items()}
    if not all(rows.values()) or not all(numbers_ok(v, f) for v in rows.values()):
        return None
    return rows


def _llm(f: dict) -> dict | None:
    """One LLM call; returns {row: text} or None. Never raises."""
    return _llm_anthropic(f) if PROVIDER == "anthropic" else _llm_openai(f)


def _llm_anthropic(f: dict) -> dict | None:
    """One Claude Haiku call (ANTHROPIC_API_KEY)."""
    try:
        import anthropic
    except ImportError:
        _log(f, None, "anthropic-error: SDK not installed", None, None)
        return None
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        _log(f, None, "anthropic-error: no ANTHROPIC_API_KEY in .env.secrets", None, None)
        return None
    try:
        client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": EFFORT},
            messages=[{"role": "user", "content": _PROMPT + json.dumps(f, ensure_ascii=False)}],
        )
        u = resp.usage
        if resp.stop_reason == "refusal":
            _log(f, None, "anthropic-refusal", u.input_tokens, u.output_tokens)
            return None
        rows = _rows("".join(b.text for b in resp.content if b.type == "text"), f)
        _log(f, rows, "anthropic" if rows else "anthropic-rejected", u.input_tokens, u.output_tokens)
        return rows
    except anthropic.AuthenticationError:
        _log(f, None, "anthropic-error: invalid API key", None, None)
    except anthropic.RateLimitError:
        _log(f, None, "anthropic-error: rate limited", None, None)
    except anthropic.APIStatusError as e:
        _log(f, None, f"anthropic-error: HTTP {e.status_code}", None, None)
    except anthropic.APIConnectionError:
        _log(f, None, "anthropic-error: no connection (blocked network?)", None, None)
    except Exception as e:                       # bad JSON etc.: keep the template
        _log(f, None, f"anthropic-error: {type(e).__name__}", None, None)
    return None


def _llm_openai(f: dict) -> dict | None:
    """One gpt-5-nano call (OPENAI_API_KEY)."""
    try:
        from openai import OpenAI
        client = OpenAI(timeout=TIMEOUT, max_retries=1)
        resp = client.responses.create(
            model=MODEL,
            reasoning={"effort": EFFORT},
            input=[{"role": "user", "content": _PROMPT + json.dumps(f, ensure_ascii=False)}],
            max_output_tokens=4000,
        )
        rows = _rows(resp.output_text, f)
        if rows:
            u = getattr(resp, "usage", None)
            _log(f, rows, "openai", getattr(u, "input_tokens", None), getattr(u, "output_tokens", None))
        return rows
    except Exception as e:
        _log(f, None, f"openai-error: {type(e).__name__}", None, None)
        return None


def _log(f, rows, outcome, tin, tout):
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(config.database_url())
        with eng.begin() as con:
            con.execute(text(
                "CREATE TABLE IF NOT EXISTS reasoning_log (ts TEXT, model TEXT, outcome TEXT, "
                "facts TEXT, rows TEXT, input_tokens INTEGER, output_tokens INTEGER)"))
            con.execute(text("INSERT INTO reasoning_log VALUES (:ts,:m,:o,:f,:r,:i,:t)"),
                        {"ts": dt.datetime.now().isoformat(timespec="seconds"), "m": MODEL,
                         "o": outcome, "f": json.dumps(f, ensure_ascii=False),
                         "r": None if rows is None else json.dumps(rows, ensure_ascii=False),
                         "i": tin, "t": tout})
    except Exception:
        pass                                     # logging must never break the advisor


def _key(f: dict) -> str:
    """Recommendation identity: lever moves + band. Same key -> reuse the AI text."""
    return json.dumps({"l": {k: [v["recommended"], v["hold"]] for k, v in f["levers"].items()},
                       "b": [f.get("band_low"), f.get("band_high")]}, sort_keys=True)


def attach(r: dict) -> None:
    """Set r["reasoning"]; start a background LLM rewrite when enabled and new."""
    if not isinstance(r, dict) or "recommended" not in r:
        return
    try:
        f = facts(r)
        tmpl = template(f)
    except Exception:
        return
    ts = r.get("ts")
    r["reasoning"] = {"source": "template", "ts": ts, "rows": tmpl}
    if PROVIDER not in ("anthropic", "openai"):
        return
    k = _key(f)
    with _lock:
        if _state["key"] == k and _state["rows"]:
            r["reasoning"] = {"source": "ai", "model": MODEL, "ts": _state["ts"],
                              "rows": {**tmpl, **_state["rows"]}}
            return
        if _state["busy"]:
            return
        _state["busy"] = True

    def run():
        try:
            rows = _llm(f)
            if rows:
                with _lock:
                    _state.update(key=k, rows=rows, ts=ts)
                r["reasoning"] = {"source": "ai", "model": MODEL, "ts": ts,
                                  "rows": {**tmpl, **rows}}
        finally:
            _state["busy"] = False

    threading.Thread(target=run, daemon=True).start()

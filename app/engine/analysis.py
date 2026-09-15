"""
Analysis engine — the "human eye" candle reading, quantified.

At each M1 candle close we score 8 factors (each -W..+W) and sum them into a
confluence score. |score| >= threshold -> CALL/PUT signal for the NEXT candle.

Factors (mirror of the trader's checklist):
  1. body        strong body continuation (close-open / high-low)
  2. wick        pin-bar rejection at extremes
  3. trend       EMA9 vs EMA21 context
  4. streak      consecutive same-color candles (continuation vs exhaustion)
  5. structure   market structure: HH/HL vs LH/LL from swings
  6. level       proximity to recent support/resistance + reaction there
  7. micro       tick delta, last-10s momentum, close location in range
  8. volatility  expansion (ATR multiple) continuation / squeeze dampener
"""
from typing import List, Dict, Any, Tuple

from ..models import Candle

W_BODY, W_WICK, W_TREND, W_STREAK = 22.0, 26.0, 14.0, 12.0
W_STRUCT, W_LEVEL, W_MICRO, W_VOL = 14.0, 20.0, 16.0, 8.0


# ------------------------------------------------------------------ indicators
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(candles: List[Candle], period: int = 14) -> float:
    if not candles:
        return 0.0
    rngs = [c.range for c in candles[-period:]]
    return sum(rngs) / len(rngs)


def swings(candles: List[Candle], look: int = 3) -> Tuple[List[float], List[float]]:
    """Simple fractal swing highs/lows over the recent history."""
    highs: List[float] = []
    lows: List[float] = []
    data = candles[-80:]
    for i in range(look, len(data) - look):
        window = data[i - look:i + look + 1]
        c = data[i]
        if c.high == max(w.high for w in window):
            highs.append(c.high)
        if c.low == min(w.low for w in window):
            lows.append(c.low)
    return highs[-4:], lows[-4:]


def levels(candles: List[Candle]) -> Dict[str, float]:
    """Nearest resistance/support from recent swing extremes (20-40 candles)."""
    recent = candles[-40:]
    if not recent:
        return {"resistance": None, "support": None, "hi20": None, "lo20": None}
    hi20 = max(c.high for c in recent[-20:]) if len(recent) >= 5 else max(c.high for c in recent)
    lo20 = min(c.low for c in recent[-20:]) if len(recent) >= 5 else min(c.low for c in recent)
    sh, sl = swings(candles)
    resistance = max(sh) if sh else hi20
    support = min(sl) if sl else lo20
    return {"resistance": resistance, "support": support, "hi20": hi20, "lo20": lo20}


def market_structure(candles: List[Candle]) -> int:
    """+1 bullish (HH & HL), -1 bearish (LH & LL), 0 mixed."""
    sh, sl = swings(candles)
    if len(sh) >= 2 and len(sl) >= 2:
        hh = sh[-1] > sh[-2]
        hl = sl[-1] > sl[-2]
        lh = sh[-1] < sh[-2]
        ll = sl[-1] < sl[-2]
        if hh and hl:
            return 1
        if lh and ll:
            return -1
    return 0


# ------------------------------------------------------------------ factors
def compute_factors(closed: Candle, history: List[Candle],
                    live: bool = True) -> Tuple[float, List[Dict[str, Any]]]:
    """Score the just-closed candle. history = closed candles INCLUDING it."""
    factors: List[Dict[str, Any]] = []

    def add(name: str, score: float, text_bn: str):
        factors.append({"name": name, "score": round(score, 1), "text": text_bn})

    d = closed.direction            # +1 green, -1 red
    closes = [c.close for c in history[-60:]]
    a = atr(history)
    lev = levels(history)

    # 1) body strength -----------------------------------------------------
    br = closed.body_ratio
    s = 0.0
    if br >= 0.5:
        s = d * min(br / 0.75, 1.0) * W_BODY
        add("body", s,
            f"শক্তিশালী বডি ({br:.0%}) — {'গ্রিন কন্টিনিউয়েশন' if d > 0 else 'রেড কন্টিনিউয়েশন'}")
    elif br <= 0.25:
        s = -d * 0.35 * W_BODY
        add("body", s, f"দুর্বল/ডোজি বডি ({br:.0%}) — উল্টো দিকের ঝুঁকি")
    factors_core = s

    # 2) wick rejection ----------------------------------------------------
    lw = closed.lower_wick / closed.range
    uw = closed.upper_wick / closed.range
    s = 0.0
    if lw >= 0.55 and uw <= 0.3:
        s = min((lw - 0.45) / 0.4, 1.0) * W_WICK
        add("wick", s, f"নিচের লম্বা উইক ({lw:.0%}) — বাই রিজেকশন/পিন বার")
    elif uw >= 0.55 and lw <= 0.3:
        s = -min((uw - 0.45) / 0.4, 1.0) * W_WICK
        add("wick", s, f"উপরের লম্বা উইক ({uw:.0%}) — সেল রিজেকশন/পিন বার")

    # 3) trend (EMA9 vs EMA21) ---------------------------------------------
    s = 0.0
    if len(closes) >= 21:
        e9 = ema(closes, 9)[-1]
        e21 = ema(closes, 21)[-1]
        diff = e9 - e21
        if a > 0:
            s = (1 if diff > 0 else -1) * min(abs(diff) / (0.12 * a), 1.0) * W_TREND
        add("trend", s, "EMA9>EMA21 — আপট্রেন্ড" if diff > 0 else ("EMA9<EMA21 — ডাউনট্রেন্ড" if diff < 0 else "EMA ফ্ল্যাট"))

    # 4) streak --------------------------------------------------------------
    streak = 0
    for c in reversed(history):
        if c.direction == d:
            streak += 1
        else:
            break
    s = 0.0
    if streak >= 5:
        s = -d * W_STREAK
        add("streak", s, f"{streak}টা একটানা {'গ্রিন' if d > 0 else 'রেড'} — exhaustion ফেড")
    elif streak in (3, 4):
        s = d * 0.55 * W_STREAK
        add("streak", s, f"{streak}টা একটানা — মোমেন্টাম কন্টিনিউ")

    # 5) market structure ----------------------------------------------------
    ms = market_structure(history)
    s = ms * W_STRUCT
    if ms != 0:
        add("structure", s, "HH+HL — বুলিশ স্ট্রাকচার" if ms > 0 else "LH+LL — বিয়ারিশ স্ট্রাকচার")

    # 6) level proximity + reaction -----------------------------------------
    s = 0.0
    if a > 0 and lev["support"] is not None:
        dist_sup = (closed.close - lev["support"]) / a
        dist_res = (lev["resistance"] - closed.close) / a
        if dist_sup <= 0.3 and lw >= 0.35:
            s = min(1.0, (0.35 - dist_sup) / 0.35 + 0.4) * W_LEVEL
            add("level", s, "সাপোর্টে রিজেকশন — বাই জোন")
        elif dist_res <= 0.3 and uw >= 0.35:
            s = -min(1.0, (0.35 - dist_res) / 0.35 + 0.4) * W_LEVEL
            add("level", s, "রেজিস্ট্যান্সে রিজেকশন — সেল জোন")
        elif dist_sup < -0.15 and br >= 0.6:
            s = -W_LEVEL * 0.7
            add("level", s, "সাপোর্ট ব্রেকডাউন — শক্তিশালী ব্রেক")
        elif dist_res < -0.15 and br >= 0.6:
            s = W_LEVEL * 0.7
            add("level", s, "রেজিস্ট্যান্স ব্রেকআউট — শক্তিশালী ব্রেক")

    # 7) micro (tick delta, last-10s, close position) ------------------------
    delta = closed.delta_norm
    l10 = closed.last10_bias if live else 0.0
    cp = closed.close_pos
    micro_raw = delta * 0.45 + l10 * 0.35 + (cp - 0.5) * 2.0 * 0.20
    s = max(-1.0, min(1.0, micro_raw)) * W_MICRO
    parts = []
    if abs(delta) >= 0.3:
        parts.append(f"টিক ডেল্টা {delta:+.0%}")
    if live and abs(l10) >= 0.3:
        parts.append(f"শেষ ১০সে বায়াস {l10:+.0%}")
    if cp >= 0.8 or cp <= 0.2:
        parts.append(f"ক্লোজ রেঞ্জের {'উপরে' if cp >= 0.8 else 'নিচে'} ({cp:.0%})")
    if parts:
        add("micro", s, " + ".join(parts))

    # 8) volatility regime ----------------------------------------------------
    total_wo_vol = sum(f["score"] for f in factors)
    s = 0.0
    if a > 0 and closed.range / a >= 1.6:
        s = d * W_VOL
        add("volatility", s, f"ভোলাটিলিটি এক্সপ্যানশন ({closed.range / a:.1f}x ATR) — কন্টিনিউ")
    squeezed = a > 0 and closed.range / a < 0.55

    total = total_wo_vol + s
    if squeezed:
        total *= 0.55
        add("volatility", 0.0, f"স্কুইজ ({closed.range / a:.2f}x ATR) — সিগন্যাল ড্যাম্পনড")
    return total, factors

"""Settings management with SQLite persistence + env overrides."""
import json
import os
from typing import List, Dict, Any

DEFAULT_PAIRS = ["EURUSD_otc", "USDJPY_otc", "AUDUSD_otc", "GBPUSD_otc"]

KNOWN_PAIRS = [
    "EURUSD_otc", "USDJPY_otc", "AUDUSD_otc", "GBPUSD_otc", "EURJPY_otc",
    "GBPJPY_otc", "USDCAD_otc", "USDCHF_otc", "EURGBP_otc", "NZDUSD_otc",
    "AUDCAD_otc", "AUDJPY_otc", "CADCHF_otc", "CHFJPY_otc", "EURAUD_otc",
    "EURUSD", "USDJPY", "AUDUSD", "GBPUSD",
]

DEFAULTS: Dict[str, Any] = {
    "token": "",
    "is_demo": 1,
    "pairs": DEFAULT_PAIRS,
    "min_confidence": 55,
    "threshold": 35.0,          # absolute confluence score required for a signal
}


class Settings:
    def __init__(self, data: Dict[str, Any] = None):
        self._d: Dict[str, Any] = dict(DEFAULTS)
        if data:
            self._d.update({k: v for k, v in data.items() if k in DEFAULTS})

    # -- persistence ---------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(self._d)

    @classmethod
    def from_json(cls, raw: str) -> "Settings":
        try:
            return cls(json.loads(raw))
        except Exception:
            return cls()

    # -- env overlay (Railway / Docker) --------------------------------------
    @classmethod
    def with_env(cls, base: "Settings") -> "Settings":
        s = cls(base._d)
        env_token = os.environ.get("QX_TOKEN", "").strip()
        if env_token:
            s._d["token"] = env_token
        env_demo = os.environ.get("QX_IS_DEMO")
        if env_demo is not None:
            s._d["is_demo"] = 1 if env_demo in ("1", "true", "True") else 0
        env_pairs = os.environ.get("QX_PAIRS")
        if env_pairs:
            s._d["pairs"] = [p.strip() for p in env_pairs.split(",") if p.strip()]
        return s

    # -- getters / setters ----------------------------------------------------
    @property
    def token(self) -> str:
        return self._d["token"]

    @property
    def is_demo(self) -> int:
        return int(self._d["is_demo"])

    @property
    def pairs(self) -> List[str]:
        return list(self._d["pairs"])

    @property
    def min_confidence(self) -> int:
        return int(self._d["min_confidence"])

    @property
    def threshold(self) -> float:
        return float(self._d["threshold"])

    def update(self, **kw) -> None:
        for k, v in kw.items():
            if k in self._d and v is not None:
                if k == "pairs":
                    v = [p for p in v if isinstance(p, str) and p]
                if k == "token":
                    v = str(v).strip()
                if k == "min_confidence":
                    v = max(50, min(95, int(v)))
                if k == "threshold":
                    v = max(15.0, min(80.0, float(v)))
                self._d[k] = v

    def masked(self) -> Dict[str, Any]:
        t = self._d["token"]
        return {
            "token_set": bool(t),
            "token_masked": (t[:4] + "..." + t[-4:]) if len(t) > 8 else ("***" if t else ""),
            "is_demo": self._d["is_demo"],
            "pairs": self._d["pairs"],
            "min_confidence": self._d["min_confidence"],
            "threshold": self._d["threshold"],
        }

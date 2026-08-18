"""Optional LLM pass -- naming and grouping ONLY.

The LLM is deliberately outside the structural path. Test *steps* come from
graph paths (see generator/path_emitter.py) and are byte-reproducible. This
module only improves human-readable testcase names, and if it fails, times
out, or returns junk, the pipeline keeps the deterministic names and carries
on. It can never change what a test does.
"""
import json
import logging
import os
import re
from typing import Dict, List, Optional

from ..generator.path_emitter import TestCase

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]+")


class LLMEngine:
    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.endpoint = os.getenv("LLM_ENDPOINT")
        self.model = os.getenv("LLM_MODEL_NAME")
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.0"))
        self.timeout = int(config.get("timeout", 120))
        self.api_style = config.get("api_style", "ollama")  # ollama | openai
        self.enabled = bool(config.get("enabled", False) and self.endpoint and self.model)

    @property
    def available(self) -> bool:
        return self.enabled

    def rename_testcases(self, cases: List[TestCase], package: str) -> List[TestCase]:
        """Best-effort friendlier names. Never raises; never alters steps."""
        if not self.enabled:
            logger.info("LLM naming pass disabled; keeping deterministic names.")
            return cases

        try:
            mapping = self._request_names(cases, package)
        except Exception as exc:
            logger.warning(
                "LLM naming pass failed (%s). Keeping deterministic names.", exc
            )
            return cases

        renamed = 0
        used = {c.name for c in cases}
        for case in cases:
            suggestion = mapping.get(case.name)
            if not suggestion:
                continue
            clean = _SAFE_NAME.sub("_", suggestion).strip("_")[:120]
            if clean and clean not in used:
                used.discard(case.name)
                used.add(clean)
                case.name = clean
                renamed += 1
        logger.info("LLM naming pass renamed %d/%d testcase(s).", renamed, len(cases))
        return cases

    # -- transport -------------------------------------------------------
    def _request_names(self, cases: List[TestCase], package: str) -> Dict[str, str]:
        listing = "\n".join(
            f"- {c.name}: {' -> '.join(c.steps[1:])[:300]}" for c in cases[:200]
        )
        prompt = (
            "You are naming automated UI testcases for the Android package "
            f"{package}. Below is a list of testcases, each shown as "
            "'current_name: steps'.\n\n"
            "Return ONLY a JSON object mapping current_name to a better name.\n"
            "Names must be snake_case identifiers, <=60 chars, describing the "
            "feature being verified. Do not change, add, or remove any steps.\n\n"
            f"{listing}\n"
        )

        response_text = self._post(prompt)
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in LLM response")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON was not an object")
        return {str(k): str(v) for k, v in parsed.items()}

    def _post(self, prompt: str) -> str:
        import requests

        if not self.endpoint:
            raise ValueError("LLM_ENDPOINT is not set")
        url: str = self.endpoint

        if self.api_style == "openai":
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "stream": False,
            }
        else:
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": self.temperature},
            }

        logger.info("Dispatching naming request to %s (%s)", url, self.model)
        response = requests.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()

        if self.api_style == "openai":
            return data["choices"][0]["message"]["content"]
        return data.get("response", "")

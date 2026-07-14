import json
import requests
import os

class LLMEngine:
    def __init__(self):
        self.endpoint = os.getenv("LLM_ENDPOINT")
        self.model = os.getenv("LLM_MODEL_NAME")
        self.temperature = float(os.getenv("LLM_TEMPERATURE", 0.0))

    def generate_test_suite(self, click_stream: list, target_package: str) -> str:
        stream_text = "\n".join([f"{i+1}. click {c['type']} \"{c['val']}\"" for i, c in enumerate(click_stream)])
        
        prompt = f"""
You are an expert QA Automation Engineer. Analyze this raw chronological log of an automation crawler navigating a camera application.

RAW LOG:
{stream_text}

CRITICAL RULES:
1. Every test case MUST start with: launch "{target_package}"
2. MUTUALLY EXCLUSIVE FEATURES: Identify independent UI branches (e.g., Flash, Resolution, Filters). Never mix them in a single testcase.
3. ISOLATE SCENARIOS: If the log clicks 'Flash' -> 'Auto', and later 'Flash' -> 'ON', these are TWO separate testcases.
4. Output ONLY the UVTA code blocks. Do not add markdown backticks.

Generate the clean UVTA test suite.
"""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature}
        }
        
        print(f"[*] Dispatching payload to {self.model} at {self.endpoint}...")
        response = requests.post(self.endpoint, json=payload)
        response.raise_for_status()
        
        return response.json().get("response", "").strip()
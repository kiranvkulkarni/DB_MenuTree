# MenuTree AutoQA Agent

A modular, AI-driven automation pipeline designed to crawl Android applications autonomously and convert raw UI interaction logs into structured UVTA (Unified Vision Test Automation) test suites.

## Project Architecture
- **Crawler:** Orchestrates DroidBot to explore the target application using DFS policy.
- **Parser:** Normalizes complex JSON DroidBot logs into actionable click-stream lists.
- **Inference Engine:** Interfaces with local LLM models (e.g., LLaMA, GAUSS) to restructure chronological logs into logical, modular System Test Cases.
- **Generator:** Compiles the refined logic into UVTA-compliant `.uvta` and Gherkin `.feature` files.

## Prerequisites
- **Python 3.10+**
- **Android SDK** (with `adb` in your PATH)
- **Ollama** (for local model hosting)
- **DroidBot** (Install via `pip install git+https://github.com/honeynet/droidbot.git`)

## Setup
1. **Clone the repository:**
   `git clone <repo-url>`
2. **Setup Environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
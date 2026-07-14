# MenuTree AutoQA Agent

An enterprise-grade, AI-driven automation framework designed to autonomously crawl Android applications and synthesize raw, non-linear UI interaction logs into clean, independent, behavior-driven UVTA (Unified Vision Test Automation) test suites.

---

## 1. System Architecture & Core Workflow

The framework operates as a decoupled pipeline composed of four primary isolation layers:

[ Target Device / APK ] 
       |
       v
 +-----------+
 |  Crawler  | --> (Orchestrates DroidBot dynamic traversal via DFS)
 +-----+-----+
       | Writes raw JSON event states
       v
 +-----------+
 |  Parser   | --> (Extracts identifiers: text, content-desc, resourceId)
 +-----+-----+
       | Generates normalized chronological click-streams
       v
 +-----------+
 |LLM Engine | --> (Applies semantic parsing & branch isolation rules)
 +-----+-----+
       | Synthesizes clean DSL code blocks (Temp: 0.0)
       v
 +-----------+
 | Generator | --> (Validates syntax and exports to .uvta & .feature)
 +-----------+


## 2. Environment Setup & Prerequisites

### Infrastructure Requirements
* Python Engine: Version 3.10 or higher.
* Android SDK Engine: Ensure 'adb' (Android Debug Bridge) is compiled, operational, and added globally to your system's environment PATH.
* Compute Host: A local or remote inference engine serving an OpenAI-compliant API endpoint (e.g., Ollama, GAUSS AI). Recommended hardware minimum for 70B+ model inference is a dedicated enterprise GPU architecture (e.g., NVIDIA RTX A6000 or similar with 48GB+ VRAM).

### Installation Steps

1. Clone and Enter Repository Context:
    git clone <your-corporate-repo-url>/MenuTree_AutoQA.git
    cd MenuTree_AutoQA

2. Initialize Isolated Virtual Environment:
    # Windows PowerShell
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1

    # Linux / macOS Shell
    python3 -m venv .venv
    source .venv/bin/activate

3. Install System Dependencies:
    pip install --upgrade pip
    pip install -r requirements.txt

4. Install Custom DroidBot Dynamic Core:
    Because the standard PyPI release lacks recent layout-parsing hooks, install the source distribution directly:
    pip install git+https://github.com/honeynet/droidbot.git

5. Initialize Environment Variables:
    Copy the provided hidden configuration template and customize it to match your target execution landscape:
    cp .env.example .env

---

## 3. Operational Execution Guide

The system execution profile is fully managed via main.py.

### Step 1: Automated Target Profiling (Optional / Pre-Run)
Ensure your target device is verified and responsive via ADB:
    adb devices
*Note: Ensure the target device serial matches the value specified inside your active .env file.*

### Step 2: Running the Full Pipeline
To execute the exploration crawler, parse the state maps, run the local LLM sanitization, and output files in a single sweep:
    python main.py

### Step 3: Running Pure Code Synthesis (Post-Crawl Analysis)
If you have already collected raw DroidBot trace data and want to re-run the synthesis layers using an upgraded model or structural prompt profile, comment out the crawler.start_exploration() block within main.py and execute:
    python main.py
*This processes the localized ./droidbot_out/events data instantaneously without putting overhead on the physical device.*

---

## 4. Concrete Examples & Transformation Mappings

### A. What the Crawler Sees (Raw Data Entry Fragment)
During execution, DroidBot dumps chronological logs containing chaotic transitions where different feature sets bleed together:
    1. click text "Flash"
    2. click text "Auto"
    3. click desc "Resolution"
    4. click text "Filters"
    5. click desc "Original"

### B. What the System Outputs (com.sec.android.app.camera_suite.uvta)
The inference layer dynamically reconstructs these entries by grouping contextually dependent UI elements into single test files while cleanly isolating distinct features:

    TESTCASE: Verify_Flash_Auto
    launch "com.sec.android.app.camera"
    click text "Flash"
    verify text "Flash" exists timeout 2.0
    click text "Auto"
    verify text "Auto" exists timeout 2.0

    TESTCASE: Verify_Resolution_Access
    launch "com.sec.android.app.camera"
    click desc "Resolution"
    verify desc "Resolution" exists timeout 2.0

    TESTCASE: Verify_Filter_Original
    launch "com.sec.android.app.camera"
    click text "Filters"
    verify text "Filters" exists timeout 2.0
    click desc "Original"
    verify desc "Original" exists timeout 2.0

---

## 5. Enterprise Troubleshooting Manual

### Pipeline Phase: Crawler & Connection Errors
* Symptom: [!] DroidBot execution interrupted or failed or device hangs indefinitely.
  * Root Cause 1: The device lost ADB authorization or entered a deep sleep state.
  * Mitigation 1: Run 'adb devicestate'. If unauthorized, disconnect the USB connection, toggle "USB Debugging" OFF and ON in Developer Options, and accept the permanent RSA fingerprint prompt.
  * Root Cause 2: Android system permissions blocking background accessibility interactions.
  * Mitigation 2: Manually open the device settings, navigate to Accessibility -> Installed Apps/Services, and verify that the DroidBot accessibility helper has full operational permissions.

### Pipeline Phase: Parser & Selector Missing Errors
* Symptom: [!] No UI events found to process. Exiting.
  * Root Cause: DroidBot generated raw click coordinate pairs ([x, y]) because the target APK features custom rendering canvases (like Flutter or Unity) that do not expose structural view node hierarchies.
  * Mitigation: Ensure fallback_to_class: true is enabled inside config.yaml. If specific buttons are still missing, update your target selector strategy in event_extractor.py to capture resource strings or layout boundaries.

### Pipeline Phase: LLM Context Blending & Formatting Anomalies
* Symptom: A single generated TESTCASE block includes multiple unrelated features concatenated together into huge strings.
  * Root Cause 1: The LLM's operational context is bleeding out due to a loose parameter setting or structural model limitations (common in architectures below 10B parameters).
  * Mitigation 1: Force LLM_TEMPERATURE=0.0 inside your .env file to suppress structural creativity. 
  * Mitigation 2: If deploying on high-end hardware, transition to a dedicated 70B parameter or larger variant (such as specialized corporate GAUSS models or LLaMA-3-70B). High-parameter architectures possess the structural logic required to strictly adhere to the system's "Mutually Exclusive Features" rule.
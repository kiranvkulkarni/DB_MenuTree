import os
import yaml
from dotenv import load_dotenv

from src.crawler.droidbot_runner import DroidBotRunner
from src.parser.event_extractor import EventExtractor
from src.llm.inference import LLMEngine
from src.generator.uvta_writer import UVTAWriter

def load_config() -> dict:
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def main():
    # 1. Initialize Environment
    load_dotenv()
    config = load_config()
    
    serial = os.getenv("DEFAULT_SERIAL")
    package = os.getenv("TARGET_PACKAGE")

    print("========================================")
    print("   MenuTree AutoQA Agent Initialized    ")
    print("========================================")

    # 2. Extract Data (Assuming DroidBot already ran, or you can trigger it here)
    # crawler = DroidBotRunner(serial, package, config['crawler'])
    # crawler.start_exploration()
    
    parser = EventExtractor(config['parser'])
    raw_clicks = parser.extract_click_stream()
    
    if not raw_clicks:
        print("[!] No UI events found to process. Exiting.")
        return
        
    print(f"[*] Parsed {len(raw_clicks)} raw UI interactions.")

    # 3. AI Inference
    llm = LLMEngine()
    try:
        suite_content = llm.generate_test_suite(raw_clicks, package)
    except Exception as e:
        print(f"[!] LLM Inference failed: {e}")
        return

    # 4. Suite Generation
    writer = UVTAWriter(config['generator'])
    writer.write_suite(suite_content)

if __name__ == "__main__":
    main()
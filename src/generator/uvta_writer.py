from pathlib import Path

class UVTAWriter:
    def __init__(self, config: dict):
        self.output_file = Path(config['uvta_output'])

    def write_suite(self, llm_output: str):
        # Sanitize any residual markdown from the LLM response
        clean_content = llm_output.replace("```uvta", "").replace("```", "").strip()
        
        with open(self.output_file, "w", encoding="utf-8") as f:
            f.write(clean_content + "\n")
            
        print(f"[*] Enterprise AutoQA suite generated: {self.output_file.absolute()}")
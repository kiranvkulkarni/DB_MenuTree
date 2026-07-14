import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional

class EventExtractor:
    def __init__(self, config: dict):
        self.events_dir = Path(config['events_dir'])
        self.fallback = config['fallback_to_class']

    def _get_best_identifier(self, view_dict: dict) -> Tuple[Optional[str], Optional[str]]:
        if not view_dict:
            return None, None
            
        for key in ['text', 'content_description', 'resource_id']:
            val = view_dict.get(key)
            if val and val.strip():
                clean_val = val.split('/')[-1] if '/' in val else val
                attr_type = 'desc' if key == 'content_description' else ('resourceId' if key == 'resource_id' else 'text')
                return attr_type, clean_val.strip()
        
        if self.fallback and view_dict.get('class'):
            return 'className', view_dict.get('class').split('.')[-1]
            
        return None, None

    def extract_click_stream(self) -> List[Dict]:
        if not self.events_dir.exists():
            print("[!] Events directory not found. Did DroidBot run successfully?")
            return []

        raw_clicks = []
        for file_path in sorted(self.events_dir.glob("event_*.json")):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    raw_data = json.load(f)
                
                event_data = raw_data.get('event', raw_data)
                if event_data.get('event_type') == 'touch':
                    sel_type, sel_val = self._get_best_identifier(event_data.get('view', {}))
                    if sel_type and sel_val:
                        raw_clicks.append({"type": sel_type, "val": sel_val})
            except Exception:
                continue
                
        return raw_clicks
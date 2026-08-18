"""Write and validate the generated UVTA suite."""
import logging
import re
from pathlib import Path
from typing import List

from .path_emitter import TestCase

logger = logging.getLogger(__name__)

_TESTCASE_LINE = re.compile(r"^TESTCASE:\s+\S+")
_LAUNCH_LINE = re.compile(r'^launch\s+"[^"]+"$')


class SuiteValidationError(Exception):
    pass


class UVTAWriter:
    def __init__(self, config: dict):
        self.output_file = Path(config["uvta_output"])
        self.strict = config.get("validate", True)

    def write_suite(self, cases: List[TestCase], header_comment: str = "") -> Path:
        if not cases:
            raise SuiteValidationError(
                "Refusing to write an empty suite -- an empty gate always passes."
            )

        blocks = [case.render() for case in cases]
        content = "\n\n".join(blocks) + "\n"
        if header_comment:
            content = header_comment.rstrip() + "\n\n" + content

        self.validate(cases)

        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.output_file.write_text(content, encoding="utf-8")
        logger.info(
            "Wrote %d testcase(s) to %s", len(cases), self.output_file.resolve()
        )
        return self.output_file

    def validate(self, cases: List[TestCase]) -> None:
        """Structural check. A malformed suite must fail loudly, not ship."""
        problems: List[str] = []
        seen = set()

        for case in cases:
            rendered = case.render().splitlines()
            if not rendered or not _TESTCASE_LINE.match(rendered[0]):
                problems.append(f"'{case.name}': missing or malformed TESTCASE header")
                continue
            if len(rendered) < 2 or not _LAUNCH_LINE.match(rendered[1]):
                problems.append(f"'{case.name}': first step is not a launch")
            if len(rendered) < 3:
                problems.append(f"'{case.name}': has a launch but no actions")
            if case.name in seen:
                problems.append(f"'{case.name}': duplicate testcase name")
            seen.add(case.name)

        if problems:
            message = f"{len(problems)} suite validation problem(s):\n  - " + "\n  - ".join(
                problems[:20]
            )
            if self.strict:
                raise SuiteValidationError(message)
            logger.warning(message)

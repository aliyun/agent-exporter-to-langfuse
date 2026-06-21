from dataclasses import dataclass, field

MARKER = "##e2e##"


@dataclass
class Progress:
    phase: str = "pending"
    passed: int = 0
    failed: int = 0
    total: int = 0
    current_test: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "current_test": self.current_test,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }


class ProgressParser:
    def __init__(self) -> None:
        self.progress = Progress()

    def parse_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped.startswith(MARKER):
            return

        rest = stripped[len(MARKER):].strip()
        if not rest:
            return

        parts = rest.split(None, 1)
        command = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

        try:
            if command == "suite":
                self._handle_suite(arg)
            elif command == "case":
                self._handle_case(arg)
            elif command == "pass":
                self._handle_pass(arg)
            elif command == "fail":
                self._handle_fail(arg)
            elif command == "summary":
                self._handle_summary(arg)
        except Exception:
            pass

    def _handle_suite(self, arg: str) -> None:
        parts = arg.rsplit(None, 1)
        if len(parts) == 2:
            try:
                self.progress.total = int(parts[1])
            except ValueError:
                pass
        self.progress.phase = "testing"

    def _handle_case(self, arg: str) -> None:
        self.progress.current_test = arg
        self.progress.phase = "testing"

    def _handle_pass(self, arg: str) -> None:
        self.progress.passed += 1
        self.progress.phase = "testing"

    def _handle_fail(self, arg: str) -> None:
        self.progress.failed += 1
        self.progress.phase = "testing"

    def _handle_summary(self, arg: str) -> None:
        parts = arg.split()
        if len(parts) >= 3:
            try:
                self.progress.passed = int(parts[0])
                self.progress.failed = int(parts[1])
                self.progress.total = int(parts[2])
            except ValueError:
                pass
        self.progress.phase = "completed"

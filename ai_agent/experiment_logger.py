import csv
from datetime import datetime
from pathlib import Path


class ExperimentLogger:
    LOG_FILE = "thesis_metrics.csv"
    HEADER = [
        "Timestamp",
        "File Analizzato",
        "Esito LLM",
        "Stato Test",
        "Passati",
        "Falliti",
        "Azione Utente",
        "Tempo API (s)",
        "Tempo Sessione (s)",
    ]

    @classmethod
    def initialize(cls, repo_root: Path) -> None:
        log_path = repo_root / cls.LOG_FILE
        try:
            if not log_path.exists() or log_path.stat().st_size == 0:
                cls._write_header(log_path)
                return

            rows = log_path.read_text(encoding="utf-8-sig").splitlines()
            first_non_empty = next((row for row in rows if row.strip()), "")
            if first_non_empty.startswith("Timestamp,"):
                return

            original = log_path.read_text(encoding="utf-8-sig")
            with log_path.open(mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(cls.HEADER)
                if original:
                    f.write(original)
        except Exception:
            pass

    @classmethod
    def _write_header(cls, log_path: Path) -> None:
        with log_path.open(mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cls.HEADER)

    @classmethod
    def log_run(
        cls,
        repo_root: Path,
        target_file: Path,
        llm_status: str,
        test_status: str,
        passed: str,
        failed: str,
        human_action: str,
        api_time: float,
        session_time: float,
    ) -> None:
        try:
            cls.initialize(repo_root)
            with (repo_root / cls.LOG_FILE).open(mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        target_file.name,
                        llm_status,
                        test_status,
                        passed,
                        failed,
                        human_action,
                        round(api_time, 2),
                        round(session_time, 2),
                    ]
                )
        except Exception:
            pass

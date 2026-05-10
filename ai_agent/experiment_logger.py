import csv
from datetime import datetime
from pathlib import Path


class ExperimentLogger:
    LOG_FILE = "thesis_metrics.csv"
    HEADER = [
        "Timestamp",
        "File Analizzato",
        "Linguaggio",
        "Esito LLM",
        "Stato Test",
        "Passati",
        "Falliti",
        "Azione Utente",
        "Tempo API (s)",
        "Tempo Sessione (s)",
        "Iterazioni",
    ]

    @classmethod
    def initialize(cls, repo_root: Path) -> None:
        log_path = repo_root / cls.LOG_FILE
        try:
            if not log_path.exists() or log_path.stat().st_size == 0:
                cls._write_header(log_path)
                return

            with log_path.open(mode="r", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))

            first_non_empty = next((row for row in rows if any(cell.strip() for cell in row)), [])
            if first_non_empty == cls.HEADER:
                return

            if first_non_empty and first_non_empty[0] == "Timestamp":
                if "Linguaggio" not in first_non_empty or "Iterazioni" not in first_non_empty:
                    cls._migrate_header(log_path, rows, first_non_empty)
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
    def _migrate_header(cls, log_path: Path, rows: list[list[str]], old_header: list[str]) -> None:
        old_index = {name: idx for idx, name in enumerate(old_header)}
        migrated_rows = []

        for row in rows[1:]:
            if not any(cell.strip() for cell in row):
                continue

            new_row = []
            for column in cls.HEADER:
                if column in old_index and old_index[column] < len(row):
                    new_row.append(row[old_index[column]])
                elif column == "Linguaggio":
                    new_row.append("")
                elif column == "Iterazioni":
                    new_row.append("1")
                else:
                    new_row.append("")
            migrated_rows.append(new_row)

        with log_path.open(mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cls.HEADER)
            writer.writerows(migrated_rows)

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
        language: str = "",
        iterations: int = 1,
    ) -> None:
        try:
            cls.initialize(repo_root)
            with (repo_root / cls.LOG_FILE).open(mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        target_file.name,
                        language,
                        llm_status,
                        test_status,
                        passed,
                        failed,
                        human_action,
                        round(api_time, 2),
                        round(session_time, 2),
                        iterations,
                    ]
                )
        except Exception:
            pass

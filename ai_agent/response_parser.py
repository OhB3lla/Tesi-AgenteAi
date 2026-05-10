import ast
import json
from typing import Tuple


class ResponseParserMixin:
    def _handle_ai_response(self, response_text: str) -> Tuple[str, str]:
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            return "failed", "Risposta AI non è JSON valido: " + str(exc)

        required_fields = ("has_bug", "analysis", "test_code", "test_file_name", "run_command")
        for field in required_fields:
            if field not in data:
                return "failed", f"JSON incompleto: campo mancante: {field}"

        if not isinstance(data["has_bug"], bool):
            return "failed", "JSON incoerente: has_bug deve essere true o false."

        fixed_code = data.get("fixed_code")
        test_code = data["test_code"]
        cmd = data["run_command"]
        t_file = data["test_file_name"]

        if data["has_bug"] and not str(fixed_code or "").strip():
            return (
                "failed",
                "JSON incoerente: has_bug è true ma fixed_code è assente. Rigenera la risposta.",
            )

        if not str(test_code or "").strip():
            return "failed", "JSON incoerente: test_code è vuoto."

        if any(operator in str(cmd) for operator in ("&&", "||", ";", "|")):
            return "failed", "run_command contiene operatori shell non consentiti."

        no_bug = not data["has_bug"]
        candidate_code = str(fixed_code).strip() if fixed_code and not no_bug else ""
        if candidate_code:
            state_error = self._find_removed_instance_state(candidate_code)
            if state_error:
                return "failed", state_error
            with self._lock:
                self.fixed_code = candidate_code

        if candidate_code:
            run_result, err_log = self._run_tests_against_candidate(
                response_text,
                cmd,
                t_file,
                candidate_code,
            )
        else:
            run_result, err_log = self._run_tests(response_text, cmd, t_file)

        if no_bug:
            if run_result == "passed":
                return "clean", ""
            return "failed", err_log

        if candidate_code:
            if run_result == "passed":
                with self._lock:
                    self.fixed_code = candidate_code
                return "bug", ""
            with self._lock:
                self.fixed_code = ""
            details = err_log or self.test_output_log or "La patch proposta non ha superato la validazione locale."
            return "failed", details

        if run_result in ("passed", "structured_failed"):
            return "bug", ""

        return "failed", err_log

    def _find_removed_instance_state(self, candidate_code: str) -> str:
        if not self.target_file or self.target_file.suffix.lower() != ".py":
            return ""

        try:
            original_tree = ast.parse(self.target_file.read_text(encoding="utf-8-sig"))
            candidate_tree = ast.parse(candidate_code.lstrip("\ufeff"))
        except (OSError, SyntaxError):
            return ""

        original_attrs = self._collect_self_attrs(original_tree)
        candidate_attrs = self._collect_self_attrs(candidate_tree)
        removed = original_attrs - candidate_attrs
        if not removed:
            return ""

        names = ", ".join(sorted(removed))
        return (
            "Patch non valida: rimuove attributi di stato esistenti "
            f"({names}). Mantieni lo stato originale e proponi una correzione minima."
        )

    @staticmethod
    def _collect_self_attrs(tree: ast.AST) -> set[str]:
        attrs: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                attrs.add(node.attr)
        return attrs
    def _run_tests_against_candidate(
        self,
        response_text: str,
        cmd: str,
        t_file: str,
        candidate_code: str,
    ) -> Tuple[str, str]:
        if not self.target_file:
            return self._run_tests(response_text, cmd, t_file)

        try:
            original_bytes = self.target_file.read_bytes()
        except OSError as exc:
            return "failed", f"Impossibile leggere il file originale prima della validazione: {exc}"

        try:
            self.target_file.write_text(candidate_code, encoding="utf-8")
            return self._run_tests(response_text, cmd, t_file)
        except OSError as exc:
            return "failed", f"Impossibile validare temporaneamente la patch: {exc}"
        finally:
            try:
                self.target_file.write_bytes(original_bytes)
            except OSError:
                pass


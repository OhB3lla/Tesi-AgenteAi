import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from .config import TEST_TIMEOUT_SECONDS
from .process_utils import run_process


class TestRunnerMixin:
    def _extract_test_block(self, response_text: str) -> Optional[str]:
        try:
            data = json.loads(response_text)
            test_code = data.get("test_code")
            if isinstance(test_code, str):
                return self._strip_code_fence(test_code)
        except json.JSONDecodeError:
            pass

        unit_heading = re.search(r"##\s*unit\s+test.*?$", response_text, re.IGNORECASE | re.MULTILINE)
        if unit_heading:
            block = re.search(r"```[^\n]*\n(.*?)\n```", response_text[unit_heading.end():], re.DOTALL)
            if block:
                return block.group(1).strip()

        blocks = [
            (m.start(), m.group(1).strip())
            for m in re.finditer(r"```[^\n]*\n(.*?)\n```", response_text, re.DOTALL)
        ]
        if not blocks:
            return None

        meta_match = re.search(r"^TEST_FILE_NAME:", response_text, re.IGNORECASE | re.MULTILINE)
        if meta_match:
            before_meta = [item for item in blocks if item[0] < meta_match.start()]
            if before_meta:
                return before_meta[-1][1]

        return blocks[-1][1]

    def _run_tests(self, response_text: str, cmd: str, t_file_name: str) -> Tuple[str, str]:
        test_code = self._extract_test_block(response_text)
        if not test_code:
            return self._record_test_failure("Codice del test vuoto.")

        test_code = re.sub(
            r"(?im)^(DEPENDENCIES|TEST_FILE_NAME|RUN_COMMAND):.*$",
            "",
            test_code,
        ).strip()
        if not test_code:
            return self._record_test_failure("Codice del test vuoto.")

        with self._lock:
            self.generated_test_code = test_code

        if not self.target_file:
            return self._record_test_failure("File target non impostato.")

        target_ext = self.target_file.suffix.lower()
        if target_ext == ".py":
            test_code = self._prepare_python_test_code_for_execution(test_code)
            if isinstance(test_code, tuple):
                return test_code
        elif target_ext == ".java":
            java_error = self._validate_java_test_structure(test_code)
            if java_error:
                return self._record_test_failure(java_error)

        guard_error = self._find_unreliable_test_code(test_code, target_ext)
        if guard_error:
            return self._record_test_failure(guard_error)

        safe_name = self._safe_test_file_name(t_file_name)

        with self._lock:
            self.generated_test_code = test_code

        if target_ext == ".java":
            return self._run_java_test(test_code, safe_name)
        if target_ext == ".c":
            return self._run_c_test(test_code, safe_name)
        if target_ext == ".cpp":
            return self._run_cpp_test(test_code, safe_name)
        if target_ext == ".cs":
            return self._run_csharp_test(test_code, safe_name)

        test_path = self._make_temp_test_path(safe_name)
        try:
            test_path.write_text(test_code, encoding="utf-8")
        except Exception as exc:
            return self._record_test_failure(f"Impossibile scrivere il test: {exc}")

        exec_args, setup_error, cleanup_paths = self._build_test_command(cmd, test_path, safe_name)
        if setup_error:
            self._cleanup_test_file(test_path)
            return self._record_test_failure(setup_error)

        try:
            res = run_process(exec_args, cwd=self.repo_root, timeout=TEST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            msg = f"Timeout: il test ha superato {TEST_TIMEOUT_SECONDS} secondi."
            self._cleanup_test_file(test_path)
            return self._record_test_failure(msg)
        except Exception as exc:
            msg = f"Esecuzione test non riuscita: {exc}"
            self._cleanup_test_file(test_path)
            return self._record_test_failure(msg)
        finally:
            for path in cleanup_paths:
                self._cleanup_path(path)
            self._cleanup_test_file(test_path)

        return self._handle_test_process_result(res)

    def _prepare_python_test_code_for_execution(self, test_code: str):
        if "sys.exit" in test_code and "import sys" not in test_code:
            test_code = "import sys\n" + test_code

        syntax_error = self._validate_python_test_syntax(test_code)
        if syntax_error:
            return self._record_test_failure(syntax_error)

        stdout_error = self._find_stdout_capture(test_code)
        if stdout_error:
            return self._record_test_failure(stdout_error)

        shadow_error = self._find_shadowed_target_symbols(test_code)
        if shadow_error:
            return self._record_test_failure(shadow_error)

        return self._prepare_python_test_code(test_code)

    def _run_java_test(self, test_code: str, safe_name: str) -> Tuple[str, str]:
        if not self.target_file:
            return self._record_test_failure("File target non impostato.")

        class_name = self._extract_java_public_class(test_code) or Path(safe_name).stem
        if not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", class_name):
            class_name = "AiAgentTest"

        temp_dir = self.repo_root / f".ai_agent_java_{uuid.uuid4().hex}"
        test_path = temp_dir / f"{class_name}.java"
        try:
            if not shutil.which("javac"):
                return self._record_test_failure("javac non trovato. Installa Java Development Kit (JDK).")
            if not shutil.which("java"):
                return self._record_test_failure("java non trovato. Installa Java Runtime Environment (JRE).")
            javac_error = self._check_runtime(["javac", "-version"], "javac non risponde correttamente.")
            if javac_error:
                return self._record_test_failure(javac_error)
            java_error = self._check_runtime(["java", "-version"], "java non risponde correttamente.")
            if java_error:
                return self._record_test_failure(java_error)

            temp_dir.mkdir(parents=True, exist_ok=False)
            test_path.write_text(test_code, encoding="utf-8")

            compile_res = run_process(
                [
                    "javac",
                    "--release",
                    "11",
                    "-encoding",
                    "UTF-8",
                    "-d",
                    str(temp_dir),
                    str(self.target_file.resolve()),
                    str(test_path),
                ],
                cwd=self.repo_root,
                timeout=TEST_TIMEOUT_SECONDS,
            )
            if compile_res.returncode != 0:
                output = (compile_res.stdout + "\n" + compile_res.stderr).strip()
                return self._record_test_failure(output or "Compilazione Java non riuscita.")

            run_res = run_process(
                ["java", "-cp", str(temp_dir), class_name],
                cwd=self.repo_root,
                timeout=TEST_TIMEOUT_SECONDS,
            )
            return self._handle_test_process_result(run_res)
        except subprocess.TimeoutExpired:
            return self._record_test_failure(f"Timeout: il test ha superato {TEST_TIMEOUT_SECONDS} secondi.")
        except Exception as exc:
            return self._record_test_failure(f"Esecuzione test Java non riuscita: {exc}")
        finally:
            self._cleanup_path(temp_dir)

    def _run_c_test(self, test_code: str, safe_name: str) -> Tuple[str, str]:
        return self._run_native_compiled_test(
            test_code=test_code,
            safe_name=safe_name,
            compiler="gcc",
            compiler_name="gcc",
            compiler_args=["-std=c11", "-Wall", "-Wextra"],
            missing_message="gcc non trovato. Installa GCC.",
        )

    def _run_cpp_test(self, test_code: str, safe_name: str) -> Tuple[str, str]:
        return self._run_native_compiled_test(
            test_code=test_code,
            safe_name=safe_name,
            compiler="g++",
            compiler_name="g++",
            compiler_args=["-std=c++17", "-Wall", "-Wextra"],
            missing_message="g++ non trovato. Installa un compilatore C++.",
        )

    def _run_native_compiled_test(
        self,
        test_code: str,
        safe_name: str,
        compiler: str,
        compiler_name: str,
        compiler_args: List[str],
        missing_message: str,
    ) -> Tuple[str, str]:
        if not self.target_file:
            return self._record_test_failure("File target non impostato.")

        if not shutil.which(compiler):
            return self._record_test_failure(missing_message)
        compiler_error = self._check_runtime([compiler, "--version"], f"{compiler} non risponde correttamente.")
        if compiler_error:
            return self._record_test_failure(compiler_error)

        temp_dir = self.repo_root / f".ai_agent_native_{uuid.uuid4().hex}"
        test_path = temp_dir / safe_name
        exe = temp_dir / ("AiAgentTest.exe" if os.name == "nt" else "AiAgentTest")
        try:
            temp_dir.mkdir(parents=True, exist_ok=False)
            test_path.write_text(test_code, encoding="utf-8")

            compile_res = run_process(
                [compiler_name] + compiler_args + [str(self.target_file.resolve()), str(test_path), "-o", str(exe)],
                cwd=self.repo_root,
                timeout=TEST_TIMEOUT_SECONDS,
            )
            if compile_res.returncode != 0:
                output = (compile_res.stdout + "\n" + compile_res.stderr).strip()
                return self._record_test_failure(output or "Compilazione test nativa non riuscita.")

            run_res = run_process([str(exe)], cwd=self.repo_root, timeout=TEST_TIMEOUT_SECONDS)
            return self._handle_test_process_result(run_res)
        except subprocess.TimeoutExpired:
            return self._record_test_failure(f"Timeout: il test ha superato {TEST_TIMEOUT_SECONDS} secondi.")
        except Exception as exc:
            return self._record_test_failure(f"Esecuzione test nativo non riuscita: {exc}")
        finally:
            self._cleanup_path(temp_dir)

    def _run_csharp_test(self, test_code: str, safe_name: str) -> Tuple[str, str]:
        if not self.target_file:
            return self._record_test_failure("File target non impostato.")

        if shutil.which("csc"):
            return self._run_csharp_with_csc(test_code, safe_name)

        if shutil.which("dotnet"):
            sdk_error = self._check_dotnet_sdk()
            if sdk_error:
                return self._record_test_failure(sdk_error)
            return self._run_csharp_with_dotnet(test_code, safe_name)

        return self._record_test_failure("Nessun runtime C# trovato. Installa .NET SDK o csc.")

    @staticmethod
    def _check_dotnet_sdk() -> str:
        try:
            res = run_process(["dotnet", "--list-sdks"], timeout=10)
        except Exception as exc:
            return f"Impossibile verificare .NET SDK: {exc}"

        if res.returncode != 0 or not res.stdout.strip():
            return "dotnet trovato ma nessun SDK .NET disponibile. Installa .NET SDK."

        return ""

    def _run_csharp_with_csc(self, test_code: str, safe_name: str) -> Tuple[str, str]:
        if not self.target_file:
            return self._record_test_failure("File target non impostato.")

        temp_dir = self.repo_root / f".ai_agent_csharp_{uuid.uuid4().hex}"
        test_path = temp_dir / safe_name
        exe = temp_dir / "AiAgentCSharpTest.exe"
        try:
            csc_error = self._check_runtime(["csc", "/help"], "csc non risponde correttamente.")
            if csc_error:
                return self._record_test_failure(csc_error)

            temp_dir.mkdir(parents=True, exist_ok=False)
            test_path.write_text(test_code, encoding="utf-8")

            compile_res = run_process(
                ["csc", "/nologo", f"/out:{exe}", str(self.target_file.resolve()), str(test_path)],
                cwd=self.repo_root,
                timeout=TEST_TIMEOUT_SECONDS,
            )
            if compile_res.returncode != 0:
                output = (compile_res.stdout + "\n" + compile_res.stderr).strip()
                return self._record_test_failure(output or "Compilazione C# non riuscita.")

            run_res = run_process([str(exe)], cwd=self.repo_root, timeout=TEST_TIMEOUT_SECONDS)
            return self._handle_test_process_result(run_res)
        except subprocess.TimeoutExpired:
            return self._record_test_failure(f"Timeout: il test ha superato {TEST_TIMEOUT_SECONDS} secondi.")
        except Exception as exc:
            return self._record_test_failure(f"Esecuzione test C# non riuscita: {exc}")
        finally:
            self._cleanup_path(temp_dir)

    def _run_csharp_with_dotnet(self, test_code: str, safe_name: str) -> Tuple[str, str]:
        if not self.target_file:
            return self._record_test_failure("File target non impostato.")

        temp_dir = self.repo_root / f".ai_agent_csharp_{uuid.uuid4().hex}"
        try:
            target_framework = self._dotnet_target_framework()
            if not target_framework:
                return self._record_test_failure("Impossibile determinare il TargetFramework .NET disponibile.")

            temp_dir.mkdir(parents=True, exist_ok=False)
            (temp_dir / self.target_file.name).write_text(
                self.target_file.read_text(encoding="utf-8-sig"),
                encoding="utf-8",
            )
            (temp_dir / safe_name).write_text(test_code, encoding="utf-8")
            (temp_dir / "AiAgentCSharpTest.csproj").write_text(
                (
                    '<Project Sdk="Microsoft.NET.Sdk">\n'
                    "  <PropertyGroup>\n"
                    "    <OutputType>Exe</OutputType>\n"
                    f"    <TargetFramework>{target_framework}</TargetFramework>\n"
                    "    <ImplicitUsings>disable</ImplicitUsings>\n"
                    "    <Nullable>disable</Nullable>\n"
                    "  </PropertyGroup>\n"
                    "</Project>\n"
                ),
                encoding="utf-8",
            )

            run_res = run_process(
                ["dotnet", "run", "--project", str(temp_dir / "AiAgentCSharpTest.csproj")],
                cwd=temp_dir,
                timeout=TEST_TIMEOUT_SECONDS,
            )
            return self._handle_test_process_result(run_res)
        except subprocess.TimeoutExpired:
            return self._record_test_failure(f"Timeout: il test ha superato {TEST_TIMEOUT_SECONDS} secondi.")
        except Exception as exc:
            return self._record_test_failure(f"Esecuzione test C# non riuscita: {exc}")
        finally:
            self._cleanup_path(temp_dir)

    @staticmethod
    def _dotnet_target_framework() -> str:
        try:
            res = run_process(["dotnet", "--list-sdks"], timeout=10)
        except Exception:
            return ""

        versions = []
        for line in res.stdout.splitlines():
            match = re.match(r"(\d+)\.(\d+)", line.strip())
            if match:
                versions.append((int(match.group(1)), int(match.group(2))))

        if not versions:
            return ""

        major, minor = max(versions)
        if major >= 5:
            return f"net{major}.0"
        if major == 3:
            return "netcoreapp3.1"
        if major == 2:
            return "netcoreapp2.1"
        return f"net{major}.{minor}"

    @staticmethod
    def _strip_code_fence(value: str) -> str:
        cleaned = value.strip().lstrip("\ufeff")

        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines:
                lines = lines[1:]
            cleaned = "\n".join(lines).strip()

        if cleaned.endswith("```"):
            lines = cleaned.splitlines()
            if lines:
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        return cleaned.lstrip("\ufeff")

    @staticmethod
    def _extract_java_public_class(test_code: str) -> Optional[str]:
        match = re.search(r"\bpublic\s+class\s+([A-Za-z_$][A-Za-z0-9_$]*)", test_code)
        return match.group(1) if match else None

    @staticmethod
    def _validate_java_test_structure(test_code: str) -> str:
        if not TestRunnerMixin._extract_java_public_class(test_code):
            return (
                "Test Java non valido: manca una classe public. "
                "Genera una classe public con una sola public static void main(String[] args)."
            )

        main_count = len(
            re.findall(
                r"\bpublic\s+static\s+void\s+main\s*\(\s*(?:String\s*\[\]\s+\w+|String\s+\w+\s*\[\])\s*\)",
                test_code,
            )
        )
        if main_count == 0:
            return (
                "Test Java non valido: manca il metodo main. "
                "Genera una sola public static void main(String[] args) e chiama li tutti i casi."
            )
        if main_count > 1:
            return (
                "Test Java non valido: la classe di test contiene piu metodi main. "
                "Genera una sola public static void main(String[] args) e chiama li tutti i casi."
            )
        return ""

    def _handle_test_process_result(self, res: subprocess.CompletedProcess) -> Tuple[str, str]:
        out_text = (res.stdout + "\n" + res.stderr).strip()
        m_pass = re.search(r"Passed:\s*(\d+)", out_text, re.IGNORECASE)
        m_fail = re.search(r"Failed:\s*(\d+)", out_text, re.IGNORECASE)
        has_metrics = bool(m_pass and m_fail)

        display_text = out_text
        if has_metrics:
            display_text = self._format_test_output(out_text, m_pass.group(1), m_fail.group(1))

        with self._lock:
            self.test_output_log = display_text
            self.tests_passed = m_pass.group(1) if m_pass else "0"
            self.tests_failed = m_fail.group(1) if m_fail else "0"

        if not has_metrics:
            msg = out_text or "Il test non ha stampato Passed/Failed nel formato richiesto."
            return self._record_test_failure(msg)

        if res.returncode == 0 and self.tests_failed == "0":
            with self._lock:
                self.test_status = "Passato"
            return "passed", ""

        if self.tests_failed == "0":
            msg = out_text or "Il test ha stampato metriche positive ma e terminato con errore."
            return self._record_test_failure(msg)

        with self._lock:
            self.test_status = "Fallito"
        return "structured_failed", display_text

    def _record_test_failure(self, message: str) -> Tuple[str, str]:
        with self._lock:
            self.test_status = "Fallito"
            self.test_output_log = message
            self.tests_passed = "0"
            self.tests_failed = "0"
        return "failed", message

    @staticmethod
    def _validate_python_test_syntax(test_code: str) -> str:
        try:
            ast.parse(test_code)
            return ""
        except SyntaxError as exc:
            location = f"linea {exc.lineno}" if exc.lineno else "posizione sconosciuta"
            return (
                "Test non valido: il codice Python generato non e sintatticamente valido "
                f"({location}: {exc.msg}). Correggi il test usando funzioni normali; "
                "non usare assert dentro lambda."
            )

    @staticmethod
    def _find_stdout_capture(test_code: str) -> str:
        blocked_patterns = (
            "sys.stdout =",
            "sys.stdout=",
            "sys.stdout.getvalue",
            "StringIO(",
            "from io import StringIO",
        )
        if any(pattern in test_code for pattern in blocked_patterns):
            return (
                "Test non valido: non catturare o sostituire sys.stdout. "
                "Stampa direttamente su console le righe [PASS], [FAIL], Passed e Failed."
            )
        return ""

    @staticmethod
    def _find_unreliable_test_code(test_code: str, target_ext: str) -> str:
        lowered = test_code.lower()
        blocked_fragments = (
            "system.out.tostring()",
            "manual analysis",
            "based on manual",
            "assumo che",
            "simuliamo il conteggio",
            "non posso catturare",
            "expected outcomes for demonstration",
            "hardcoded",
            "valori manualmente",
        )
        if any(fragment in lowered for fragment in blocked_fragments):
            return (
                "Test non valido: i contatori Passed/Failed devono essere calcolati "
                "durante l'esecuzione reale, non impostati o giustificati manualmente."
            )

        if target_ext in (".java", ".js", ".ts"):
            suspicious_assignments = (
                r"passed\w*\s*=\s*\d+\s*;\s*(?://|/\*).*manual",
                r"failed\w*\s*=\s*0\s*;\s*(?://|/\*).*manual",
            )
            if any(re.search(pattern, test_code, re.IGNORECASE) for pattern in suspicious_assignments):
                return "Test non valido: i contatori finali non devono essere hardcoded."

        if target_ext in (".js", ".ts"):
            assert_helper = re.search(
                r"function\s+assert\s*\(\s*(\w+)\s*,.*?\)\s*\{(?P<body>.*?)\n\}",
                test_code,
                re.DOTALL,
            )
            if assert_helper:
                callback_name = re.escape(assert_helper.group(1))
                body = assert_helper.group("body")
                ignored_callback = re.search(
                    rf"\b{callback_name}\s*\(\s*\)\s*;\s*console\.(?:log|error)\s*\(\s*`?\[PASS\]",
                    body,
                    re.DOTALL,
                )
                if ignored_callback:
                    return (
                        "Test non valido: l'helper assert chiama la callback ma ignora il valore restituito. "
                        "Controlla esplicitamente il risultato prima di stampare [PASS]."
                    )

        return ""

    def _find_shadowed_target_symbols(self, test_code: str) -> str:
        if not self.target_file or self.target_file.suffix.lower() != ".py":
            return ""

        try:
            target_tree = ast.parse(self.target_file.read_text(encoding="utf-8-sig"))
            test_tree = ast.parse(test_code)
        except (OSError, SyntaxError):
            return ""

        target_symbols = {
            node.name
            for node in target_tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
        }
        if not target_symbols:
            return ""

        test_symbols = {
            node.name
            for node in test_tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in target_symbols
        }
        if not test_symbols:
            return ""

        names = ", ".join(sorted(test_symbols))
        return (
            "Test non valido: ridefinisce simboli del file target "
            f"({names}) invece di usare il codice reale da validare."
        )

    def _prepare_python_test_code(self, test_code: str) -> str:
        if not self.target_file or self.target_file.suffix.lower() != ".py":
            return test_code

        target_path = self.target_file.resolve()
        preload = (
            "import importlib.util as _ai_importlib_util\n"
            "from pathlib import Path as _AiPath\n\n"
            f"_ai_target_path = _AiPath(r\"{target_path}\")\n"
            "_ai_spec = _ai_importlib_util.spec_from_file_location(\"_ai_agent_target_module\", _ai_target_path)\n"
            "_ai_module = _ai_importlib_util.module_from_spec(_ai_spec)\n"
            "_ai_spec.loader.exec_module(_ai_module)\n"
            "for _ai_name in dir(_ai_module):\n"
            "    if not _ai_name.startswith(\"_\"):\n"
            "        globals().setdefault(_ai_name, getattr(_ai_module, _ai_name))"
        )
        return preload + "\n\n" + test_code

    @staticmethod
    def _format_test_output(out_text: str, passed: str, failed: str) -> str:
        has_detail_rows = bool(re.search(r"^\s*\[(PASS|FAIL)\]", out_text, re.IGNORECASE | re.MULTILINE))
        if has_detail_rows:
            return out_text

        summary = []
        try:
            passed_count = int(passed)
            failed_count = int(failed)
        except ValueError:
            return out_text

        if passed_count > 0:
            summary.append(f"[PASS] {passed_count} controlli superati")
        if failed_count > 0:
            summary.append(f"[FAIL] {failed_count} controlli falliti: dettagli nei messaggi precedenti")

        if not summary:
            return out_text

        base = out_text.rstrip()
        return base + "\n\nDettaglio sintetico:\n" + "\n".join(summary)

    def _safe_test_file_name(self, name: str) -> str:
        base = Path(name.strip().strip('"').strip("'")).name
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)

        if not base or "." not in base:
            ext = ".py"
            if self.target_file:
                ext = {
                    ".js": ".js",
                    ".ts": ".ts",
                    ".dart": ".dart",
                    ".swift": ".swift",
                    ".java": ".java",
                    ".c": ".c",
                    ".cpp": ".cpp",
                    ".cs": ".cs",
                }.get(self.target_file.suffix.lower(), ".py")
            base = f"test_ai_fix{ext}"

        return base

    def _make_temp_test_path(self, safe_name: str) -> Path:
        base_dir = self.repo_root
        if self.target_file and self.target_file.suffix.lower() in {".py", ".js", ".ts", ".dart", ".swift"}:
            base_dir = self.target_file.parent

        return base_dir / f".ai_agent_test_{uuid.uuid4().hex}_{safe_name}"

    def _build_test_command(
        self,
        cmd: str,
        test_path: Path,
        safe_name: str,
    ) -> Tuple[List[str], str, List[Path]]:
        if not self.target_file:
            return [], "File target non impostato.", []

        target_ext = self.target_file.suffix.lower()
        tokens = self._split_command(cmd)
        if not tokens:
            return [], "RUN_COMMAND vuoto.", []

        executable = Path(tokens[0]).name.lower()

        if target_ext == ".py" or executable in ("python", "python3", "py"):
            return [sys.executable, str(test_path)], "", []

        if target_ext == ".ts":
            if shutil.which("tsx"):
                runtime_error = self._check_runtime(["tsx", "--version"], "tsx non risponde correttamente.")
                if runtime_error:
                    return [], runtime_error, []
                return ["tsx", str(test_path)], "", []
            if shutil.which("ts-node"):
                runtime_error = self._check_runtime(["ts-node", "--version"], "ts-node non risponde correttamente.")
                if runtime_error:
                    return [], runtime_error, []
                return ["ts-node", str(test_path)], "", []
            return [], "Nessun runtime TypeScript trovato. Installa tsx con: npm install -g tsx", []

        if target_ext == ".js" or executable == "node":
            if not shutil.which("node"):
                return [], "node non trovato. Installa Node.js.", []
            runtime_error = self._check_runtime(["node", "--version"], "node non risponde correttamente.")
            if runtime_error:
                return [], runtime_error, []
            return ["node", str(test_path)], "", []

        if target_ext == ".dart" or executable == "dart":
            if not shutil.which("dart"):
                return [], "dart non trovato. Installa il Dart SDK.", []
            runtime_error = self._check_runtime(["dart", "--version"], "dart non utilizzabile. Installa il Dart SDK.")
            if runtime_error:
                return [], runtime_error, []
            return ["dart", str(test_path)], "", []

        if target_ext == ".swift":
            return self._compile_swift(test_path)

        allowed = {"dotnet"}
        if executable not in allowed:
            return [], f"Comando non consentito per sicurezza: {cmd}", []

        normalized = [str(test_path) if Path(tok).name == safe_name else tok for tok in tokens]
        if str(test_path) not in normalized:
            return [], "Il comando di test non fa riferimento al file generato.", []

        return normalized, "", []

    @staticmethod
    def _split_command(cmd: str) -> List[str]:
        try:
            return shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError:
            return []

    @staticmethod
    def _check_runtime(args: List[str], fallback_message: str) -> str:
        try:
            res = run_process(args, timeout=5)
        except FileNotFoundError:
            return fallback_message
        except subprocess.TimeoutExpired:
            return fallback_message
        except Exception as exc:
            return f"Impossibile eseguire {args[0]}: {exc}"

        if res.returncode != 0:
            output = (res.stdout + "\n" + res.stderr).strip()
            return output or fallback_message

        return ""

    def _compile_swift(self, test_path: Path) -> Tuple[List[str], str, List[Path]]:
        if not self.target_file:
            return [], "File target non impostato.", []

        if not shutil.which("swiftc"):
            return [], "swiftc non trovato. Installa Xcode Command Line Tools con: xcode-select --install", []

        target_dir = self.target_file.parent
        swift_files = sorted(str(path) for path in target_dir.glob("*.swift"))
        test_abs = str(test_path.resolve())
        if test_abs not in swift_files:
            swift_files.append(test_abs)

        exe = self.git_dir / "ai_agent_tests" / ("TestExe.exe" if os.name == "nt" else "TestExe")
        comp = run_process(["swiftc"] + swift_files + ["-o", str(exe)], cwd=self.repo_root)
        if comp.returncode != 0:
            return [], comp.stderr.strip() or comp.stdout.strip(), []

        return [str(exe)], "", [exe]

    def _cleanup_test_file(self, t_file: Optional[Path]) -> None:
        if not t_file:
            return
        try:
            if t_file.exists():
                t_file.unlink()
        except Exception:
            pass

    def _cleanup_path(self, path: Optional[Path]) -> None:
        if not path:
            return
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except Exception:
            pass

    def _cleanup_backups(self) -> None:
        for path in self.backup_files:
            self._cleanup_path(path)

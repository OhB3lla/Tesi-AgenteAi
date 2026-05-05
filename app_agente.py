import os
import sys
import subprocess
import re
import threading
import difflib
import csv
import time
import stat
import shutil
from datetime import datetime
import tkinter as tk
from tkinter import filedialog
from google import genai
import customtkinter as ctk

# ──────────────────────────────────────────────
# CONFIGURAZIONE GLOBALE
# ──────────────────────────────────────────────
BYPASS_ENV_VAR        = "AI_AGENT_BYPASS"
BYPASS_TTL_SECONDS    = 120          # il bypass file scade dopo 2 minuti
DIFF_CONTEXT_LINES    = 8
API_KEY_FALLBACK_FILE = ".api_key"
MAX_RETRY_ATTEMPTS    = 3


# ──────────────────────────────────────────────
# BYPASS HOOK
# ──────────────────────────────────────────────
def check_and_clear_bypass():
    """
    Verifica se il flag di bypass è attivo e, se presente, lo consuma.

    Il meccanismo primario è un file fisico in .git/ai_agent_bypass:
    sopravvive alla terminazione del processo Python e viene letto dal
    processo figlio avviato dal successivo 'git push'. La variabile
    d'ambiente è mantenuta come fallback ma non è affidabile tra processi
    distinti (es. Git Bash, SourceTree).

    Il bypass file include un timestamp UNIX: viene accettato solo se
    scritto negli ultimi BYPASS_TTL_SECONDS secondi, evitando che un file
    rimasto orfano (es. crash di rete durante il secondo push) salti
    indefinitamente le analisi future.
    """
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, text=True, check=True
        )
        bypass_file = os.path.join(res.stdout.strip(), "ai_agent_bypass")
        if os.path.exists(bypass_file):
            try:
                with open(bypass_file, "r") as f:
                    written_at = float(f.read().strip())
                age = time.time() - written_at
                os.remove(bypass_file)
                if age <= BYPASS_TTL_SECONDS:
                    return True
                print(f"[Agente AI] Bypass file scaduto ({int(age)}s > {BYPASS_TTL_SECONDS}s), analisi riparte.")
            except (ValueError, OSError):
                try:
                    os.remove(bypass_file)
                except OSError:
                    pass
    except Exception:
        pass

    if os.environ.get(BYPASS_ENV_VAR) == "1":
        return True

    return False


def set_bypass_flag():
    """
    Scrive il flag di bypass in .git/ con il timestamp corrente.
    È l'unico metodo affidabile per comunicare il bypass al processo
    figlio del successivo 'git push'.
    """
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, text=True, check=True
        )
        bypass_file = os.path.join(res.stdout.strip(), "ai_agent_bypass")
        with open(bypass_file, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


# ──────────────────────────────────────────────
# RISOLUZIONE API KEY
# ──────────────────────────────────────────────
def resolve_api_key():
    """
    Risolve la API key con tre livelli di fallback progressivi:
      1. Variabile d'ambiente GOOGLE_API_KEY  (standard)
      2. File .api_key nella directory corrente
      3. File .api_key nella root del repository Git

    Il supporto al file fisico è necessario perché Git Bash e alcune GUI
    (SourceTree, GitKraken) non propagano le variabili d'ambiente di sistema
    ai processi figli degli hook.
    """
    key = os.getenv("GOOGLE_API_KEY")
    if key:
        return key.strip()

    local_file = os.path.join(os.getcwd(), API_KEY_FALLBACK_FILE)
    if os.path.exists(local_file):
        try:
            with open(local_file, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except Exception:
            pass

    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, check=True
        )
        repo_root_file = os.path.join(res.stdout.strip(), API_KEY_FALLBACK_FILE)
        if os.path.exists(repo_root_file) and repo_root_file != local_file:
            with open(repo_root_file, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
    except Exception:
        pass

    return None


# ──────────────────────────────────────────────
# GIT MANAGER
# ──────────────────────────────────────────────
class GitManager:

    @staticmethod
    def get_modified_files():
        """
        Restituisce i percorsi assoluti dei file aggiunti o modificati
        nell'ultimo commit (HEAD), escludendo le cancellazioni.
        """
        try:
            root_res = subprocess.run(
                ['git', 'rev-parse', '--show-toplevel'],
                capture_output=True, text=True, check=True
            )
            repo_root = root_res.stdout.strip()

            res = subprocess.run(
                ['git', 'diff-tree', '--no-commit-id', '--name-status', '-r', 'HEAD'],
                capture_output=True, text=True, check=True
            )
            valid_files = []
            for line in res.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t')
                if parts[0].startswith('D'):
                    continue
                full_path = os.path.abspath(os.path.join(repo_root, parts[-1]))
                if os.path.exists(full_path) and os.path.isfile(full_path):
                    valid_files.append(full_path)
            return valid_files
        except Exception:
            return []

    @staticmethod
    def get_context_files(target_file, max_files=3):
        """
        Recupera fino a max_files file nella stessa directory con la stessa
        estensione, da fornire come contesto architetturale all'LLM.
        """
        target_dir = os.path.dirname(os.path.abspath(target_file))
        target_ext = os.path.splitext(target_file)[1]
        context_files = []
        if not os.path.exists(target_dir):
            return []
        for f in os.listdir(target_dir):
            full_path = os.path.join(target_dir, f)
            if full_path == os.path.abspath(target_file):
                continue
            if not f.endswith(target_ext):
                continue
            context_files.append(full_path)
            if len(context_files) >= max_files:
                break
        return context_files

    @staticmethod
    def read_files(file_list):
        """Concatena il contenuto dei file in una stringa strutturata per il prompt."""
        content = ""
        for file_name in file_list:
            if not os.path.exists(file_name):
                continue
            try:
                with open(file_name, "r", encoding="utf-8") as f:
                    content += f"\n\n--- FILE: {os.path.basename(file_name)} ---\n{f.read()}\n"
            except Exception:
                continue
        return content


# ──────────────────────────────────────────────
# CLIENT AI
# ──────────────────────────────────────────────
class GenAIClient:
    MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]

    def __init__(self, api_key):
        self.client = genai.Client(api_key=api_key)

    def analyze_code(self, target_file, source_code, context_code="", error_feedback=""):
        """
        Costruisce il prompt strutturato e lo invia all'API Gemini.
        Se error_feedback è valorizzato, viene aggiunto in coda al prompt
        per implementare il ciclo di Execution Feedback (Zhang et al., 2026).
        In caso di errore temporaneo (503/429) ritenta una volta dopo 5s;
        in caso di modello non disponibile (404) passa al successivo.
        """
        prompt = (
            "Sei un Code Reviewer automatizzato. Analizza il codice seguente:\n\n"
            f"FILE TARGET: {target_file}\nCODICE TARGET:\n{source_code}\n\n"
            f"CONTESTO ARCHITETTURALE:\n{context_code}\n\n"
            "REGOLE OPERATIVE:\n"
            "1. Individua difetti logici nel FILE TARGET. Ignora stile e formattazione.\n"
            "2. Se rilevi bug, fornisci: ## ANALISI DELL'ERRORE, ## CODICE CORRETTO e ## UNIT TEST.\n"
            "3. Se non ci sono bug, scrivi 'Nessun bug' e fornisci uno script di test basilare.\n"
            "4. I test devono essere script FLAT (nessun framework esterno, no pytest, no junit).\n"
            "5. Lo script di test deve stampare le metriche esatte in questo formato:\n"
            "   Passed: [numero]\n"
            "   Failed: [numero]\n"
            "   Usa sys.exit(1) in caso di fallimenti, sys.exit(0) se tutto passa.\n"
            "6. Concludi SEMPRE con il blocco:\n"
            "   DEPENDENCIES: NONE\n"
            "   TEST_FILE_NAME: [nome file, es. test_fix.py]\n"
            "   RUN_COMMAND: [comando shell di esecuzione, es. python test_fix.py]\n"
        )

        if error_feedback:
            prompt += (
                f"\n\n[FEEDBACK ESECUZIONE PRECEDENTE]\n"
                f"Il test ha restituito il seguente errore:\n```\n{error_feedback}\n```\n"
                f"Analizza l'errore, correggi il codice e assicurati di "
                f"stampare Passed/Failed nel formato richiesto."
            )

        for model_name in self.MODELS:
            print(f"[API] Connessione a: {model_name}")
            for attempt in range(2):
                try:
                    return self.client.models.generate_content(
                        model=model_name, contents=prompt
                    ).text
                except Exception as e:
                    err = str(e)
                    if "404" in err:
                        print(f"[API] Modello {model_name} non disponibile (404), switch.")
                        break
                    elif any(x in err for x in ["503", "UNAVAILABLE", "429"]):
                        if attempt < 1:
                            print("[API] Server saturo, nuovo tentativo in 5s...")
                            time.sleep(5)
                        else:
                            print(f"[API] Server {model_name} irraggiungibile, switch.")
                    else:
                        print(f"[API] Errore su {model_name}: {e}, switch.")
                        break

        raise Exception("Nessun modello Gemini disponibile. Verificare connessione e API key.")


# ──────────────────────────────────────────────
# LOGGER TELEMETRIA
# ──────────────────────────────────────────────
class ExperimentLogger:
    """
    Modulo passivo di raccolta dati per la fase sperimentale.
    Separa il tempo netto di chiamata API dal tempo totale di sessione
    (che include la revisione umana), consentendo un'analisi quantitativa
    dell'efficienza del ciclo Human-in-the-Loop.
    """
    LOG_FILE = "thesis_metrics.csv"

    @staticmethod
    def initialize():
        if not os.path.exists(ExperimentLogger.LOG_FILE):
            try:
                with open(ExperimentLogger.LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Timestamp", "File Analizzato", "Esito LLM", "Stato Test",
                        "Passati", "Falliti", "Azione Utente",
                        "Tempo API (s)", "Tempo Sessione (s)"
                    ])
            except Exception:
                pass

    @staticmethod
    def log_run(target_file, llm_status, test_status, passed, failed,
                human_action, api_time, session_time):
        try:
            with open(ExperimentLogger.LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    os.path.basename(target_file),
                    llm_status, test_status, passed, failed, human_action,
                    round(api_time, 2),
                    round(session_time, 2)
                ])
        except Exception:
            pass


# ──────────────────────────────────────────────
# APP PRINCIPALE
# ──────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class GitAgentApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Git Pre-Push AI Reviewer")
        self.geometry("860x620")
        self.protocol("WM_DELETE_WINDOW", self.bypass_hook)

        self.fixed_code          = ""
        self.target_file         = ""
        self.generated_test_code = ""
        self.test_output_log     = ""
        self.tests_passed        = "0"
        self.tests_failed        = "0"
        self.test_status         = "N/A"
        self.action_taken        = "Nessuna azione"
        self._pending_test_file  = ""

        self.user_decision_event  = threading.Event()
        self.force_push_requested = False
        self.patched_files_list   = []

        self._lock = threading.Lock()

        self._total_files    = 0
        self._analyzed_files = 0
        self._patched_count  = 0

        ExperimentLogger.initialize()
        self._build_ui()

        self.safe_log("Avvio analisi batch commit...")
        threading.Thread(target=self.run_agent_logic, daemon=True).start()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            header, text="Code Review Automatica",
            font=ctk.CTkFont(size=20, weight="bold")
        ).pack(side="left")

        self.status_label = ctk.CTkLabel(
            header, text="● In esecuzione",
            font=ctk.CTkFont(size=13), text_color="#4CAF50"
        )
        self.status_label.pack(side="right")

        self.log_box = ctk.CTkTextbox(
            self, width=820, height=480, font=("Courier New", 12)
        )
        self.log_box.pack(padx=20, pady=10)

    def safe_log(self, text):
        self.after(0, lambda: self.log_box.insert("end", text + "\n"))
        self.after(0, lambda: self.log_box.see("end"))

    def _set_status(self, text, color):
        self.after(0, lambda: self.status_label.configure(text=text, text_color=color))

    def _log_and_exit(self, exit_code, reason=""):
        if reason:
            print(f"[Sistema] {reason}")
        self.destroy()
        os._exit(exit_code)

    def bypass_hook(self):
        self._log_and_exit(0, "Finestra chiusa. Push originale consentito.")

    # ── Diff Viewer ────────────────────────────────────────────────────────

    def show_diff_viewer(self):
        """
        Apre il popup di revisione con tre schede: diff colorato, script di
        test generato, output dell'esecuzione.

        Gestisce il caso in cui fixed_code sia vuoto (bug rilevato dal test
        ma nessuna patch generata): mostra un messaggio esplicativo nel tab
        Diff e disabilita il pulsante Applica, evitando che l'utente rimanga
        bloccato senza capire cosa sta guardando.
        """
        try:
            with open(self.target_file, "r", encoding="utf-8") as f:
                old_lines = f.readlines()
        except Exception:
            old_lines = []

        has_patch = bool(self.fixed_code)
        new_lines = self.fixed_code.splitlines(keepends=True) if has_patch else old_lines

        diff_lines = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile="Originale", tofile="Patch AI",
            n=DIFF_CONTEXT_LINES
        ))

        popup = ctk.CTkToplevel(self)
        popup.title(f"Review: {os.path.basename(self.target_file)}")
        popup.geometry("940x820")
        popup.grab_set()

        with self._lock:
            t_passed = self.tests_passed
            t_failed = self.tests_failed
            t_status = self.test_status
            t_log    = self.test_output_log

        all_passed  = t_failed == "0" and t_status == "Passato"
        badge_color = "#4CAF50" if all_passed else "#FF6B35"
        badge_text  = (
            f"✓ {t_passed} Passati — Patch validata" if all_passed else
            f"✗ {t_failed} Falliti  |  ✓ {t_passed} Passati — Patch NON validata"
        )

        ctk.CTkLabel(
            popup, text=badge_text,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=badge_color
        ).pack(pady=(15, 5))

        tabs = ctk.CTkTabview(popup, width=900, height=560)
        tabs.pack(pady=5, padx=10)

        tab_diff     = tabs.add("Diff Patch")
        tab_test     = tabs.add("Script di Test")
        tab_log_name = "Output Test" + ("" if all_passed else " ⚠")
        tabs.add(tab_log_name)

        # ── TAB DIFF con colorazione sintattica ─────────────────────────
        diff_frame = ctk.CTkFrame(tab_diff, fg_color="transparent")
        diff_frame.pack(fill="both", expand=True)

        txt_diff = tk.Text(
            diff_frame, font=("Courier New", 11),
            bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", relief="flat", bd=0, wrap="none"
        )
        sb_y = tk.Scrollbar(diff_frame, orient="vertical",   command=txt_diff.yview)
        sb_x = tk.Scrollbar(diff_frame, orient="horizontal", command=txt_diff.xview)
        txt_diff.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.pack(side="right",  fill="y")
        sb_x.pack(side="bottom", fill="x")
        txt_diff.pack(fill="both", expand=True)

        txt_diff.tag_configure("added",   background="#1e3a1e", foreground="#6fcf6f")
        txt_diff.tag_configure("removed", background="#3a1e1e", foreground="#f47676")
        txt_diff.tag_configure("header",  foreground="#569cd6",
                               font=("Courier New", 11, "bold"))
        txt_diff.tag_configure("hunk",    foreground="#c586c0")
        txt_diff.tag_configure("neutral", foreground="#9a9a9a")
        txt_diff.tag_configure("notice",  foreground="#FFA726",
                               font=("Courier New", 11, "italic"))

        if diff_lines:
            for line in diff_lines:
                if line.startswith("+++") or line.startswith("---"):
                    txt_diff.insert("end", line, "header")
                elif line.startswith("@@"):
                    txt_diff.insert("end", line, "hunk")
                elif line.startswith("+"):
                    txt_diff.insert("end", line, "added")
                elif line.startswith("-"):
                    txt_diff.insert("end", line, "removed")
                else:
                    txt_diff.insert("end", line, "neutral")
        elif has_patch:
            txt_diff.insert("end", "Nessuna modifica strutturale rilevata.", "neutral")
        else:
            txt_diff.insert(
                "end",
                "Nessuna patch generata dall'AI per questo file.\n\n"
                "Il test ha rilevato dei fallimenti ma l'AI non ha prodotto\n"
                "una versione corretta del codice. Controlla la scheda\n"
                "'Output Test' per i dettagli e valuta se procedere con\n"
                "il push o scartare.",
                "notice"
            )

        txt_diff.configure(state="disabled")

        # ── TAB SCRIPT DI TEST ──────────────────────────────────────────
        txt_test = ctk.CTkTextbox(tab_test, width=880, height=500, font=("Courier New", 11))
        txt_test.pack(fill="both", expand=True)
        txt_test.insert("0.0", self.generated_test_code or "Script di test non generato.")

        # ── TAB OUTPUT TEST ─────────────────────────────────────────────
        tab_log_widget = tabs.tab(tab_log_name)
        txt_log = ctk.CTkTextbox(tab_log_widget, width=880, height=500, font=("Courier New", 11))
        txt_log.pack(fill="both", expand=True)
        txt_log.insert("0.0", t_log if t_log else "Nessun output disponibile.")

        if not all_passed:
            tabs.set(tab_log_name)

        # ── Pulsanti ────────────────────────────────────────────────────
        btn_frame = ctk.CTkFrame(popup, fg_color="transparent")
        btn_frame.pack(pady=15)

        apply_label = "✔ Applica Patch"  if (all_passed and has_patch) else "⚠ Applica (non validata)"
        apply_color = "#2e7d32"          if (all_passed and has_patch) else "#b8860b"
        apply_hover = "#1b5e20"          if (all_passed and has_patch) else "#8b6508"
        apply_state = "normal" if has_patch else "disabled"

        ctk.CTkButton(
            btn_frame, text=apply_label,
            fg_color=apply_color, hover_color=apply_hover, width=190,
            command=lambda: self._handle_decision(popup, "queue"),
            state=apply_state
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame, text="✖ Scarta",
            fg_color="#795500", hover_color="#5c3d00", width=120,
            command=lambda: self._handle_decision(popup, "skip")
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame, text="⚡ Forza Push",
            fg_color="#b71c1c", hover_color="#7f0000", width=140,
            command=lambda: self._handle_decision(popup, "force")
        ).pack(side="right", padx=10)

    def _handle_decision(self, popup, decision):
        if decision == "queue":
            abs_path = os.path.abspath(self.target_file)
            try:
                shutil.copy2(abs_path, abs_path + ".bak")
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(self.fixed_code)
                self.patched_files_list.append(abs_path)
                with self._lock:
                    self.action_taken   = "Patch Applicata"
                    self._patched_count += 1
                self.safe_log(f"[+] Patch applicata: {os.path.basename(abs_path)}")
            except Exception as e:
                self.safe_log(f"[!] Errore scrittura patch: {e}")

        elif decision == "skip":
            with self._lock:
                self.action_taken = "Scartato"
            self.safe_log(f"[-] Patch ignorata: {os.path.basename(self.target_file)}")

        elif decision == "force":
            self.force_push_requested = True
            with self._lock:
                self.action_taken = "Forza Push"
            self.safe_log("[!] Forza push richiesto dall'utente.")

        # Pulizia file di test FUORI dal lock: os.remove è I/O bloccante
        # e tenere il lock occupato durante I/O causa deadlock su Windows.
        pending = self._pending_test_file
        with self._lock:
            self._pending_test_file = ""
        if pending:
            self._cleanup_test_file(pending)

        popup.destroy()
        self.user_decision_event.set()

    # ── Logica agente ──────────────────────────────────────────────────────

    def run_agent_logic(self):
        try:
            api_key = resolve_api_key()
            if not api_key:
                self.safe_log("[!] API key non trovata.")
                self.safe_log("    1. export GOOGLE_API_KEY=la_tua_chiave")
                self.safe_log("    2. File .api_key nella root del repo")
                self.safe_log("       (aggiungi .api_key al .gitignore!)")
                self.safe_log("    Push autorizzato senza analisi.")
                time.sleep(2)
                return self._log_and_exit(0)

            target_files = GitManager.get_modified_files()
            valid_ext    = ('.py', '.dart', '.swift', '.js', '.ts',
                            '.java', '.cpp', '.c', '.cs')
            target_files = [f for f in target_files if f.endswith(valid_ext)]

            if not target_files:
                self.safe_log("Nessun file sorgente modificato. Push autorizzato.")
                time.sleep(1)
                return self._log_and_exit(0)

            self._total_files = len(target_files)
            self._set_status(f"● Analisi ({self._total_files} file)", "#FFA726")
            ai_client = GenAIClient(api_key)

            for idx, file_path in enumerate(target_files, start=1):
                if self.force_push_requested:
                    break

                self.target_file = file_path
                base_name        = os.path.basename(file_path)
                api_time         = 0.0
                file_start       = time.time()

                self.safe_log(f"\n{'─' * 50}")
                self.safe_log(f"[{idx}/{self._total_files}] Analisi: {base_name}")

                # Reset stato per questo file.
                # Fatto una sola volta qui (non ad ogni tentativo) per non perdere
                # la patch generata in un tentativo precedente nel caso in cui
                # il tentativo successivo produca solo il test corretto.
                with self._lock:
                    self.tests_passed        = "0"
                    self.tests_failed        = "0"
                    self.test_status         = "N/A"
                    self.test_output_log     = ""
                    self.action_taken        = "Nessuna azione"
                    self.fixed_code          = ""
                    self.generated_test_code = ""
                    self._pending_test_file  = ""

                error_feedback = ""
                bug_confirmed  = False

                for attempt in range(MAX_RETRY_ATTEMPTS):
                    self.safe_log(f"  Tentativo {attempt + 1}/{MAX_RETRY_ATTEMPTS}...")

                    t0   = time.time()
                    resp = self._fetch_ai_response(ai_client, file_path, error_feedback)
                    api_time += time.time() - t0

                    if not resp:
                        self.safe_log("  [!] Nessuna risposta dall'AI.")
                        break

                    result, err_log = self._handle_ai_response(resp)

                    if result == "confirmed":
                        bug_confirmed = True
                        with self._lock:
                            p, fa = self.tests_passed, self.tests_failed
                        self.safe_log(f"  Test eseguiti: {p} passati, {fa} falliti.")
                        break
                    elif result == "clean":
                        self.safe_log("  Nessun bug rilevato dall'AI. ✓")
                        break
                    elif result == "failed":
                        self.safe_log(
                            f"  Crash del test (tentativo {attempt + 1}). "
                            f"Invio feedback all'AI..."
                        )
                        error_feedback = err_log or "Esecuzione fallita senza output."
                    else:
                        self.safe_log("  Risposta AI non strutturata correttamente.")
                        break

                self._analyzed_files += 1

                needs_review = bug_confirmed or self.test_status == "Fallito"
                if needs_review:
                    self.safe_log("  → Revisione utente richiesta.")
                    # Pulizia preventiva dell'evento prima di aspettare:
                    # se fosse rimasto settato da una sessione precedente,
                    # wait() uscirebbe immediatamente senza aspettare la
                    # decisione dell'utente sul file corrente.
                    self.user_decision_event.clear()
                    self.after(0, self.show_diff_viewer)
                    self.user_decision_event.wait()
                else:
                    pending = self._pending_test_file
                    with self._lock:
                        self._pending_test_file = ""
                    if pending:
                        self._cleanup_test_file(pending)
                    self.safe_log("  ✓ File validato, nessun problema.")

                # session_time misurato DOPO wait() per includere il tempo di
                # revisione umana, metrica chiave del ciclo Human-in-the-Loop.
                session_time = round(time.time() - file_start, 2)

                with self._lock:
                    ExperimentLogger.log_run(
                        file_path, "Processato", self.test_status,
                        self.tests_passed, self.tests_failed,
                        self.action_taken,
                        round(api_time, 2), session_time
                    )

            self.safe_log(f"\n{'─' * 50}")
            self._finalize()

        except Exception as e:
            self.safe_log(f"\n[Eccezione critica] {e}")
            time.sleep(2)
            self._log_and_exit(0)

    def _finalize(self):
        if self.force_push_requested:
            self.safe_log("Forza push attivo. Push originale ripristinato.")
            self._set_status("● Forza Push", "#FF6B35")
            time.sleep(2)
            return self._log_and_exit(0)

        self.safe_log(
            f"Riepilogo: {self._total_files} file totali | "
            f"{self._analyzed_files} analizzati | "
            f"{self._patched_count} patch applicate"
        )

        for f in self.patched_files_list:
            bak = f + ".bak"
            if os.path.exists(bak):
                try:
                    os.remove(bak)
                except Exception:
                    pass

        if self.patched_files_list:
            # Verifica che i file patchati esistano ancora prima di aggiungerli
            existing = [f for f in self.patched_files_list if os.path.exists(f)]
            missing  = [f for f in self.patched_files_list if not os.path.exists(f)]
            for m in missing:
                self.safe_log(f"  [!] File non trovato, escluso: {os.path.basename(m)}")

            if existing:
                self.safe_log(f"Aggiornamento commit per {len(existing)} file patchati...")

                add_res = subprocess.run(
                    ['git', 'add'] + existing,
                    capture_output=True, text=True
                )
                if add_res.returncode != 0:
                    self.safe_log(f"  [!] Errore git add: {add_res.stderr.strip()}")

                amend_res = subprocess.run(
                    ['git', 'commit', '--amend', '--no-edit'],
                    capture_output=True, text=True
                )
                if amend_res.returncode != 0:
                    self.safe_log(f"  [!] Errore git commit --amend: {amend_res.stderr.strip()}")
                    self.safe_log("  Il push viene bloccato. Risolvi manualmente e riprova.")
                    time.sleep(4)
                    return self._log_and_exit(1)

                set_bypass_flag()
                self._set_status("● Completato", "#4CAF50")
                self.safe_log("✅ Commit aggiornato con le patch.")
                self.safe_log("👉 Esegui nuovamente 'git push' per inviare il codice corretto.")
                time.sleep(4)
                return self._log_and_exit(1)

        self._set_status("● OK", "#4CAF50")
        self.safe_log("✅ Nessuna patch applicata. Push originale autorizzato.")
        time.sleep(2)
        self._log_and_exit(0)

    # ── Metodi privati ─────────────────────────────────────────────────────

    def _fetch_ai_response(self, ai_client, target_file, error_feedback=""):
        source_code  = GitManager.read_files([target_file])
        context_code = GitManager.read_files(GitManager.get_context_files(target_file))
        try:
            return ai_client.analyze_code(
                target_file, source_code, context_code, error_feedback
            )
        except Exception as e:
            self.safe_log(f"  [!] Errore API: {e}")
            return None

    def _handle_ai_response(self, response_text):
        """
        Interpreta la risposta AI ed estrae patch e metadati del test.

        Il reset di fixed_code NON avviene qui: viene fatto una sola volta
        per file in run_agent_logic, in modo che la patch generata in un
        tentativo precedente rimanga disponibile anche se il tentativo
        successivo (destinato a correggere il test) non la ripete.

        Ritorna: ("confirmed"|"clean"|"failed"|"unclear", log_errore)
        """
        match_code = re.search(
            r"##\s*codice corretto.*?```[^\n]*\n(.*?)\n```",
            response_text, re.DOTALL | re.IGNORECASE
        )
        if match_code:
            with self._lock:
                self.fixed_code = match_code.group(1).strip()

        cmd_match    = re.search(r"RUN_COMMAND:\s*(.*)",     response_text, re.IGNORECASE)
        t_file_match = re.search(r"TEST_FILE_NAME:\s*(\S+)", response_text, re.IGNORECASE)
        no_bug       = re.search(r"nessun bug",              response_text, re.IGNORECASE)

        if no_bug:
            if cmd_match and t_file_match:
                return self._run_tests(
                    response_text,
                    cmd_match.group(1).strip(),
                    t_file_match.group(1).strip()
                )
            with self._lock:
                self.test_status = "N/A"
            return "clean", ""

        if cmd_match and t_file_match:
            return self._run_tests(
                response_text,
                cmd_match.group(1).strip(),
                t_file_match.group(1).strip()
            )

        return "unclear", ""

    def _run_tests(self, response_text, cmd, t_file):
        """
        Scrive ed esegue lo script di test generato dall'AI.

        Classificazione del risultato:
        - returncode == 0                      → tutti i test passano  → "confirmed"
        - returncode != 0 + output strutturato → bug reali nel codice  → "confirmed"
          (il viewer mostra i fallimenti per la revisione umana)
        - returncode != 0 + nessun output      → crash del test stesso → "failed"
          (il log viene mandato all'AI per correggere lo script)
        - timeout superato                     → possibile loop infinito → "failed"

        Il file di test viene salvato in _pending_test_file e rimosso
        in _handle_decision() DOPO che l'utente ha chiuso il viewer,
        in modo che rimanga leggibile nella scheda Script di Test.
        """
        blocks = re.findall(r"```[^\n]*\n(.*?)\n```", response_text, re.DOTALL)
        if not blocks:
            return "unclear", ""

        test_code = blocks[-1].strip()

        # Pulizia difensiva: rimuove metadati che l'AI include a volte nel blocco codice
        test_code = re.sub(
            r"(?im)^(DEPENDENCIES|TEST_FILE_NAME|RUN_COMMAND):.*$", "", test_code
        ).strip()

        # Correzione automatica: import sys mancante quando il test usa sys.exit
        if "sys.exit" in test_code and "import sys" not in test_code:
            test_code = "import sys\n" + test_code

        with self._lock:
            self.generated_test_code = test_code

        try:
            with open(t_file, "w", encoding="utf-8") as f:
                f.write(test_code)
        except Exception as e:
            return "failed", str(e)

        with self._lock:
            self._pending_test_file = t_file

        target_ext = os.path.splitext(self.target_file)[1]
        exec_cmd   = cmd

        if cmd.startswith("pytest"):
            exec_cmd = f"{sys.executable} -m {cmd}"
        elif cmd.startswith("python"):
            exec_cmd = f"{sys.executable} {t_file}"
        elif target_ext == ".swift":
            result, exec_cmd = self._compile_swift(t_file)
            if result == "failed":
                self._cleanup_test_file(t_file)
                with self._lock:
                    self._pending_test_file = ""
                return "failed", exec_cmd

        try:
            res = subprocess.run(
                exec_cmd, shell=True,
                capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            timeout_msg = "Timeout: esecuzione test superata (30s). Possibile loop infinito."
            self._cleanup_test_file(t_file)
            with self._lock:
                self._pending_test_file = ""
                self.test_status        = "Fallito"
                self.test_output_log    = timeout_msg
            return "failed", timeout_msg

        out_text = (res.stdout + "\n" + res.stderr).strip()

        m_pass = re.search(r"Passed:\s*(\d+)", out_text, re.IGNORECASE)
        m_fail = re.search(r"Failed:\s*(\d+)", out_text, re.IGNORECASE)

        with self._lock:
            self.test_output_log = out_text
            self.tests_passed    = m_pass.group(1) if m_pass else "0"
            self.tests_failed    = m_fail.group(1) if m_fail else "0"

        if target_ext == ".swift":
            exe = "TestExe.exe" if os.name == 'nt' else "TestExe"
            try:
                os.remove(exe)
            except Exception:
                pass

        has_structured_output = bool(m_pass or m_fail)

        if res.returncode == 0:
            with self._lock:
                self.test_status = "Passato"
            return "confirmed", ""

        if has_structured_output:
            # Il test è stato eseguito correttamente e ha trovato bug reali nel codice.
            # Va mostrato all'utente per la revisione, non rimandato all'AI.
            with self._lock:
                self.test_status = "Fallito"
            return "confirmed", ""

        # Crash sintattico o errore di import nello script di test.
        # Il log viene mandato all'AI per correggere il test stesso.
        with self._lock:
            self.test_status = "Fallito"

        self._cleanup_test_file(t_file)
        with self._lock:
            self._pending_test_file = ""

        err = res.stderr.strip() or res.stdout.strip() or "Exit code non zero, nessun output."
        return "failed", err

    def _cleanup_test_file(self, t_file):
        try:
            if t_file and os.path.exists(t_file):
                os.remove(t_file)
        except Exception:
            pass

    def _compile_swift(self, t_file):
        """Compila i file Swift nella directory target insieme al test generato."""
        target_dir  = os.path.dirname(os.path.abspath(self.target_file))
        swift_files = [
            os.path.join(target_dir, f)
            for f in os.listdir(target_dir) if f.endswith('.swift')
        ]
        abs_t = os.path.abspath(t_file)
        if abs_t not in swift_files:
            swift_files.append(abs_t)

        exe     = "TestExe.exe" if os.name == 'nt' else "TestExe"
        cmd_str = " ".join(f'"{f}"' for f in swift_files)
        comp    = subprocess.run(
            f"swiftc {cmd_str} -o {exe}",
            shell=True, capture_output=True, text=True
        )
        if comp.returncode != 0:
            return "failed", comp.stderr.strip()
        return "ok", f"./{exe}" if os.name != 'nt' else exe


# ──────────────────────────────────────────────
# INSTALLAZIONE HOOK
# ──────────────────────────────────────────────
def install_hook():
    root = tk.Tk()
    root.withdraw()
    target_dir = filedialog.askdirectory(title="Seleziona la root del repository Git")

    if not target_dir:
        sys.exit(1)

    hooks_dir = os.path.join(target_dir, ".git", "hooks")
    if not os.path.exists(hooks_dir):
        print("Errore: directory .git/hooks non trovata.")
        sys.exit(1)

    pre_push_path = os.path.join(hooks_dir, "pre-push")
    script_path   = os.path.abspath(__file__).replace("\\", "/")
    python_exe    = sys.executable.replace("\\", "/")

    bash_hook = f'#!/bin/sh\n"{python_exe}" "{script_path}"\nexit $?\n'
    try:
        with open(pre_push_path, "w", encoding="utf-8") as f:
            f.write(bash_hook)
        current = os.stat(pre_push_path).st_mode
        os.chmod(pre_push_path, current | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        print("✅ Hook pre-push installato correttamente.")
        print(f"   Percorso: {pre_push_path}\n")
        print("Configurazione API key (scegli uno dei metodi):")
        print("  1. export GOOGLE_API_KEY=la_tua_chiave        # Linux/macOS")
        print("     set GOOGLE_API_KEY=la_tua_chiave           # Windows CMD\n")
        print("  2. File .api_key nella root del repository:")
        print("     echo 'la_tua_chiave' > .api_key")
        print("     echo '.api_key' >> .gitignore   # non committare la chiave!")
    except Exception as e:
        print(f"Errore installazione: {e}")
    sys.exit(0)


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_hook()
    else:
        if check_and_clear_bypass():
            print("[Agente AI] Bypass attivo — push in esecuzione senza analisi.")
            sys.exit(0)

        app = GitAgentApp()
        app.mainloop()
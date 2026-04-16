import os
import sys
import subprocess
import re
import threading
import difflib
import stat
import tkinter as tk
from tkinter import filedialog
from google import genai
import customtkinter as ctk

#'installazione automatica dell'hook in un repo target
if len(sys.argv) > 1 and sys.argv[1] == "--install":
    root = tk.Tk()
    root.withdraw()
    
    print("Seleziona la root del progetto Git...")
    target_dir = filedialog.askdirectory(title="Seleziona il repository Git")
    
    if not target_dir:
        sys.exit(1)

    hooks_dir = os.path.join(target_dir, ".git", "hooks")
    if not os.path.exists(hooks_dir):
        print("Errore: cartella .git/hooks non trovata. Sicuro sia un repo Git?")
        sys.exit(1)
        
    pre_push_path = os.path.join(hooks_dir, "pre-push")
    script_path = os.path.abspath(__file__).replace("\\", "/") 
    python_exe = sys.executable.replace("\\", "/")

    # Script per richiamare questo file Python
    bash_hook = f"""#!/bin/sh
"{python_exe}" "{script_path}"
exit $?
"""
    try:
        with open(pre_push_path, "w", encoding="utf-8") as f:
            f.write(bash_hook)
        
        # Aggiunge permessi di esecuzione (necessario per Unix/Git Bash)
        os.chmod(pre_push_path, os.stat(pre_push_path).st_mode | stat.S_IEXEC)
        print(f"Hook installato con successo in: {pre_push_path}")
    except Exception as e:
        print(f"Errore in fase di scrittura dell'hook: {e}")
    
    sys.exit(0)


def read_files(file_list):
    content = ""
    for file_name in file_list:
        if not os.path.exists(file_name) or os.path.isdir(file_name):
            continue
        try:
            with open(file_name, "r", encoding="utf-8") as f:
                content += f"\n\n--- FILE: {file_name} ---\n{f.read()}\n"
        except Exception:
            continue
    return content

def get_modified_files():
    try:
        cmd = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        files = res.stdout.strip().split('\n')
        return [os.path.normpath(f) for f in files if f.strip()]
    except Exception:
        return []


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class GitAgentApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Git Pre-Push AI Reviewer")
        self.geometry("850x600")

        # se l'utente forza la chiusura, facciamo passare il push
        self.protocol("WM_DELETE_WINDOW", self.bypass_hook)

        self.fixed_code = ""
        self.target_file = ""

        self.lbl_title = ctk.CTkLabel(self, text="Code Review", font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_title.pack(pady=(20, 10))

        self.log_box = ctk.CTkTextbox(self, width=800, height=400)
        self.log_box.pack(pady=10)

        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=20)

        self.btn_approve = ctk.CTkButton(self.btn_frame, text="Approva e Pusha", fg_color="green", hover_color="darkgreen", command=self.approve_push, state="disabled")
        self.btn_approve.grid(row=0, column=0, padx=10)

        self.btn_block = ctk.CTkButton(self.btn_frame, text="Blocca Push", fg_color="red", hover_color="darkred", command=self.block_push)
        self.btn_block.grid(row=0, column=1, padx=10)

        self.btn_fix = ctk.CTkButton(self.btn_frame, text="Visualizza Diff e Applica", fg_color="#b8860b", text_color="white", hover_color="#8b6508", command=self.show_diff_viewer, state="disabled")
        self.btn_fix.grid(row=0, column=2, padx=10)

        self.log("Avvio analisi automatica del commit...")
        threading.Thread(target=self.run_agent_logic, daemon=True).start()

    def log(self, text):
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")

    def approve_push(self):
        self.destroy()
        os._exit(0)

    def block_push(self):
        self.destroy()
        os._exit(1)

    def bypass_hook(self):
        self.destroy()
        os._exit(0)

    def show_diff_viewer(self):
        if not self.fixed_code or not self.target_file:
            return

        try:
            with open(self.target_file, "r", encoding="utf-8") as f:
                old_code = f.readlines()
        except Exception:
            old_code = []

        new_code = self.fixed_code.splitlines(keepends=True)
        diff_output = "".join(difflib.unified_diff(old_code, new_code, fromfile='Current', tofile='AI Proposal'))

        popup = ctk.CTkToplevel(self)
        popup.title("Diff Viewer")
        popup.geometry("700x500")
        
        txt = ctk.CTkTextbox(popup, width=650, height=350, font=("Courier", 12))
        txt.pack(pady=20)
        txt.insert("0.0", diff_output if diff_output else "Nessuna differenza strutturale.")
        
        def apply_changes():
            with open(self.target_file, "w", encoding="utf-8") as f:
                f.write(self.fixed_code)
            self.log(f"File {self.target_file} aggiornato. Blocca il push e fai un nuovo commit.")
            self.btn_approve.configure(state="disabled")
            self.btn_fix.configure(state="disabled")
            popup.destroy()

        ctk.CTkButton(popup, text="Applica", fg_color="green", command=apply_changes).pack(side="left", padx=50, pady=10)
        ctk.CTkButton(popup, text="Annulla", fg_color="gray", command=popup.destroy).pack(side="right", padx=50, pady=10)

    def run_agent_logic(self):
        try:
            api_key = os.getenv('GOOGLE_API_KEY')
            if not api_key:
                self.log("Errore: GOOGLE_API_KEY non trovata nell'ambiente.")
                return

            modified_files = get_modified_files()
            if not modified_files:
                self.btn_approve.configure(state="normal")
                return

            valid_extensions = ('.py', '.dart', '.swift', '.js', '.ts', '.java', '.go', '.cpp', '.c', '.cs')
            target_files = [f for f in modified_files if f.endswith(valid_extensions)]

            if not target_files:
                self.btn_approve.configure(state="normal")
                return

            self.target_file = target_files[0]
            source_code = read_files([self.target_file])

            client = genai.Client(api_key=api_key)
            prompt = (
                "Sei un Code Reviewer automatizzato. Analizza questo codice:\n\n"
                f"FILE: {self.target_file}\n\nCODICE:\n{source_code}\n\n"
                "REGOLE:\n"
                "1. Identifica il linguaggio e trova falle logiche reali. Ignora stile o formattazione.\n"
                "2. Se trovi un bug: fornisci ## ANALISI DELL'ERRORE, ## CODICE CORRETTO e ## UNIT TEST.\n"
                "3. Se non ci sono bug: scrivi 'Nessun bug' e un semplice UNIT TEST che passi con il codice attuale.\n"
                "4. Termina tassativamente con:\n"
                "   DEPENDENCIES: [pacchetti o NONE]\n"
                "   TEST_FILE_NAME: [nome file]\n"
                "   RUN_COMMAND: [comando di test]\n"
            )
            
            self.log(f"Analisi di {self.target_file} in corso...")
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            
            # Salvataggio report per debugging
            with open("REVIEW_REPORT.md", "w", encoding="utf-8") as report:
                report.write(response.text)

            # Parsing della risposta
            match_code = re.search(r"## CODICE CORRETTO.*?```[^\n]*\n(.*?)\n```", response.text, re.DOTALL)
            if match_code: 
                self.fixed_code = match_code.group(1).strip()

            cmd_match = re.search(r"RUN_COMMAND:\s*(.*)", response.text)
            t_file_match = re.search(r"TEST_FILE_NAME:\s*(\S+)", response.text)

            if cmd_match and t_file_match:
                cmd = cmd_match.group(1).strip()
                t_file = t_file_match.group(1).strip()
                
                blocks = re.findall(r"```[^\n]*\n(.*?)\n```", response.text, re.DOTALL)
                if blocks:
                    with open(t_file, "w", encoding="utf-8") as f: 
                        f.write(blocks[-1])
                    
                    self.log(f"Avvio test suite: {cmd}")
                    # Usa l'interprete corrente se è pytest per evitare conflitti di ambiente
                    exec_cmd = f"{sys.executable} -m {cmd}" if cmd.startswith("pytest") else cmd
                    res = subprocess.run(exec_cmd, shell=True, capture_output=True, text=True)

                    if res.returncode == 0:
                        self.log("Test passati con successo. Ready to push.")
                        self.btn_approve.configure(state="normal")
                    else:
                        self.log("Test falliti. Verifica i log o accetta la patch proposta.")
                        if self.fixed_code: 
                            self.btn_fix.configure(state="normal")

        except Exception as e: 
            self.log(f"Eccezione non gestita: {e}")

if __name__ == "__main__":
    app = GitAgentApp()
    app.mainloop()
import os
import subprocess
import re
import threading
from google import genai
import customtkinter as ctk
from tkinter import filedialog

# --- 1. FUNZIONI HELPER (Le mettiamo in cima, fuori dalla classe) ---
def leggi_contenuto_file(lista_nomi):
    testo_accumulato = ""
    for nome in lista_nomi:
        if not os.path.exists(nome) or os.path.isdir(nome):
            continue
        try:
            with open(nome, "r", encoding="utf-8") as f:
                testo_accumulato += f"\n\n--- INIZIO FILE: {nome} ---\n"
                testo_accumulato += f.read()
                testo_accumulato += f"\n--- FINE FILE: {nome} ---\n"
        except Exception:
            continue
    return testo_accumulato

def rileva_nomi_modificati():
    try:
        comando = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']
        risultato = subprocess.run(comando, capture_output=True, text=True, check=True)
        nomi = risultato.stdout.strip().split('\n')
        return [os.path.normpath(n) for n in nomi if n.strip()]
    except Exception as e:
        return []


# --- 2. CLASSE DELL'INTERFACCIA GRAFICA ---
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class AgenteIAApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("AI Code Reviewer - Gemini")
        self.geometry("800x600")
        self.cartella_progetto = ""

        # UI Elements
        self.label_titolo = ctk.CTkLabel(self, text="Agente IA per Code Review", font=ctk.CTkFont(size=20, weight="bold"))
        self.label_titolo.pack(pady=(20, 10))

        self.btn_seleziona = ctk.CTkButton(self, text="1. Seleziona Cartella Progetto (Git)", command=self.seleziona_cartella)
        self.btn_seleziona.pack(pady=10)

        self.label_cartella = ctk.CTkLabel(self, text="Nessuna cartella selezionata", text_color="gray")
        self.label_cartella.pack(pady=(0, 20))

        self.btn_avvia = ctk.CTkButton(self, text="2. Avvia Analisi IA", command=self.avvia_analisi_thread, state="disabled", fg_color="green", hover_color="darkgreen")
        self.btn_avvia.pack(pady=10)

        self.textbox_log = ctk.CTkTextbox(self, width=750, height=350)
        self.textbox_log.pack(pady=20)
        self.scrivi_log("Benvenuto. Seleziona un repository Git per iniziare.")

    def scrivi_log(self, testo):
        """Scrive nel box testuale dell'app invece che nel terminale nero"""
        self.textbox_log.insert("end", testo + "\n")
        self.textbox_log.see("end")

    def seleziona_cartella(self):
        cartella = filedialog.askdirectory(title="Seleziona la cartella del progetto Git")
        if cartella:
            self.cartella_progetto = cartella
            self.label_cartella.configure(text=f"Progetto: {self.cartella_progetto}", text_color="white")
            self.btn_avvia.configure(state="normal")
            self.scrivi_log(f"Cartella impostata: {cartella}")

    def avvia_analisi_thread(self):
        self.btn_avvia.configure(state="disabled", text="Analisi in corso...")
        self.scrivi_log("\n" + "="*40)
        self.scrivi_log("Avvio procedura automatica...")
        
        # Lanciamo la logica pesante in un thread separato
        thread = threading.Thread(target=self.esegui_logica_agente)
        thread.start()

    def esegui_logica_agente(self):
        """Questa è la tua logica originale, adattata per la GUI"""
        try:
            # 1. Spostiamo l'esecuzione nella cartella del progetto
            os.chdir(self.cartella_progetto)
            
            # 2. Controllo API KEY
            KEY = os.getenv('GOOGLE_API_KEY')
            if not KEY:
                self.scrivi_log("❌ ERRORE: Chiave 'GOOGLE_API_KEY' non trovata nelle variabili d'ambiente!")
                return # Invece di exit(1), usiamo return per fermare solo questa funzione

            # 3. Lettura file Git
            self.scrivi_log("Analisi file modificati nell'ultimo commit...")
            nomi_cambiati = rileva_nomi_modificati()

            if not nomi_cambiati:
                self.scrivi_log("Nessun file modificato trovato. Termino analisi.")
                return

            self.scrivi_log(f"File target rilevati: {nomi_cambiati}")

            # 4. Preparazione contesto
            tutti_i_nomi = []
            for root, dirs, files in os.walk("."):
                dirs[:] = [d for d in dirs if not d.startswith('.git')]
                for file in files:
                    if file == "test_gemini.py" or file == "agente_ia.py" or file == "app_agente.py":
                        continue
                    percorso = os.path.relpath(os.path.join(root, file), ".")
                    tutti_i_nomi.append(percorso)

            nomi_contesto = [n for n in tutti_i_nomi if n not in nomi_cambiati]
            codice_target = leggi_contenuto_file(nomi_cambiati)
            codice_contesto = leggi_contenuto_file(nomi_contesto)

            # 5. Connessione a Gemini
            self.scrivi_log("Connessione a Gemini in corso... Attendi.")
            client = genai.Client(api_key=KEY)
            numero_tentativi = 0

            while numero_tentativi < 3:
                try:
                    prompt = (
                        "Sei un Senior Software Engineer universale. Analizza i file seguenti.\n\n"
                        "CONTESTO DEL PROGETTO:\n"
                        f"{codice_contesto}\n\n"
                        "FILE DA ANALIZZARE E CORREGGERE (TARGET):\n"
                        f"{codice_target}\n\n"
                        "Istruzioni TASSATIVE per la tua risposta:\n"
                        "1. Identifica il linguaggio di programmazione.\n"
                        "2. Scrivi una sezione \"## ANALISI DELL'ERRORE\" spiegando in modo conciso (massimo 3-4 righe) qual è il problema e perché è sbagliato. Non fare ragionamenti lunghi.\n"
                        "3. Fornisci la sezione \"## CODICE CORRETTO\" in un blocco markdown.\n"
                        "4. Fornisci la sezione \"## UNIT TEST\" scrivendo il codice di test nel framework standard di quel linguaggio, racchiuso in un singolo blocco markdown.\n"
                        "5. SUBITO DOPO l'ultimo blocco di codice, fornisci queste DUE righe esatte per permettermi di eseguire il test automaticamente:\n"
                        "   TEST_FILE_NAME: [nome_del_file_di_test_con_estensione_corretta]\n"
                        "   RUN_COMMAND: [comando_da_terminale_per_eseguire_il_test]\n"
                    )
                    
                    response = client.models.generate_content(
                        model="gemini-2.5-flash", 
                        contents=prompt
                    )
                    
                    self.scrivi_log("✅ Risposta ricevuta dall'IA! Salvataggio report...")
                    
                    with open("REPORT_AGENTE_IA.md", "w", encoding="utf-8") as report:
                        report.write(response.text)

                    # 6. Esecuzione Test
                    test_file_match = re.search(r"TEST_FILE_NAME:\s*(\S+)", response.text)
                    run_command_match = re.search(r"RUN_COMMAND:\s*(.*)", response.text)

                    if test_file_match and run_command_match:
                        test_file_name = test_file_match.group(1).strip()
                        run_command = run_command_match.group(1).strip()

                        blocchi_codice = re.findall(r"```[^\n]*\n(.*?)\n```", response.text, re.DOTALL)
                        
                        if blocchi_codice:
                            codice_test = blocchi_codice[-1]
                            
                            with open(test_file_name, "w", encoding="utf-8") as f:
                                f.write(codice_test)
                            
                            self.scrivi_log(f"Esecuzione dinamica test: {run_command} ...")
                            
                            risultato = subprocess.run(run_command, shell=True, capture_output=True, text=True)
                            
                            stato_str = "✅ PASSATO" if risultato.returncode == 0 else "❌ FALLITO"
                            self.scrivi_log(f"Esito Test: {stato_str}")
                            
                            esito_test = (
                                "\n## ESITO ESECUZIONE TEST AUTOMATIZZATA\n"
                                f"**Comando eseguito:** `{run_command}`\n"
                                f"**Stato:** {stato_str}\n\n"
                                "### Output del Terminale:\n"
                                "```text\n"
                                f"{risultato.stdout.strip()}\n"
                                "```\n"
                            )

                            with open("REPORT_AGENTE_IA.md", "a", encoding="utf-8") as report:
                                report.write(esito_test)

                    self.scrivi_log("🚀 Analisi completata al 100%. Leggi il file REPORT_AGENTE_IA.md")
                    break # Esce dal while se tutto va a buon fine

                except Exception as e:
                    self.scrivi_log(f"⚠️ [Tentativo {numero_tentativi + 1} fallito] Errore API: {e}")
                    numero_tentativi += 1

            if numero_tentativi == 3:
                self.scrivi_log("❌ Impossibile completare l'analisi dopo 3 tentativi.")

        except Exception as strano_errore:
            self.scrivi_log(f"❌ Errore critico nel sistema: {strano_errore}")
            
        finally:
            # Riabilita sempre il pulsante alla fine, anche se ci sono stati errori
            self.btn_avvia.configure(state="normal", text="2. Avvia Analisi IA")

# --- AVVIO DELL'APP ---
if __name__ == "__main__":
    app = AgenteIAApp()
    app.mainloop()
import os
import subprocess
from google import genai

# --- 1. FUNZIONE PER LEGGERE IL TESTO DENTRO I FILE ---
def leggi_contenuto_file(lista_nomi):
    testo_accumulato = ""
    for nome in lista_nomi:
        # Controllo di sicurezza: se il file non esiste o è una cartella, saltalo
        if not os.path.exists(nome) or os.path.isdir(nome):
            continue
        try:
            # Apre il file in modalità lettura ('r') con codifica UTF-8
            with open(nome, "r", encoding="utf-8") as f:
                testo_accumulato += f"\n\n--- INIZIO FILE: {nome} ---\n"
                testo_accumulato += f.read() # Legge tutto il testo
                testo_accumulato += f"\n--- FINE FILE: {nome} ---\n"
        except Exception:
            continue # Se c'è un errore (es. file binario), salta silenziosamente
    return testo_accumulato

# --- 2. FUNZIONE PER CHIEDERE A GIT I FILE MODIFICATI NELL'ULTIMO COMMIT ---
def rileva_nomi_modificati():
    try:
        # Comando Git per ottenere solo i nomi dei file modificati nell'ultimo commit (HEAD)
        comando = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']
        # Esegue il comando e cattura l'output del terminale come testo
        risultato = subprocess.run(comando, capture_output=True, text=True, check=True)
        # Pulisce il testo e lo divide in una lista, una riga per file
        nomi = risultato.stdout.strip().split('\n')
        # Rimuove eventuali righe vuote e normalizza i percorsi (es. trasforma / in \ su Windows)
        return [os.path.normpath(n) for n in nomi if n.strip()]
    except Exception as e:
        print(f"Errore Git: {e}")
        return []

# --- 3. LOGICA PRINCIPALE (IL CUORE) ---

# A. Recuperiamo i nomi dei file cambiati (TARGET)
nomi_cambiati = rileva_nomi_modificati()

if not nomi_cambiati:
    print("Nessun file modificato trovato nell'ultimo commit. Termino.")
    exit(0)

print(f"File target rilevati: {nomi_cambiati}")

# B. Recuperiamo TUTTI i file nella cartella (per il CONTESTO)
tutti_i_nomi = []
for root, dirs, files in os.walk("."):
    # Ignora le cartelle di sistema e se stesso
    dirs[:] = [d for d in dirs if not d.startswith('.git')]
    for file in files:
        if file == "agente_ia.py":
            continue
        percorso = os.path.relpath(os.path.join(root, file), ".")
        tutti_i_nomi.append(percorso)

# C. Separiamo i nomi: Target (modificati) e Contesto (gli altri)
nomi_contesto = [n for n in tutti_i_nomi if n not in nomi_cambiati]

# D. LEGGIAMO il contenuto vero e proprio dei file (Ecco il collegamento che mancava!)
codice_target = leggi_contenuto_file(nomi_cambiati)
codice_contesto = leggi_contenuto_file(nomi_contesto)

# --- 4. INVIO A GEMINI ---
KEY = os.getenv('GOOGLE_API_KEY')
if not KEY:
    print("Errore: La chiave API 'GOOGLE_API_KEY' non è stata trovata nelle variabili d'ambiente!")
    exit(1)

client = genai.Client(api_key=KEY)
numero_tentativi = 0

while numero_tentativi < 3:
    try:
        # --- MODIFICA DEL PROMPT PER MOSTRARE IL RAGIONAMENTO ---
        prompt = f"""
        Sei un Senior Software Engineer esperto e rigoroso. Il tuo compito è analizzare il codice fornito per trovare errori, proporre refactoring e scrivere test.

        Istruzioni TASSATIVE per la tua risposta:
        1. Inizia con una sezione chiamata "## RAGIONAMENTO APPROFONDITO".
        2. In questa sezione, mostra PASSO DOPO PASSO il tuo processo mentale di analisi. Spiega cosa stai guardando, quali dipendenze verifichi nel contesto, e quali potenziali problemi stai ipotizzando.
        3. Solo DOPO aver concluso il ragionamento, fornisci le sezioni "## CORREZIONI PROPOSTE" e "## UNIT TEST".

        ---
        CONTESTO DEL PROGETTO (usalo solo per capire le dipendenze, NON modificarlo):
        {codice_contesto}
        
        ---
        FILE DA ANALIZZARE E CORREGGERE (TARGET) - Concentrati ESCLUSIVAMENTE qui:
        {codice_target}
        """
        
        # Chiamata all'API utilizzando il modello Flash per velocità
        response = client.models.generate_content(
            model="gemini-2.5-flash", 
            contents=prompt
        )
        
        # Stampiamo il risultato completo nei log (che includerà il ragionamento)
        print("\n" + "="*60)
        print("RISPOSTA DELL'AGENTE IA (Incluso Ragionamento):")
        print("="*60 + "\n")
        print(response.text)
        
        # SALVIAMO il report completo in un file Markdown per la tesi
        with open("REPORT_AGENTE_IA.md", "w", encoding="utf-8") as report:
            report.write(response.text)
            
        print("\nAnalisi completata con successo! Report salvato in 'REPORT_AGENTE_IA.md'.")
        break # Se tutto va bene, esce dal ciclo di tentativi

    except Exception as e:
        print(f"Tentativo {numero_tentativi+1} fallito a causa di un errore API: {e}")
        numero_tentativi += 1

if numero_tentativi >= 3:
    print("\nL'IA non è riuscita a rispondere dopo 3 tentativi. Controlla la connessione o la chiave API.")
import json
import queue
import threading
import time
from pathlib import Path

from google import genai

from .config import API_TIMEOUT_SECONDS, LANGUAGE_NAMES, SOURCE_EXTENSIONS


class GenAIClient:
    MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    def analyze_code(
        self,
        target_file: Path,
        source_code: str,
        context_code: str = "",
        error_feedback: str = "",
    ) -> str:
        prompt = self._build_prompt(target_file, source_code, context_code, error_feedback)

        for model_name in self.MODELS:
            print(f"[API] Connessione al modello: {model_name}")
            for attempt in range(2):
                try:
                    response = self._generate_content(model_name, prompt)
                    raw_text = (response.text or "").strip()
                    cleaned = self._clean_json_response(raw_text)
                    return cleaned
                except ValueError:
                    raise
                except Exception as exc:
                    err = str(exc)
                    if isinstance(exc, TimeoutError):
                        print(f"[API] Timeout su {model_name} dopo {API_TIMEOUT_SECONDS} secondi.")
                        break
                    if "404" in err:
                        print(f"[API] Modello non disponibile: {model_name}.")
                        break
                    if any(marker in err for marker in ("503", "UNAVAILABLE", "429")):
                        if attempt == 0:
                            print("[API] Servizio temporaneamente saturo. Riprovo tra 5 secondi.")
                            time.sleep(5)
                            continue
                        print(f"[API] Modello non raggiungibile: {model_name}.")
                        break

                    print(f"[API] Errore su {model_name}: {exc}")
                    break

        raise RuntimeError(
            "Nessun modello Gemini disponibile. Controllare connessione e API key."
        )

    @staticmethod
    def _clean_json_response(raw_text: str) -> str:
        cleaned = raw_text.strip()

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

        try:
            json.loads(cleaned)
            return cleaned
        except json.JSONDecodeError as first_error:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and start < end:
                candidate = cleaned[start : end + 1].strip()
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError as second_error:
                    raise ValueError(
                        "Risposta AI non è JSON valido dopo pulizia: "
                        + str(second_error)
                    ) from second_error

            raise ValueError(
                "Risposta AI non è JSON valido dopo pulizia: "
                + str(first_error)
            ) from first_error

    def _generate_content(self, model_name: str, prompt: str):
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                result_queue.put(("ok", response))
            except Exception as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            status, payload = result_queue.get(timeout=API_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise TimeoutError from exc

        if status == "error":
            raise payload
        return payload

    @staticmethod
    def _build_prompt(
        target_file: Path,
        source_code: str,
        context_code: str,
        error_feedback: str,
    ) -> str:
        target_ext = target_file.suffix.lower()
        is_python = target_ext == ".py"
        language_name = LANGUAGE_NAMES.get(target_ext, "linguaggio del file target")
        supported_note = (
            "Estensione supportata dal runner locale."
            if target_ext in SOURCE_EXTENSIONS
            else "Estensione non presente tra quelle supportate dal runner locale."
        )

        patch_rules = [
            "1. Proponi la modifica minima necessaria per correggere il bug osservato.",
            "2. Non aggiungere nuove feature, nuovi campi di stato o nuovi concetti di dominio.",
            "3. Non introdurre logiche temporali, calendario o reset giornaliero se non sono gia presenti nel codice originale.",
            "4. I test devono verificare solo il comportamento documentato dal codice target, non requisiti inventati.",
            "5. Non rimuovere campi, attributi o variabili di stato gia presenti se partecipano al comportamento esistente.",
        ]
        if is_python:
            patch_rules.append(
                "6. Per Python, non rimuovere attributi self.* gia presenti: se esiste self.withdrawn_today, preservalo e usalo per la validazione cumulativa."
            )

        test_rules = [
            "1. Niente framework esterni, database o servizi remoti.",
            "2. Il test deve essere flat e autonomo, scritto nel linguaggio piu adatto al file target.",
            "3. Il test deve stampare una riga leggibile per ogni caso verificato:",
            "   [PASS] <descrizione del caso>",
            "   [FAIL] <descrizione del caso>: <motivo>",
            "4. L'output deve includere sia i casi superati sia quelli falliti, non solo gli errori.",
            "5. Alla fine il test deve stampare esattamente queste metriche:",
            "   Passed: <numero>",
            "   Failed: <numero>",
            "6. Il test deve terminare con exit code 0 se passa e non-zero se fallisce.",
            "7. Per verificare eccezioni o errori usa costrutti espliciti del linguaggio target.",
            "8. Non inventare requisiti non presenti nel codice o nella docstring/commenti del file target.",
            "9. Non ridefinire nel test classi, funzioni o tipi gia presenti nel file target: usa direttamente il codice reale da validare.",
            "10. Non impostare manualmente i contatori finali: Passed e Failed devono derivare dai casi eseguiti.",
            "11. Non usare frasi o commenti come manual analysis, assumo, simuliamo il conteggio o valori manuali.",
            "12. Non usare operatori di shell come &&, || o ; nel campo run_command: indica un comando diretto e semplice.",
        ]
        if is_python:
            test_rules.extend(
                [
                    "13. Vincoli specifici Python: niente unittest, pytest, classi di test o decorator.",
                    "14. Vincoli specifici Python: non usare assert dentro lambda; in Python e SyntaxError.",
                    "15. Vincoli specifici Python: non catturare o sostituire sys.stdout e non usare StringIO; stampa direttamente su console.",
                    "16. Vincoli specifici Python: non mescolare argomenti posizionali dopo keyword argument.",
                    "17. Non proporre comandi distruttivi o comandi che non eseguono il test.",
                ]
            )
        else:
            test_rules.append("13. Non proporre comandi distruttivi o comandi che non eseguono il test.")
            if target_ext == ".java":
                test_rules.extend(
                    [
                        "14. Vincoli specifici Java: la classe di test deve contenere una sola public static void main(String[] args).",
                        "15. Vincoli specifici Java: non duplicare il metodo main e non inserire codice fuori dalla classe pubblica.",
                    ]
                )

        prompt = (
            "Sei un revisore automatico di codice per un progetto universitario.\n"
            "Analizza solo difetti logici, bug reali e casi limite rilevanti. "
            "Ignora stile, formattazione e preferenze personali.\n\n"
            f"FILE TARGET: {target_file}\n"
            f"LINGUAGGIO TARGET: {language_name}\n"
            f"SUPPORTO LOCALE: {supported_note}\n"
            f"CODICE TARGET:\n{source_code}\n\n"
            f"CONTESTO ARCHITETTURALE:\n{context_code}\n\n"
            "Vincoli sulla patch:\n"
            + "\n".join(patch_rules)
            + "\n\n"
            "Vincoli sui test:\n"
            + "\n".join(test_rules)
            + "\n\n"
            "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido. "
            "Nessun testo prima o dopo. Nessun blocco markdown. Nessun backtick. "
            "La struttura deve essere esattamente questa:\n"
            "{\n"
            '  "has_bug": true,\n'
            '  "analysis": "spiegazione del bug o conferma che non ce ne sono",\n'
            '  "fixed_code": "intero file corretto come stringa, oppure null",\n'
            '  "test_code": "codice del test come stringa",\n'
            '  "test_file_name": "nome_file_test.estensione",\n'
            '  "run_command": "comando diretto senza operatori shell"\n'
            "}\n"
            "Il campo has_bug deve essere true se trovi un bug, altrimenti false.\n"
            "Se has_bug è false, fixed_code deve essere null.\n"
            "Se has_bug è true, fixed_code deve contenere il file completo corretto.\n"
        )

        if error_feedback:
            prompt += (
                "\n\n[FEEDBACK ESECUZIONE PRECEDENTE]\n"
                "Il test precedente non e stato eseguibile o non ha rispettato il formato.\n"
                "Correggi risposta, test o patch mantenendo i vincoli sopra.\n"
                f"Output ricevuto:\n```\n{error_feedback}\n```\n"
            )

        return prompt

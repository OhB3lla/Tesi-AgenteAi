# File: codice_con_errore.py

def calcola_media_voti(lista_voti):
    """
    Calcola la media aritmetica di una lista di numeri.
    Contiene un bug: non gestisce il caso di lista vuota.
    """
    somma_totale = sum(lista_voti)
    
    # ERRORE LOGICO: Se lista_voti è [], lista_voti.length (o len() in Python) è 0.
    # Questo provocherà un ZeroDivisionError.
    media = somma_totale / len(lista_voti)
    #con print(f"Somma totale: {somma_totale}, Numero di voti: {len(lista_voti)}, Media: {media}")
    return media

# Esempio d'uso che funziona
voti_classe = [28, 30, 24, 27]
print(f"Media voti della classe: {calcola_media_voti(voti_classe)}")

# Esempio d'uso che farà crashare il programma (se scommentato)
# voti_vuoti = []
# print(f"Media voti vuoti: {calcola_media_voti(voti_vuoti)}")
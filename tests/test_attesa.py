"""Prove del cervello del popup: la coda delle richieste di attesa.

Tutto a orologio finto (`adesso=`) e senza toccare lo schermo: qui si verifica
la regola di cosa mostrare, non il disegno di una finestra.
"""
from myagents.attesa import Coda, chiave, TIPI


def test_chiave_distingue_richieste_diverse():
    # Due richieste diverse non devono mai avere la stessa chiave, o rispondere
    # a una risponderebbe all'altra.
    a = chiave("domanda", "sess-1", "tool_use_A")
    b = chiave("domanda", "sess-1", "tool_use_B")
    c = chiave("permesso", "sess-1", "tool_use_A")
    assert a != b and b != c and a != c


def test_chiave_stabile_per_stessa_richiesta():
    assert chiave("domanda", "s", "x") == chiave("domanda", "s", "x")


def test_apri_e_mostra():
    q = Coda()
    q.apri("domanda", "s1", "u1", testo="Continuo?", cwd="/p", tty="ttys001",
           adesso=100)
    fuori = q.da_mostrare(adesso=101)
    assert len(fuori) == 1
    assert fuori[0]["testo"] == "Continuo?"


def test_riaprire_non_duplica():
    q = Coda()
    q.apri("domanda", "s1", "u1", adesso=100)
    q.apri("domanda", "s1", "u1", adesso=105)  # stessa chiave
    assert len(q.tutte()) == 1


def test_chiudi_per_chiave():
    q = Coda()
    k = q.apri("domanda", "s1", "u1", adesso=100)
    assert q.chiudi(k) == 1
    assert q.da_mostrare(adesso=101) == []


def test_chiudi_per_sessione_toglie_tutte():
    # Quando l'utente risponde nel terminale (o la sessione finisce), spariscono
    # tutte le richieste di quella sessione insieme.
    q = Coda()
    q.apri("domanda", "s1", "u1", adesso=100)
    q.apri("permesso", "s1", "u2", adesso=100)
    q.apri("domanda", "s2", "u3", adesso=100)
    assert q.chiudi("s1") == 2
    resta = q.da_mostrare(adesso=101)
    assert len(resta) == 1 and resta[0]["session_id"] == "s2"


def test_kill_switch_non_mostra_niente():
    q = Coda()
    q.apri("permesso", "s1", "u1", adesso=100)
    assert q.da_mostrare(disabilitato=True, adesso=101) == []


def test_terminale_gia_in_primo_piano_non_si_mostra():
    # Se stai gia' guardando quel terminale, un popup sopra sarebbe rumore.
    q = Coda()
    q.apri("domanda", "s1", "u1", tty="ttys001", adesso=100)
    assert q.da_mostrare(in_primo_piano="ttys001", adesso=101) == []
    # ma un'altra sessione, su un altro tty, si mostra lo stesso
    q.apri("domanda", "s2", "u2", tty="ttys005", adesso=100)
    fuori = q.da_mostrare(in_primo_piano="ttys001", adesso=101)
    assert len(fuori) == 1 and fuori[0]["tty"] == "ttys005"


def test_silenzio_tutto():
    q = Coda()
    q.apri("domanda", "s1", "u1", adesso=100)
    q.silenzia("tutto", 60, adesso=100)
    assert q.da_mostrare(adesso=130) == []       # ancora zitto
    assert len(q.da_mostrare(adesso=161)) == 1   # silenzio finito


def test_silenzio_per_sessione_non_tocca_le_altre():
    q = Coda()
    q.apri("domanda", "s1", "u1", adesso=100)
    q.apri("domanda", "s2", "u2", adesso=100)
    q.silenzia("sessione:s1", 60, adesso=100)
    fuori = q.da_mostrare(adesso=110)
    assert len(fuori) == 1 and fuori[0]["session_id"] == "s2"


def test_ordine_per_urgenza():
    # Un permesso blocca piu' di una domanda: va mostrato prima.
    q = Coda()
    q.apri("domanda", "s1", "u1", adesso=100)
    q.apri("permesso", "s2", "u2", adesso=101)
    fuori = q.da_mostrare(adesso=110)
    assert fuori[0]["tipo"] == "permesso"
    assert TIPI.index(fuori[0]["tipo"]) < TIPI.index(fuori[1]["tipo"])


def test_scadenza_toglie_le_vecchie():
    q = Coda()
    q.apri("domanda", "s1", "u1", adesso=100)
    # oltre la scadenza senza altri segnali: sparisce da sola
    assert q.da_mostrare(adesso=100 + 1801) == []


def test_tipo_sconosciuto_diventa_domanda():
    q = Coda()
    q.apri("qualcosa-di-strano", "s1", "u1", adesso=100)
    assert q.tutte()[0]["tipo"] == "domanda"

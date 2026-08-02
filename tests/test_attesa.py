"""Prove del cervello del popup: la coda delle richieste di attesa.

Tutto a orologio finto (`adesso=`) e senza toccare lo schermo: qui si verifica
la regola di cosa mostrare, non il disegno di una finestra.
"""
from myagents.attesa import Coda, chiave, TIPI, da_notification, da_permission


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


# -- da_notification: mappa il payload REALE dell'evento Notification ----------

def test_da_notification_payload_reale():
    # Esattamente la forma misurata dalla sonda il 2026-08-02.
    p = {"notification_type": "idle_prompt",
         "message": "Claude is waiting for your input",
         "session_id": "abc", "cwd": "/p", "prompt_id": "pid-1"}
    arg = da_notification(p)
    assert arg["tipo"] == "inattivo"
    assert arg["session_id"] == "abc"
    assert arg["dettaglio"] == "pid-1"       # prompt_id, in assenza di tool_use_id
    assert arg["testo"] == "Claude is waiting for your input"


def test_da_notification_tool_use_id_ha_precedenza():
    p = {"notification_type": "permission_prompt", "session_id": "s",
         "tool_use_id": "tu-9", "prompt_id": "pid-9"}
    arg = da_notification(p)
    assert arg["tipo"] == "permesso"
    assert arg["dettaglio"] == "tu-9"        # tool_use_id vince su prompt_id


def test_da_notification_senza_sessione_e_none():
    # Senza session_id non si sa a chi appartiene: niente richiesta.
    assert da_notification({"notification_type": "idle_prompt"}) is None


def test_da_notification_robusta_a_spazzatura():
    # Un payload malformato non deve sollevare, ma ritornare None o una richiesta
    # tracciata -- mai un'eccezione che risalga fino a rompere qualcosa.
    for schifo in (None, [], "stringa", 42, {"message": "senza sessione"}):
        assert da_notification(schifo) is None


def test_da_notification_tipo_ignoto_diventa_domanda():
    arg = da_notification({"notification_type": "cosa_nuova_2027", "session_id": "s"})
    assert arg["tipo"] == "domanda"          # tipo nuovo: tracciato, non perso


# -- da_permission: la domanda con le opzioni dal payload PermissionRequest -----

def test_da_permission_askuserquestion():
    p = {"tool_name": "AskUserQuestion", "session_id": "s", "cwd": "/p",
         "tool_input": {"questions": [{"question": "Continuo?", "header": "X",
             "options": [{"label": "Sì", "description": "a"},
                         {"label": "No", "description": "b"}]}]}}
    arg = da_permission(p)
    assert arg["tipo"] == "domanda"
    assert arg["testo"] == "Continuo?"
    assert arg["opzioni"] == ["Sì", "No"]


def test_da_permission_ignora_i_tool_che_non_chiedono():
    # Un Bash/Edit non chiede niente all'utente: niente popup.
    for tool in ("Bash", "Edit", "Read", "Write"):
        assert da_permission({"tool_name": tool, "session_id": "s",
                              "tool_input": {"command": "ls"}}) is None


def test_da_permission_robusta_a_questions_malformate():
    # tool_input senza questions, o questions vuote: non deve sollevare.
    assert da_permission({"tool_name": "AskUserQuestion", "session_id": "s",
                          "tool_input": {}})["opzioni"] == []
    assert da_permission({"tool_name": "AskUserQuestion", "session_id": "s"})[
        "opzioni"] == []


def test_da_permission_senza_sessione_e_none():
    assert da_permission({"tool_name": "AskUserQuestion",
                          "tool_input": {"questions": []}}) is None

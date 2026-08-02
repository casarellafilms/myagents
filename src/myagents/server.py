"""Servizio locale: tiene aggiornato il database e serve la barra e la dashboard.

Solo libreria standard: nessun framework web. E' una pagina locale per un solo
utente sul suo Mac -- `http.server` basta e avanza, e ogni dipendenza in meno
e' un modo in meno di rompersi fra sei mesi.

Non e' mai nel percorso critico di una sessione: se questo processo muore, gli
hook continuano a scrivere sullo spool e non si perde niente. Al massimo la
barra mostra dati vecchi, ed e' progettata per dirtelo invece di fingere.

Ascolta solo su 127.0.0.1: nessuna esposizione fuori dalla macchina.
"""
import datetime
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import attesa, terminali, workflows
from .curator import cura_in_coda, cura_profonda_tutti
from .drain import drain
from .render import _e_rumore
from .paths import (DB_PATH, ERROR_LOG, SPOOL_DIR, is_disabled, popup_attivo,
                    token_locale, utcnow)
from .schema import connect, migrate
from .verify import riverifica_tutto

PORTA = 7777
INTERVALLO_TRAVASO = 20   # secondi
# Il segretario costa una chiamata per sessione: si fa con calma, poche per
# volta, e mai mentre la cattura e' in pausa.
INTERVALLO_CURA = 180
# Con una coda lunga il ritmo normale e' troppo lento: undici sessioni in
# attesa a due ogni tre minuti sono venti minuti, e nel frattempo la dashboard
# mostra progetti senza task senza spiegare perche'.
CURA_A_RAFFICA = 6
INTERVALLO_AVVISI = 900          # 15 minuti
GIORNI_PRIMA_DI_AVVISARE = 2
# Ogni quanto si ricontrolla quali terminali sono aperti e dove. Piu' fitto di
# cosi' non serve: le finestre non si aprono e chiudono dieci volte al minuto.
INTERVALLO_TERMINALI = 8
# Un progetto fermo da piu' di tanti giorni e' "inattivo": la dashboard lo mette
# da parte, cosi' la vista principale resta su cio' su cui lavori davvero.
GIORNI_INATTIVO = 14
# Ogni quanto si rieseguono i comandi di verifica salvati. Dieci minuti: e' il
# meccanismo che chiude i task da solo, e non deve pesare -- ogni giro esegue
# davvero dei comandi, quindi va tenuto raro e a lotti piccoli.
INTERVALLO_RIVERIFICA = 600

# Cosa e' gia' stato notificato, per non ripetersi a ogni giro: una notifica
# che torna ogni quarto d'ora si impara a ignorare, e allora tanto vale non
# averla.
_gia_avvisato: set = set()


def _notifica(titolo: str, testo: str) -> None:
    """Notifica di sistema. Mai solleva: e' un extra, non un servizio."""
    try:
        script = (f'display notification {json.dumps(testo)} '
                  f'with title "myagents" subtitle {json.dumps(titolo)}')
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _avvisa_ogni_tanto() -> None:
    """Segnala solo due cose, entrambe silenziose per natura.

    1. Un task che Claude ha dichiarato fatto e che nessuno ha mai verificato,
       fermo da giorni. Senza avviso resterebbe li' per sempre.
    2. Una verifica scaduta: era verde, il codice e' cambiato dopo. E' il caso
       piu' insidioso, perche' il sistema sembra dirti che va tutto bene.
    """
    from .verify import scadute
    while True:
        time.sleep(INTERVALLO_AVVISI)
        try:
            if is_disabled():
                continue
            conn = connect()
            migrate(conn)
            for chiave, titolo in conn.execute(
                    "SELECT task_key, title FROM tasks WHERE status='claimed'"
                    " AND claimed_at IS NOT NULL"
                    " AND julianday('now') - julianday(claimed_at) >= ?",
                    (GIORNI_PRIMA_DI_AVVISARE,)):
                if ("claimed", chiave) not in _gia_avvisato:
                    _gia_avvisato.add(("claimed", chiave))
                    _notifica("Dichiarato fatto, mai verificato", titolo)
            for t in scadute(conn):
                chiave = t["task_key"]
                if ("scaduta", chiave) not in _gia_avvisato:
                    _gia_avvisato.add(("scaduta", chiave))
                    _notifica("Verifica scaduta: il codice e' cambiato", t["titolo"])
        except Exception:
            pass  # gli avvisi sono un extra: non devono mai fermare il servizio

_ultimo_travaso = {"quando": 0.0, "applicati": 0, "errore": ""}


def _travasa_ogni_tanto() -> None:
    """Il travaso periodico e' cio' che rende la barra viva.

    Senza, i dati si aggiornerebbero solo quando lanci `tk drain` a mano, e una
    barra che mostra numeri fermi e' peggio di nessuna barra: sembra dirti che
    non sta succedendo niente.
    """
    while True:
        try:
            if not is_disabled():
                n = drain()
                _ultimo_travaso.update(quando=time.time(), applicati=n, errore="")
        except Exception as exc:  # il servizio non deve mai morire per un travaso
            _ultimo_travaso.update(quando=time.time(),
                                   errore=f"{type(exc).__name__}: {exc}")
        time.sleep(INTERVALLO_TRAVASO)


# Stato dell'ultimo giro del revisore. Esiste perche' un processo che gira da
# solo ogni tre minuti e non lascia traccia e' indistinguibile da un processo
# rotto: si vedono i task comparire, o non comparire, e in nessuno dei due casi
# si sa perche'. Qui restano l'ora, l'esito e l'eventuale errore.
_ultima_cura = {"quando": 0.0, "esiti": [], "errore": "", "in_corso": False,
                "prossimo": 0.0, "giri": 0}
# Un solo revisore per volta: il pulsante della dashboard e il giro automatico
# possono capitare insieme, e curare due volte la stessa sessione sprecherebbe
# una chiamata (l'idempotenza la scarterebbe, ma il costo e' gia' stato pagato).
_lucchetto_cura = threading.Lock()


def cura_adesso(massimo: int | None = None) -> dict:
    """Fa girare il revisore subito. E' il pulsante, ed e' anche `tk cura`.

    Ritorna cosa ha fatto, non solo se e' andata: "0 task da 3 sessioni" e "non
    e' partito" sono due situazioni diverse che senza questo dato sembrano
    identiche dall'esterno.
    """
    if is_disabled():
        return {"ok": False, "errore": "la cattura e' in pausa"}
    if not _lucchetto_cura.acquire(blocking=False):
        return {"ok": False, "errore": "il revisore sta gia' girando"}
    _ultima_cura["in_corso"] = True
    try:
        conn = connect()
        migrate(conn)
        arretrato = conn.execute("SELECT COUNT(*) FROM curation_queue").fetchone()[0]
        if massimo is None:
            massimo = CURA_A_RAFFICA if arretrato > 4 else 2
        esiti = cura_in_coda(conn, massimo=massimo)
        _ultima_cura.update(quando=time.time(), esiti=esiti, errore="",
                            giri=_ultima_cura["giri"] + 1)
        return {"ok": True, "esiti": esiti, "restano_in_coda": conn.execute(
            "SELECT COUNT(*) FROM curation_queue").fetchone()[0]}
    except Exception as exc:
        _ultima_cura.update(quando=time.time(), errore=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "errore": f"{type(exc).__name__}: {exc}"}
    finally:
        _ultima_cura["in_corso"] = False
        _lucchetto_cura.release()


def _cura_ogni_tanto() -> None:
    while True:
        _ultima_cura["prossimo"] = time.time() + INTERVALLO_CURA
        time.sleep(INTERVALLO_CURA)
        try:
            cura_adesso()
        except Exception:
            pass  # il revisore e' un extra: non deve mai fermare il servizio


# Stato del revisore profondo, per la barra e la dashboard: quando ha girato
# l'ultima volta, cosa ne e' uscito, e se sta girando ora.
_ultima_profonda = {"quando": 0.0, "esiti": [], "in_corso": False, "prossimo": 0.0}
_lucchetto_profonda = threading.Lock()
# Piu' raro della cura di sessione: passa in rassegna interi progetti, e
# l'arretrato non cambia ogni minuto.
INTERVALLO_PROFONDA = 900


def profonda_adesso() -> dict:
    """Fa girare il revisore profondo subito. E' il pulsante e il giro periodico."""
    if is_disabled():
        return {"ok": False, "errore": "la cattura e' in pausa"}
    if not _lucchetto_profonda.acquire(blocking=False):
        return {"ok": False, "errore": "il revisore profondo sta gia' girando"}
    _ultima_profonda["in_corso"] = True
    try:
        conn = connect(); migrate(conn)
        esiti = cura_profonda_tutti(conn, massimo=3)
        _ultima_profonda.update(quando=time.time(), esiti=esiti)
        return {"ok": True, "esiti": esiti}
    except Exception as exc:
        return {"ok": False, "errore": f"{type(exc).__name__}: {exc}"}
    finally:
        _ultima_profonda["in_corso"] = False
        _lucchetto_profonda.release()


def _profonda_ogni_tanto() -> None:
    while True:
        _ultima_profonda["prossimo"] = time.time() + INTERVALLO_PROFONDA
        time.sleep(INTERVALLO_PROFONDA)
        try:
            profonda_adesso()
        except Exception:
            pass  # il revisore profondo e' un extra: mai fermare il servizio


_ultima_riverifica = {"quando": 0.0, "chiusi": [], "riaperti": [], "esaminati": 0}


def riverifica_adesso(massimo: int = 6) -> dict:
    """Riesegue i comandi di verifica noti e aggiorna gli stati."""
    if is_disabled():
        return {"ok": False, "errore": "la cattura e' in pausa"}
    conn = connect()
    migrate(conn)
    esito = riverifica_tutto(conn, massimo=massimo)
    _ultima_riverifica.update(quando=time.time(), **esito)
    for chiave in esito.get("riaperti", []):
        # Un verde che smette di esserlo e' la cosa piu' importante che questo
        # sistema possa dirti: si dice subito, non alla prossima occhiata.
        _notifica("Verifica caduta", f"{chiave}: il comando non passa piu'")
    return {"ok": True, **esito}


def _riverifica_ogni_tanto() -> None:
    """La via d'uscita automatica dei task.

    Senza questo giro, l'unico modo di chiudere un task era lanciare `tk verify`
    a mano -- e non lo lancia nessuno. I task entravano e non uscivano, la lista
    cresceva, e una lista che cresce e non cala si smette di guardare.
    Qui non decide nessun modello: si riesegue un comando gia' scritto e si
    legge il codice di uscita.
    """
    while True:
        time.sleep(INTERVALLO_RIVERIFICA)
        try:
            if not is_disabled():
                riverifica_adesso()
        except Exception:
            pass  # mai fermare il servizio per una riverifica


_pannelli_vivi: list = []
_workflow_vivi: list = []


# Le richieste di attesa che il popup deve mostrare ORA, ricalcolate dal thread
# e lette dagli endpoint senza toccare la coda a ogni richiesta.
_attesa_da_mostrare: list = []
_ciottolo_pid = None
_tty_focus_cache = {"tty": "", "quando": 0.0}
# Un'attesa deve durare almeno questo prima di comparire. Un agente che si ferma
# un istante e riprende non merita un popup: senza questa maturazione ogni pausa
# di un secondo faceva lampeggiare la finestra. Il popup e' per quando sei via,
# non per un respiro dell'agente.
MATURA_DOPO = 3.0


def _calcola_attesa() -> list:
    """Le richieste da mostrare adesso: mature, e non su un terminale in focus.

    L'osascript del focus si interroga SOLO se c'e' gia' qualcosa di maturo da
    mostrare. Con popup spento o coda vuota non si tocca osascript -- chiamarlo
    ogni secondo era inutile, e la maturazione elimina il flapping alla radice:
    un'attesa che si risolve in meno di MATURA_DOPO non appare mai.
    """
    if not popup_attivo():
        return []
    adesso = time.time()
    mature = [r for r in _coda_attesa.da_mostrare(is_disabled())
              if adesso - r.get("aperta_a", adesso) >= MATURA_DOPO]
    if not mature:
        return []
    focus = _tty_focus()
    return [r for r in mature if not (focus and r.get("tty") == focus)]


def _tty_focus() -> str:
    """Il tty del terminale in primo piano, con una cache breve.

    Ogni lettura e' un paio di osascript: chiamarli a ogni rinfresco (che scatta
    su ogni richiesta di attesa) e ogni secondo dal thread sarebbe troppo. Due
    secondi di cache: il terminale che hai davanti non cambia dieci volte al
    secondo, e un popup soppresso mezzo secondo in ritardo non e' un problema.
    """
    adesso = time.time()
    if adesso - _tty_focus_cache["quando"] < 2.0:
        return _tty_focus_cache["tty"]
    try:
        tty = terminali.tty_in_primo_piano()
    except Exception:
        tty = ""
    _tty_focus_cache.update(tty=tty, quando=adesso)
    return tty
# Il check-and-spawn del Ciottolo va serializzato: lo chiamano insieme il thread
# _guarda_attesa (ogni 1s) e ogni worker HTTP che serve una attesa/apri via
# _rinfresca_attesa. Senza lock, due che vedono `_ciottolo_pid is None` insieme
# lanciano DUE Ciottoli, e l'ultimo pid scritto vince: il primo processo resta
# non tracciato e non piu' terminabile -- il processo orfano che questo file
# giura di non lasciare.
_lucchetto_ciottolo = threading.Lock()


def _assicura_ciottolo() -> None:
    """Fa in modo che ci sia UN Ciottolo vivo, e uno solo.

    Se ne lancia un altro solo quando il precedente e' morto (coda svuotata,
    watchdog, crash): tenerne il pid ed evitare i doppioni e' cio' che tiene la
    scrivania pulita. Il Ciottolo poi si chiude da solo quando non c'e' piu'
    niente -- il servizio non deve inseguirlo per spegnerlo.
    """
    global _ciottolo_pid
    with _lucchetto_ciottolo:
        if _ciottolo_pid is not None:
            try:
                os.kill(_ciottolo_pid, 0)
                return  # ancora vivo
            except OSError:
                _ciottolo_pid = None
        repo = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        python = os.path.join(repo, "venv", "bin", "python")
        src = os.path.join(repo, "src")
        try:
            p = subprocess.Popen(
                [python, "-c", f"import sys;sys.path.insert(0,{src!r});"
                               "from myagents.ciottolo import main;main()"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            _ciottolo_pid = p.pid
        except OSError:
            pass


def _guarda_attesa() -> None:
    """Tiene aggiornato cosa il popup deve mostrare, e lo apre quando serve.

    Il calcolo sta qui e non negli endpoint perche' la finestra interroga
    /api/attesa spesso: la risposta dev'essere una copia gia' pronta, non un
    ricalcolo a ogni richiesta.
    """
    # Lo stesso lavoro che fa _rinfresca_attesa dopo ogni apri/chiudi: qui a
    # ritmo fisso, cosi' anche senza eventi (una richiesta che matura, il focus
    # che cambia) la copia resta fresca. Un solo posto in cui vive la logica.
    while True:
        _rinfresca_attesa()
        time.sleep(1.0)


def _workflow_correnti() -> list:
    """L'ultimo quadro noto delle squadre di agenti.

    Si serve la copia calcolata dal thread, non un ricalcolo: leggere le
    cartelle dei workflow costa, e /api/stato viene chiamato ogni pochi secondi
    da barra e dashboard insieme.
    """
    return list(_workflow_vivi)


def _guarda_i_terminali() -> None:
    """Tiene aggiornato l'elenco dei terminali aperti, fuori dalle richieste.

    Rilevarli costa mezzo secondo (`ps`, un `lsof`, un `osascript` per app):
    poco in assoluto, troppo da pagare dentro /api/stato, che la barra chiama
    ogni dieci secondi. Aggiornandolo qui, la risposta resta istantanea e il
    costo se lo prende un thread che nessuno sta aspettando.
    """
    global _pannelli_vivi, _workflow_vivi
    while True:
        try:
            _pannelli_vivi = terminali.pannelli(usa_cache=False)
        except Exception:
            pass  # sapere dov'e' un terminale e' un lusso, non un servizio
        try:
            _workflow_vivi = workflows.elenco(usa_cache=False)
        except Exception:
            pass
        time.sleep(INTERVALLO_TERMINALI)


def _nome_alias(config_dir: str) -> str:
    """Da ~/.claude-lavoro al nome dell'alias che usi: claude-lavoro."""
    nome = str(config_dir or "").rstrip("/").split("/")[-1]
    return nome[1:] if nome.startswith(".") else (nome or "claude")


def _ultima_richiesta(conn, pid: int) -> str:
    """L'ultima cosa che l'utente ha davvero chiesto in questo progetto.

    Filtra il macchinario che passa dallo stesso canale: notifiche interne,
    riassunti automatici di altri plugin, blocchi di tool-use. Senza filtro la
    scheda mostrava "<task-notification> <tool-use-id>toolu_01KP..." come se
    fosse una tua richiesta.
    """
    for (testo,) in conn.execute(
            "SELECT p.text FROM prompts p WHERE p.project_id = ?"
            " AND EXISTS (SELECT 1 FROM activity a WHERE a.session_id = p.session_id)"
            " ORDER BY p.id DESC LIMIT 12", (pid,)):
        if not _e_rumore(testo):
            return " ".join(str(testo).split())[:150]
    return ""


def stato() -> dict:
    """Tutto cio' che serve alla barra e alla dashboard."""
    conn = connect()
    migrate(conn)
    progetti = []
    righe = conn.execute("""
        SELECT p.id, p.key,
               (SELECT COUNT(*) FROM tasks t
                 WHERE t.project_id = p.id AND t.status IN ('open','in_progress')),
               (SELECT COUNT(*) FROM tasks t
                 WHERE t.project_id = p.id AND t.status = 'claimed'),
               (SELECT COUNT(*) FROM activity a WHERE a.project_id = p.id),
               (SELECT MAX(a.ts) FROM activity a WHERE a.project_id = p.id),
               (SELECT ps.injection FROM project_state ps WHERE ps.project_id = p.id),
               p.root_path
          FROM projects p ORDER BY p.key
    """).fetchall()
    for pid, key, aperti, claimed, attivita, ultima, iniezione, radice in righe:
        # Un progetto senza attivita' non e' un progetto su cui stai lavorando:
        # tenerlo nell'elenco riempirebbe la barra di rumore.
        if not attivita:
            continue
        # Salute del progetto a colpo d'occhio, e se e' fermo da un pezzo.
        verdi = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id=?"
                             " AND status='verified'", (pid,)).fetchone()[0]
        giorni = None
        if ultima:
            try:
                giorni = (datetime.datetime.utcnow() - datetime.datetime.strptime(
                    ultima, "%Y-%m-%dT%H:%M:%SZ")).days
            except (ValueError, TypeError):
                giorni = None
        progetti.append({
            "id": pid, "key": key, "aperti": aperti, "claimed": claimed,
            "verdi": verdi, "giorni_fermo": giorni,
            "inattivo": bool(giorni is not None and giorni >= GIORNI_INATTIVO),
            "attivita": attivita, "ultima": ultima, "radice": radice,
            "nota": iniezione or "",
            "task": [
                {"key": k, "titolo": t, "stato": s}
                for k, t, s in conn.execute(
                    "SELECT task_key,title,status FROM tasks WHERE project_id = ?"
                    " AND status != 'archived' ORDER BY"
                    " CASE status WHEN 'claimed' THEN 0 WHEN 'in_progress' THEN 1"
                    "   WHEN 'open' THEN 2 ELSE 3 END, last_touched_at DESC LIMIT 8",
                    (pid,))
            ],
            # Da quale alias di Claude Code e' arrivato questo lavoro. L'utente
            # ne usa tre (claude, claude-lavoro, claude-cliente) e sapere quale
            # ha visto cosa e' parte di cosa vuole vedere.
            "alias": [
                _nome_alias(r[0]) for r in conn.execute(
                    "SELECT DISTINCT config_dir FROM sessions WHERE project_id = ?"
                    " AND config_dir IS NOT NULL AND config_dir != ''", (pid,))
            ],
            "richiesta": _ultima_richiesta(conn, pid),
            # Quante sessioni di questo progetto aspettano l'analisi. Senza
            # questo numero un progetto senza task sembra un difetto.
            "in_coda": conn.execute(
                "SELECT COUNT(*) FROM curation_queue q JOIN sessions s"
                " ON s.id = q.session_id WHERE s.project_id = ?", (pid,)).fetchone()[0],
            "file": [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT target FROM activity WHERE project_id = ?"
                    " AND tool IN ('Edit','Write','MultiEdit') AND target IS NOT NULL"
                    " ORDER BY ts DESC LIMIT 6", (pid,))
            ],
            # I terminali aperti dentro questo progetto in questo momento.
            # E' l'unico dato dell'intera risposta che non viene dal database:
            # non e' storia, e' cosa c'e' acceso adesso sulla scrivania.
            "terminali": terminali.per_cartella(radice, _pannelli_vivi),
        })
    try:
        errori = ERROR_LOG.stat().st_size if ERROR_LOG.is_file() else 0
    except OSError:
        errori = 0
    return {
        "progetti": progetti,
        "aperti": sum(p["aperti"] for p in progetti),
        "claimed": sum(p["claimed"] for p in progetti),
        "spool": len(list(SPOOL_DIR.glob("*.jsonl"))) if SPOOL_DIR.is_dir() else 0,
        "in_coda": conn.execute("SELECT COUNT(*) FROM curation_queue").fetchone()[0],
        "spento": is_disabled(),
        "errori": errori,
        "db": str(DB_PATH),
        "ultimo_travaso": dict(_ultimo_travaso),
        # Quando il revisore ha girato, cosa ne e' uscito, e quando rigira.
        # Senza questi tre numeri "non compaiono task" ha due spiegazioni
        # indistinguibili: non c'era niente da estrarre, oppure e' rotto.
        "revisore": {**_ultima_cura, "ogni": INTERVALLO_CURA},
        "profonda": {**_ultima_profonda, "ogni": INTERVALLO_PROFONDA},
        "riverifica": {**_ultima_riverifica, "ogni": INTERVALLO_RIVERIFICA},
        "terminali": list(_pannelli_vivi),
        # Le squadre di agenti che stanno girando. Mentre lavorano non si vede
        # niente: si sa solo che qualcosa e' partito. Qui si vede chi c'e'
        # dentro, con un nome, e chi ha gia' finito.
        "workflow": _workflow_correnti(),
        "adesso": time.time(),
    }


# La coda delle richieste di attesa (il cervello del popup). Vive qui, in
# memoria: se il servizio muore la coda muore con lui, ed e' corretto -- una
# richiesta di attesa e' vera solo finche' la sessione e il servizio sono vivi.
_coda_attesa = attesa.Coda()

# Le azioni che parlano a una sessione o al popup dell'utente. Oltre all'origine
# locale richiedono il token segreto (~/.myagents/token): una pagina web non lo
# ha, un altro processo locale qualunque nemmeno. Le azioni sui task (conferma,
# archivia) NON sono qui: bastano dell'origine, e chiederle protette
# complicherebbe la dashboard senza aggiungere sicurezza reale.
AZIONI_PROTETTE = {"attesa/apri", "attesa/chiudi", "attesa/silenzio"}


def _rinfresca_attesa() -> None:
    """Ricalcola subito cosa mostrare, invece di aspettare il giro del thread.

    Senza, fra l'arrivo di una richiesta e la sua comparsa passava fino a un
    secondo intero: troppo, per qualcosa che nasce per avvisarti in tempo. Dopo
    ogni apri/chiudi/silenzio la copia e' aggiornata all'istante, e la finestra
    la vede al suo prossimo poll.
    """
    global _attesa_da_mostrare
    try:
        _attesa_da_mostrare = _calcola_attesa()
        if _attesa_da_mostrare and popup_attivo() and not is_disabled():
            _assicura_ciottolo()
    except Exception:
        pass


def _azione_attesa(comando: str, dati: dict) -> dict:
    """I comandi del popup. Separati da azione() perche' hanno regole loro."""
    if comando == "attesa/apri":
        # Un PermissionRequest (ha tool_name) porta la domanda con le opzioni;
        # una Notification porta solo il messaggio. Si prova prima il primo.
        arg = attesa.da_permission(dati) if dati.get("tool_name") else None
        if not arg:
            arg = attesa.da_notification(dati)
        if not arg:
            return {"ok": False, "errore": "notifica non interpretabile"}
        tty = ""
        # Si arricchisce con il tty del terminale della sessione, se lo si
        # conosce: e' cio' che permette al popup di NON mostrarsi quando quel
        # terminale e' gia' davanti, e di portartici con un clic.
        for p in _pannelli_vivi:
            if p.get("cwd") and arg.get("cwd") and p["cwd"] == arg["cwd"]:
                tty = p.get("tty", "")
                break
        k = _coda_attesa.apri(
            arg["tipo"], arg["session_id"], arg["dettaglio"],
            testo=arg["testo"], opzioni=arg.get("opzioni"),
            cwd=arg["cwd"], tty=tty)
        _rinfresca_attesa()
        return {"ok": True, "chiave": k}

    if comando == "attesa/chiudi":
        bersaglio = (dati or {}).get("chiave") or (dati or {}).get("session_id")
        if not bersaglio:
            return {"ok": False, "errore": "niente da chiudere"}
        n = _coda_attesa.chiudi(bersaglio)
        _rinfresca_attesa()
        return {"ok": True, "chiuse": n}

    if comando == "attesa/silenzio":
        ambito = (dati or {}).get("ambito") or "tutto"
        secondi = float((dati or {}).get("secondi") or 900)
        _coda_attesa.silenzia(ambito, secondi)
        _rinfresca_attesa()
        return {"ok": True, "ambito": ambito, "secondi": secondi}

    return {"ok": False, "errore": "comando di attesa sconosciuto"}


def azione(comando: str, dati: dict) -> dict:
    if comando.startswith("attesa/"):
        return _azione_attesa(comando, dati)

    """Le poche azioni che la dashboard puo' compiere.

    Volutamente poche: il database e' l'unica fonte di verita' e la dashboard
    non deve diventare un secondo posto dove la logica vive.
    """
    conn = connect()
    migrate(conn)
    task_key = (dati or {}).get("task_key")
    adesso = utcnow()

    if comando == "conferma" and task_key:
        # Conferma umana: l'unico modo per cui un task diventa 'verified' senza
        # una prova raccolta dall'harness. Resta tracciato come tale grazie a
        # evidence.source='user' -- non si confonde con cio' che il sistema ha visto.
        riga = conn.execute("SELECT id FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        if not riga:
            return {"ok": False, "errore": "task inesistente"}
        conn.execute(
            "UPDATE tasks SET status='verified', verified_at=?, updated_at=?"
            " WHERE task_key=?", (adesso, adesso, task_key))
        conn.execute(
            "INSERT INTO evidence (task_id,kind,payload,source,ts) VALUES (?,?,?,?,?)",
            (riga[0], "manual_confirm", "{}", "user", adesso))
        return {"ok": True}

    if comando == "archivia" and task_key:
        conn.execute("UPDATE tasks SET status='archived', updated_at=? WHERE task_key=?",
                     (adesso, task_key))
        return {"ok": True}

    if comando == "riapri" and task_key:
        conn.execute(
            "UPDATE tasks SET status='open', verified_at=NULL, claimed_at=NULL,"
            " updated_at=? WHERE task_key=?", (adesso, task_key))
        return {"ok": True}

    if comando == "travasa":
        return {"ok": True, "applicati": drain()}

    if comando == "riverifica":
        return riverifica_adesso()

    if comando == "profonda":
        return profonda_adesso()

    if comando == "cura":
        # Il revisore a richiesta: aspettare tre minuti per sapere se funziona
        # e' il motivo per cui non si controlla mai.
        return cura_adesso(massimo=(dati or {}).get("massimo"))

    if comando == "terminale":
        # Torna al terminale gia' aperto su quel progetto, o ne apre uno.
        return terminali.apri_o_torna((dati or {}).get("radice") or "")

    return {"ok": False, "errore": "comando sconosciuto"}


class _Handler(BaseHTTPRequestHandler):
    def _path_pulito(self) -> bool:
        """Rifiuta i path con '..': non aggirano nulla oggi (il comando
        risultante non matcha nessuna azione), ma un domani in cui si aggiunge
        un comando potrebbero, e costa una riga chiuderli adesso."""
        return ".." not in self.path

    def _e_locale(self) -> bool:
        """Solo la dashboard servita da questo processo puo' agire.

        Ascoltare su 127.0.0.1 NON basta, ed e' un errore facile da fare. Una
        pagina web qualunque, aperta nel browser mentre il servizio gira, puo'
        mandare qui un POST con Content-Type text/plain: e' una "richiesta
        semplice", quindi il browser non fa nessun preflight CORS e la manda.
        Il risultato e' che un sito puo' marcare `verified` un task che nessuno
        ha verificato, archiviarne altri, o far aprire un terminale su un
        percorso che sceglie lui.
        Sarebbe la beffa peggiore per questo progetto: esiste per distinguere
        cio' che e' stato dimostrato da cio' che e' stato dichiarato, e una
        pagina qualsiasi potrebbe dichiarare.

        Host validato chiude anche il DNS rebinding. Origin assente significa
        client non-browser (la barra, curl): il browser lo manda sempre sulle
        richieste cross-site, quindi la sua assenza non e' falsificabile da una
        pagina.
        """
        porta = self.server.server_port
        if (self.headers.get("Host") or "") not in (f"127.0.0.1:{porta}",
                                                    f"localhost:{porta}"):
            return False
        return self.headers.get("Origin") in (
            None, f"http://127.0.0.1:{porta}", f"http://localhost:{porta}")

    def _vietato(self) -> None:
        corpo = b'{"ok": false, "errore": "origine non ammessa"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _rispondi(self, corpo: bytes, tipo: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", tipo + "; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):  # noqa: N802 (nome imposto da http.server)
        try:
            if not self._e_locale() or not self._path_pulito():
                return self._vietato()
            if self.path.startswith("/api/attesa"):
                # Endpoint leggero: la finestra lo interroga spesso, quindi non
                # deve toccare il database. Ritorna la copia gia' calcolata dal
                # watchdog, non un ricalcolo.
                self._rispondi(json.dumps(
                    {"richieste": list(_attesa_da_mostrare)},
                    ensure_ascii=False).encode("utf-8"))
            elif self.path.startswith("/api/stato"):
                self._rispondi(json.dumps(stato(), ensure_ascii=False).encode("utf-8"))
            else:
                from .dashboard import PAGINA
                self._rispondi(PAGINA.encode("utf-8"), "text/html")
        except Exception as exc:
            self._rispondi(json.dumps(
                {"errore": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ).encode("utf-8"))

    def do_POST(self):  # noqa: N802
        try:
            # QUI serve piu' che su do_GET, ed era l'unico posto in cui mancava.
            # Un POST con Content-Type text/plain e' una "richiesta semplice":
            # nessun preflight, quindi qualunque pagina aperta nel browser lo
            # spedisce. Verificato dal vivo: un POST con Origin falso ha
            # archiviato un task davvero, e la risposta e' stata {"ok": true}.
            if not self._e_locale() or not self._path_pulito():
                return self._vietato()
            # Comando a piu' livelli: /api/attesa/apri -> "attesa/apri",
            # /api/conferma -> "conferma".
            comando = self.path.split("/api/", 1)[-1].strip("/")
            # Le azioni che parlano a una sessione o al popup esigono il token
            # segreto, che il browser non possiede. Ferma un altro processo
            # locale qualunque, non solo il browser.
            #
            # `not token` e' load-bearing: se il token non e' ottenibile (file
            # vuoto, illeggibile, crash fra creazione e scrittura) token_locale()
            # ritorna "", e senza questo controllo un header vuoto matcherebbe
            # "" e passerebbe -- il gate si aprirebbe proprio quando dovrebbe
            # chiudersi. Fail-closed: niente token, niente azione.
            # compare_digest per non perdere tempo-costante sul confronto.
            import hmac
            token = token_locale()
            if comando in AZIONI_PROTETTE and (
                    not token or not hmac.compare_digest(
                        token, self.headers.get("X-Myagents-Token") or "")):
                return self._vietato()
            lunghezza = int(self.headers.get("Content-Length") or 0)
            dati = json.loads(self.rfile.read(lunghezza) or b"{}")
            self._rispondi(json.dumps(azione(comando, dati),
                                      ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            self._rispondi(json.dumps(
                {"ok": False, "errore": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False).encode("utf-8"))

    def log_message(self, *_args):
        pass  # niente rumore sul terminale


def avvia(porta: int = PORTA, in_background: bool = True) -> ThreadingHTTPServer:
    threading.Thread(target=_travasa_ogni_tanto, daemon=True).start()
    threading.Thread(target=_cura_ogni_tanto, daemon=True).start()
    threading.Thread(target=_profonda_ogni_tanto, daemon=True).start()
    threading.Thread(target=_avvisa_ogni_tanto, daemon=True).start()
    threading.Thread(target=_guarda_i_terminali, daemon=True).start()
    threading.Thread(target=_riverifica_ogni_tanto, daemon=True).start()
    threading.Thread(target=_guarda_attesa, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", porta), _Handler)
    if in_background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    else:
        httpd.serve_forever()
    return httpd

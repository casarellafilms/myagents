"""I workflow di agenti: quali girano, chi ci lavora, a che punto sono.

Un workflow e' una squadra di agenti che lavorano in parallelo su pezzi diversi
di uno stesso problema. Mentre gira non si vede niente: finisce, e arriva un
risultato. Se ci mette dieci minuti, per dieci minuti non sai se sta procedendo,
quanti agenti hai in volo, o se uno e' morto.

Questo modulo legge cio' che l'harness lascia su disco e ne ricava un quadro.

COSA C'E' SU DISCO, misurato il 2026-08-02:

    <config>/projects/<progetto>/<sessione>/subagents/workflows/wf_<id>/
        journal.jsonl                    una riga per evento
        agent-<id>.jsonl                 il dialogo completo di quell'agente
        agent-<id>.meta.json             {"agentType": ..., "spawnDepth": 1}

Il journal porta due tipi di riga, e nient'altro:

    {"type":"started","key":"v2:<hash>","agentId":"aa04b34e3f2f19282"}
    {"type":"result", "key":"v2:<hash>","agentId":"...","result":"..."}

Da qui si ricava tutto lo stato: un agente con `started` e senza `result` sta
ancora lavorando; con entrambi ha finito.

I NOMI. Il journal NON contiene le etichette assegnate nello script: restano
solo identificatori esadecimali, che a schermo non dicono niente. Il nome si
ricava allora dal prompt dell'agente, che sta nel suo transcript. E' una scelta
che vale per qualunque workflow, anche scritto da altri, senza dipendere da come
e' stato etichettato -- e un elenco di agenti chiamati "aa04b34e" non e' un
elenco, e' un enigma.

COSTO. Si leggono i primi byte di ogni transcript, non i file interi: quelli
arrivano a mezzo megabyte l'uno. Il resto e' `stat` sulle cartelle.
"""
import json
import time
from pathlib import Path

# Quanti byte leggere dalla testa di un transcript per trovarci il prompt. Il
# primo record e' il messaggio di apertura: 64 kB lo contengono sempre, e i file
# interi (mezzo megabyte l'uno, decine per workflow) non vanno letti.
_TESTA = 64 * 1024
_MAX_RUN = 12
_MAX_NOME = 52
_CACHE_SECONDI = 2
# Dopo quanti secondi di silenzio un agente avviato e mai concluso si considera
# fermo. Un agente al lavoro scrive sul suo transcript di continuo -- anche solo
# per pensare -- quindi qualche minuto di immobilita' e' gia' molto.
SILENZIO_MASSIMO = 240
_cache: tuple = (0.0, [])


def _config_dirs() -> list:
    """Le config dir di Claude Code presenti nella home."""
    fuori = []
    try:
        for cartella in sorted(Path.home().glob(".claude*")):
            if cartella.is_dir() and (cartella / "projects").is_dir():
                fuori.append(cartella)
    except OSError:
        pass
    return fuori


def _cartelle_run() -> list:
    """Tutte le cartelle wf_* trovate, dalla piu' recente."""
    trovate = []
    for config in _config_dirs():
        try:
            for run in (config / "projects").glob("*/*/subagents/workflows/wf_*"):
                if not run.is_dir():
                    continue
                try:
                    trovate.append((run.stat().st_mtime, run))
                except OSError:
                    continue
        except OSError:
            continue
    trovate.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in trovate[:_MAX_RUN]]


# I prompt ben scritti dichiarano il compito dopo un'etichetta. Cercarla da' un
# nome che descrive cosa fa quell'agente, invece di una riga qualsiasi presa
# dal corpo del testo -- che e' come si finiva con nomi tipo
# "titolo: verifica_scaduta: falso positivo garantito, ...".
_ETICHETTE = ("OGGETTO:", "COMPITO:", "LENTE:", "DIREZIONE ASSEGNATA:",
              "CRITERIO:", "REPERTO da attaccare:", "TASK:", "OBIETTIVO:")


def _candidati(testo: str) -> list:
    """Le righe di un prompt che potrebbero fare da nome, in ordine di merito.

    Un prompt comincia quasi sempre con un cappello identico per tutta la
    squadra ("Sei un revisore avversariale", "REGOLA ASSOLUTA:"). Prenderne la
    prima riga darebbe a cinque agenti lo stesso nome, e un nome che non
    distingue non e' un nome. Si cercano prima le etichette esplicite, poi si
    ripiega sul corpo; la scelta finale la fa chi vede tutti i fratelli.
    """
    grezzo = testo or ""
    marcate = []
    for etichetta in _ETICHETTE:
        posizione = grezzo.find(etichetta)
        while posizione >= 0:
            resto = grezzo[posizione + len(etichetta):]
            # Il valore puo' stare sulla stessa riga o su quella dopo.
            for pezzo in resto.split("\n", 3)[:2]:
                valore = " ".join(pezzo.split())
                if 6 < len(valore) < 300:
                    marcate.append(valore)
                    break
            posizione = grezzo.find(etichetta, posizione + 1)

    generiche = ("sei un", "sei il", "sei uno", "you are", "il tuo compito",
                 "regola assoluta", "progetto:", "contesto:", "vincoli",
                 "leggi ", "indaga", "rispondi", "marca ")
    righe = [" ".join(r.split()) for r in grezzo.splitlines()]
    righe = [r for r in righe
             if 14 < len(r) < 400 and not r.startswith(("#", "```", "---", "-", "*"))]
    preferite = [r for r in righe if not r.lower().startswith(generiche)]
    return (marcate + preferite + righe)[:14]


def _nome_da_prompt(percorso: Path) -> list:
    """I candidati-nome di un agente, letti dal suo prompt."""
    try:
        with open(percorso, encoding="utf-8", errors="replace") as fh:
            testa = fh.read(_TESTA)
    except OSError:
        return []
    for riga in testa.splitlines():
        if '"type"' not in riga:
            continue
        try:
            dato = json.loads(riga)
        except ValueError:
            continue  # il primo record puo' superare la testa letta: si prosegue
        if not isinstance(dato, dict) or dato.get("type") != "user":
            continue
        contenuto = (dato.get("message") or {}).get("content")
        if isinstance(contenuto, list):
            contenuto = " ".join(
                b.get("text", "") for b in contenuto
                if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(contenuto, str) or not contenuto.strip():
            continue
        candidati = _candidati(contenuto)
        if candidati:
            return candidati
    return []


def _accorcia(testo: str) -> str:
    testo = " ".join(str(testo or "").split())
    return testo[:_MAX_NOME] + ("…" if len(testo) > _MAX_NOME else "")


def _battezza(candidati_per_agente: list) -> list:
    """Un nome distinto per ogni agente della stessa squadra.

    Si scorre in parallelo l'elenco dei candidati: al primo giro ognuno propone
    la sua riga migliore; chi propone una riga che nessun altro ha se la tiene,
    chi collide passa alla successiva. Cosi' il nome finale e' la prima cosa che
    quell'agente ha e i suoi fratelli no -- che e' esattamente cio' che serve
    per distinguerli a colpo d'occhio.
    """
    nomi = [None] * len(candidati_per_agente)
    presi: set = set()

    # Quante volte ogni riga compare nella squadra: quelle che compaiono in piu'
    # di un agente sono il cappello comune e non distinguono nessuno.
    quante: dict = {}
    for candidati in candidati_per_agente:
        for riga in set(candidati):
            quante[riga] = quante.get(riga, 0) + 1

    # Primo passaggio: la prima riga che l'agente NON condivide con nessuno.
    for i, candidati in enumerate(candidati_per_agente):
        for riga in candidati:
            if quante.get(riga, 0) == 1:
                testo = _accorcia(riga)
                if testo not in presi:
                    nomi[i] = testo
                    presi.add(testo)
                    break

    # Secondo passaggio, per chi e' rimasto: la prima riga ancora libera, anche
    # se condivisa. Meglio un nome imperfetto ma diverso che un numero.
    for i, candidati in enumerate(candidati_per_agente):
        if nomi[i] is not None:
            continue
        for riga in candidati:
            testo = _accorcia(riga)
            if testo not in presi:
                nomi[i] = testo
                presi.add(testo)
                break
    # Chi resta senza (prompt identici davvero) prende un numero: meglio
    # "agente 3" di tre voci con la stessa etichetta.
    for i, nome in enumerate(nomi):
        if nome is None:
            base = candidati_per_agente[i]
            nomi[i] = (f"{_accorcia(base[0])} · {i + 1}" if base
                       else f"agente {i + 1}")
    return nomi


def _journal(run: Path) -> tuple:
    """(avviati, conclusi): gli agentId, nell'ordine in cui sono partiti."""
    avviati, conclusi = [], set()
    try:
        with open(run / "journal.jsonl", encoding="utf-8", errors="replace") as fh:
            for riga in fh:
                try:
                    dato = json.loads(riga)
                except ValueError:
                    continue
                if not isinstance(dato, dict):
                    continue
                aid = dato.get("agentId")
                if not aid:
                    continue
                if dato.get("type") == "started":
                    if aid not in avviati:
                        avviati.append(aid)  # lista: l'ordine e' quello di avvio
                elif dato.get("type") == "result":
                    conclusi.add(aid)
    except OSError:
        pass
    return avviati, conclusi


def _campo(testo: str, chiave: str) -> str:
    """Il valore di `chiave: '...'` in un blocco meta, quale che sia l'apice."""
    for apice in ("'", '"', "`"):
        marca = f"{chiave}: {apice}"
        if marca in testo:
            return testo.split(marca, 1)[1].split(apice, 1)[0].strip()
    return ""


def _meta(run: Path) -> dict:
    """Nome, scopo e fasi dichiarati dal workflow nel proprio script.

    Ogni script si apre con un blocco `meta` che dice come si chiama, a cosa
    serve e in quali fasi si articola. E' l'unico posto in cui lo SCOPO e'
    scritto: senza, una scheda mostra dei pallini che si muovono e non spiega
    perche' esistano. Gli script stanno in una cartella sorella, col run id nel
    nome del file.
    """
    fuori = {"nome": run.name, "scopo": "", "fasi": []}
    try:
        scripts = run.parents[2] / "workflows" / "scripts"
        for candidato in scripts.glob(f"*{run.name}*.js"):
            testa = candidato.read_text(encoding="utf-8", errors="replace")[:6000]
            testa = testa.split("phases:", 1)
            intestazione = testa[0]
            fuori["nome"] = _campo(intestazione, "name") or run.name
            fuori["scopo"] = _campo(intestazione, "description")
            if len(testa) > 1:
                # Le fasi sono `{ title: '...', detail: '...' }` in fila.
                blocco = testa[1].split("]", 1)[0]
                for pezzo in blocco.split("{")[1:]:
                    titolo = _campo(pezzo, "title")
                    if titolo:
                        fuori["fasi"].append(
                            {"titolo": titolo, "dettaglio": _campo(pezzo, "detail")})
            return fuori
    except (OSError, IndexError):
        pass
    return fuori


def elenco(usa_cache: bool = True) -> list:
    """I workflow trovati, dal piu' recente, con i loro agenti.

    Ogni voce: {id, nome, stato, avviati, conclusi, quando, agenti[]}.
    Ogni agente: {id, nome, stato, ultimo_segno}.
    """
    global _cache
    adesso = time.time()
    if usa_cache and _cache[1] and (adesso - _cache[0]) < _CACHE_SECONDI:
        return _cache[1]

    fuori = []
    for run in _cartelle_run():
        avviati, conclusi = _journal(run)
        if not avviati:
            continue
        candidati = [_nome_da_prompt(run / f"agent-{aid}.jsonl") for aid in avviati]
        nomi = _battezza(candidati)
        agenti = []
        adesso_s = time.time()
        for indice, aid in enumerate(avviati):
            try:
                ultimo = (run / f"agent-{aid}.jsonl").stat().st_mtime
            except OSError:
                ultimo = 0.0
            if aid in conclusi:
                stato = "finito"
            elif ultimo and (adesso_s - ultimo) > SILENZIO_MASSIMO:
                # Avviato, mai concluso, e il suo transcript non cresce piu' da
                # un pezzo. Un agente vivo scrive di continuo; questo o e' stato
                # fermato o e' morto. Chiamarlo "in corso" e' quello che faceva
                # apparire come attivo un workflow interrotto un'ora prima --
                # cioe' esattamente il genere di bugia che questo progetto
                # esiste per evitare.
                stato = "fermo"
            else:
                stato = "in corso"
            agenti.append({
                "id": aid[:8],
                "nome": nomi[indice] or f"agente {indice + 1}",
                "stato": stato,
                "ultimo_segno": ultimo,
            })
        try:
            quando = run.stat().st_mtime
        except OSError:
            quando = 0.0
        meta = _meta(run)
        if len(conclusi) >= len(avviati):
            stato_squadra = "finito"
        elif any(a["stato"] == "in corso" for a in agenti):
            stato_squadra = "in corso"
        else:
            # Nessuno ha finito e nessuno sta piu' scrivendo: la squadra e'
            # stata interrotta. Va detto, non lasciato credere che stia girando.
            stato_squadra = "interrotto"
        fuori.append({
            "id": run.name,
            "nome": meta["nome"],
            "scopo": meta["scopo"],
            "fasi": meta["fasi"],
            "stato": stato_squadra,
            "avviati": len(avviati),
            "conclusi": len(conclusi),
            "quando": quando,
            "agenti": agenti,
        })

    _cache = (adesso, fuori)
    return fuori


def in_corso() -> int:
    """Quanti workflow stanno girando adesso."""
    return sum(1 for w in elenco() if w["stato"] == "in corso")

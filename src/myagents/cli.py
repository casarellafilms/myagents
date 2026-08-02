"""Interfaccia da terminale."""
import argparse
import sys
import traceback

from . import install as installer
from .drain import drain
from .paths import DB_PATH, ERROR_LOG, SPOOL_DIR, is_disabled, utcnow
from .schema import connect, migrate
from .spool import spool_files

_ICON = {"verified": "*", "claimed": "!", "in_progress": ">", "open": "-",
         "archived": "x"}


def _log_error(exc: Exception) -> None:
    """Stessa disciplina append-and-never-raise di drain.py: un problema qui
    (disco pieno, permessi) non deve mai bloccare la CLI."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow()} cli: {type(exc).__name__}\n")
            fh.write(traceback.format_exc())
            fh.write("\n")
    except Exception:
        pass


def _cmd_drain(_args) -> int:
    print(f"eventi applicati: {drain()}")
    return 0


def _cmd_list(args) -> int:
    conn = connect()
    migrate(conn)
    sql = ("SELECT p.key, t.task_key, t.status, t.title FROM tasks t"
           " JOIN projects p ON p.id = t.project_id"
           " WHERE t.status != 'archived'")
    params: list = []
    if args.project:
        sql += " AND p.key = ?"
        params.append(args.project)
    sql += " ORDER BY p.key, t.id"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("nessun task")
        return 0
    for _key, task_key, status, title in rows:
        print(f"{_ICON.get(status, '?')} {task_key:<24} {status:<12} {title}")
    return 0


def _cmd_projects(_args) -> int:
    conn = connect()
    migrate(conn)
    rows = conn.execute(
        "SELECT p.key, COUNT(t.id) FROM projects p"
        " LEFT JOIN tasks t ON t.project_id = p.id AND t.status != 'archived'"
        " GROUP BY p.id ORDER BY p.key").fetchall()
    for key, count in rows:
        print(f"{key:<32} {count} task")
    return 0


def _cmd_doctor(_args) -> int:
    conn = connect()
    migrate(conn)
    tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    pending = len(spool_files())
    errors = ERROR_LOG.stat().st_size if ERROR_LOG.is_file() else 0
    print(f"database   : {DB_PATH} ({tasks} task)")
    print(f"spool      : {SPOOL_DIR} ({pending} file da drenare)")
    print(f"kill-switch: {'ATTIVO (cattura spenta)' if is_disabled() else 'spento'}")
    print(f"errori hook: {errors} byte in {ERROR_LOG}")
    return 0


def _cmd_verify(args) -> int:
    """Esegue il comando di verifica di un task. E' l'unico modo per il verde."""
    from .verify import verifica
    conn = connect(); migrate(conn)
    esito = verifica(conn, args.task, args.cmd or "", args.cwd or "")
    if not esito.get("ok"):
        print(f"errore: {esito.get('errore')}", file=sys.stderr)
        return 1
    if esito.get("verde"):
        print(f"* {args.task} VERIFICATO (uscita 0)")
        return 0
    print(f"! {args.task} non verificato (uscita {esito.get('uscita')})")
    if esito.get("stderr"):
        print("  " + esito["stderr"].strip().replace("\n", "\n  "))
    return 1


def _cmd_cura(args) -> int:
    """Fa girare il revisore adesso, e dice cosa ha fatto.

    Esiste perche' il revisore gira da solo ogni tre minuti dentro il servizio:
    comodo, ma invisibile. Se non produce task non si distingue "non c'era
    niente da estrarre" da "e' rotto" -- e la seconda e' gia' successa due
    volte, per un formato di risposta diverso da quello atteso.
    """
    from .server import cura_adesso
    esito = cura_adesso(massimo=args.massimo)
    if not esito.get("ok"):
        print(f"errore: {esito.get('errore')}", file=sys.stderr)
        return 1
    esiti = esito.get("esiti") or []
    if not esiti:
        print("nessuna sessione in coda: niente da rivedere")
        return 0
    for e in esiti:
        if e.get("saltata"):
            print(f"- {e.get('progetto') or '?':<28} saltata ({e.get('motivo')})")
        elif e.get("ok"):
            print(f"* {e.get('progetto'):<28} {e.get('task', 0)} task,"
                  f" {e.get('suggerimenti', 0)} suggerimenti")
        else:
            print(f"! {e.get('progetto') or '?':<28} "
                  f"{e.get('errore') or 'nessuna risposta utilizzabile'}")
    print(f"\n{len(esiti)} sessioni riviste · "
          f"{esito.get('restano_in_coda', 0)} ancora in coda")
    return 0


def _cmd_profonda(_args) -> int:
    """Il revisore profondo: rivaluta l'arretrato dei task contro i commit git.

    Gira gia' da solo nel servizio ogni 15 minuti; questo comando lo forza ora.
    """
    from .server import profonda_adesso
    esito = profonda_adesso()
    if not esito.get("ok"):
        print(f"errore: {esito.get('errore')}", file=sys.stderr)
        return 1
    for e in esito.get("esiti", []):
        if e.get("ok"):
            print(f"* {e.get('esaminati', 0)} task esaminati · "
                  f"{e.get('claimed', 0)} → giallo · "
                  f"{e.get('verificabile', 0)} verificabili · "
                  f"{e.get('archivia', 0)} archiviati · {e.get('resta', 0)} restano")
        elif e.get("saltata"):
            print(f"- saltato: {e.get('motivo')}")
        else:
            print(f"! {e.get('errore')}")
    return 0


def _cmd_riverifica(args) -> int:
    """Riesegue i comandi di verifica salvati e aggiorna gli stati.

    E' la via d'uscita dei task: senza, l'unico modo di chiuderne uno era
    lanciare `tk verify` a mano, e la lista cresceva senza mai calare.
    """
    from .server import riverifica_adesso
    esito = riverifica_adesso(massimo=args.massimo)
    if not esito.get("ok"):
        print(f"errore: {esito.get('errore')}", file=sys.stderr)
        return 1
    for k in esito.get("chiusi", []):
        print(f"* {k:<32} VERIFICATO (il comando passa)")
    for k in esito.get("riaperti", []):
        print(f"! {k:<32} RIAPERTO (il comando non passa piu')")
    print(f"\n{esito.get('esaminati', 0)} esaminati · "
          f"{len(esito.get('chiusi', []))} chiusi · "
          f"{len(esito.get('riaperti', []))} riaperti")
    return 0


def _cmd_terminali(_args) -> int:
    """I terminali aperti e su quale cartella stanno. L'asterisco marca quelli
    con una sessione dell'agente dentro."""
    from .terminali import pannelli
    trovati = pannelli(usa_cache=False)
    if not trovati:
        print("nessun terminale rilevato")
        return 0
    for p in trovati:
        print(f"{'*' if p['claude'] else ' '} {p['tty']:<9} "
              f"{p['app'] or '?':<9} {p['cwd']}")
    print(f"\n{sum(1 for p in trovati if p['claude'])} con una sessione attiva"
          f" su {len(trovati)} terminali")
    return 0


def _cmd_dash(args) -> int:
    """Avvia il servizio locale: travaso periodico + dashboard + dati per la barra."""
    import webbrowser
    from . import server as srv
    print(f"taskdb in ascolto su http://127.0.0.1:{args.porta}")
    print("travaso automatico ogni", srv.INTERVALLO_TRAVASO, "secondi · ctrl-c per fermare")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.porta}")
    try:
        srv.avvia(args.porta, in_background=False)
    except KeyboardInterrupt:
        print("\nfermato")
    return 0


def _cmd_install(_args) -> int:
    installed = installer.install()
    installed_set = set(installed)

    for config_dir in installer.CONFIG_DIRS:
        config_dir = installer.Path(config_dir)
        settings_path = config_dir / "settings.json"

        if settings_path in installed_set:
            print(f"✓ installato in {config_dir}")
        elif not config_dir.is_dir():
            print(f"- non esiste {config_dir}")
        else:
            # Directory exists, settings.json exists but couldn't be processed
            print(f"⊘ saltato {config_dir} (settings.json non valido)")

    return 0


def _cmd_uninstall(_args) -> int:
    uninstalled = installer.uninstall()
    uninstalled_set = set(uninstalled)

    for config_dir in installer.CONFIG_DIRS:
        config_dir = installer.Path(config_dir)
        settings_path = config_dir / "settings.json"

        if settings_path in uninstalled_set:
            print(f"✓ rimosso da {config_dir}")
        elif not config_dir.is_dir():
            print(f"- non esiste {config_dir}")
        elif not settings_path.is_file():
            print(f"- nessun hook in {config_dir}")
        else:
            # settings.json exists but couldn't be processed
            print(f"⊘ saltato {config_dir} (settings.json non valido)")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tk")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="mostra i task")
    p_list.add_argument("--project")
    p_list.set_defaults(func=_cmd_list)

    sub.add_parser("projects", help="elenca i progetti").set_defaults(func=_cmd_projects)
    sub.add_parser("drain", help="applica lo spool al database").set_defaults(func=_cmd_drain)
    sub.add_parser("doctor", help="stato del sistema").set_defaults(func=_cmd_doctor)

    p_ver = sub.add_parser("verify", help="esegue la verifica di un task (l'unico modo per il verde)")
    p_ver.add_argument("task", help="chiave del task, es. progetto/T-0001")
    p_ver.add_argument("--cmd", help="comando da eseguire (si ricorda per le volte dopo)")
    p_ver.add_argument("--cwd", help="cartella in cui eseguirlo")
    p_ver.set_defaults(func=_cmd_verify)

    p_cura = sub.add_parser("cura", help="fa girare il revisore adesso")
    p_cura.add_argument("--massimo", type=int,
                        help="quante sessioni al massimo (default: decide da solo)")
    p_cura.set_defaults(func=_cmd_cura)

    sub.add_parser("profonda", help="rivaluta l'arretrato dei task contro i commit"
                   ).set_defaults(func=_cmd_profonda)

    p_riv = sub.add_parser("riverifica",
                           help="riesegue le verifiche note e aggiorna gli stati")
    p_riv.add_argument("--massimo", type=int, default=6)
    p_riv.set_defaults(func=_cmd_riverifica)

    sub.add_parser("terminali", help="i terminali aperti e dove stanno"
                   ).set_defaults(func=_cmd_terminali)

    p_dash = sub.add_parser("dash", help="servizio locale + dashboard nel browser")
    p_dash.add_argument("--porta", type=int, default=7777)
    p_dash.add_argument("--no-browser", action="store_true")
    p_dash.set_defaults(func=_cmd_dash)
    sub.add_parser("install", help="registra gli hook").set_defaults(func=_cmd_install)
    sub.add_parser("uninstall", help="rimuove gli hook").set_defaults(func=_cmd_uninstall)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "func", None):
        parser.print_usage()
        return 2

    try:
        return args.func(args)
    except Exception as exc:
        _log_error(exc)
        # Print short error message to stderr
        if "database" in str(exc).lower() or "file is not a database" in str(exc):
            print(f"errore: il database {DB_PATH} non è leggibile", file=sys.stderr)
        else:
            print(f"errore: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

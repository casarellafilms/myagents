"""La pallina nella barra del Mac.

Non ha una copia dei dati: interroga il servizio locale via HTTP. Se il servizio
non risponde mostra rosso e lo dice, invece di continuare a esibire numeri vecchi
come se fossero attuali -- un indicatore che mente e' peggio di un indicatore
assente, perche' smetti di controllarlo.

Non importa nessun modulo di taskdb tranne i percorsi: parla HTTP. Cosi' un suo
crash non puo' toccare ne' il database ne' la cattura.

L'icona E' l'informazione:
    ●      niente in sospeso, sei in pari
    ● 7    sette cose aperte, tutte tracciate
    ⚠ 3    tre dichiarate fatte da Claude senza uno straccio di prova
    ◌      cattura in pausa
    ⃠      servizio giu': la cattura continua, l'aggiornamento no

Il giallo e' il motivo per cui questo progetto esiste. Senza barra lo scopriresti
solo aprendo qualcosa; cosi' lo vedi mentre bevi il caffe'.
"""
import datetime
import json
import os
import subprocess
import urllib.error
import urllib.request

import rumps

from .paths import SPENTO

URL = "http://127.0.0.1:7777"
OGNI_SECONDI = 10
_MAX_TITOLO = 54


def _stato() -> dict | None:
    try:
        with urllib.request.urlopen(f"{URL}/api/stato", timeout=3) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _eta(iso) -> str:
    if not iso:
        return "mai"
    try:
        quando = datetime.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    minuti = int((datetime.datetime.utcnow() - quando).total_seconds() // 60)
    if minuti < 1:
        return "adesso"
    if minuti < 60:
        return f"{minuti} min fa"
    if minuti < 2880:
        return f"{minuti // 60} h fa"
    return f"{minuti // 1440} gg fa"


def _breve(percorso: str, radice: str) -> str:
    """Percorso leggibile: relativo al progetto, o gli ultimi due pezzi."""
    p = str(percorso or "")
    if radice and p.startswith(radice):
        return p[len(radice):].lstrip("/") or os.path.basename(p)
    pezzi = [x for x in p.split("/") if x]
    return "…/" + "/".join(pezzi[-2:]) if len(pezzi) > 2 else p


class Barra(rumps.App):
    def __init__(self):
        super().__init__("myagents", title="●", quit_button=None)
        self._costruisci(None)
        rumps.Timer(self.aggiorna, OGNI_SECONDI).start()

    # -- costruzione ---------------------------------------------------------

    def _costruisci(self, d) -> None:
        voci = []

        if d is None:
            self.title = "⃠"
            voci += [
                rumps.MenuItem("Servizio non attivo"),
                rumps.MenuItem("   La cattura continua: nulla va perso"),
                rumps.separator,
                rumps.MenuItem("Avvia il servizio", callback=self.avvia_servizio),
            ]
        else:
            voci += self._intestazione(d)
            for p in d.get("progetti", []):
                voci.append(self._progetto(p))
            if not d.get("progetti"):
                voci.append(rumps.MenuItem("Nessuna attività ancora registrata"))
            voci += self._coda(d)

        voci += [
            rumps.separator,
            rumps.MenuItem("Apri la dashboard", callback=self.apri_dashboard),
            rumps.MenuItem("Aggiorna adesso", callback=self.aggiorna_ora),
            rumps.MenuItem("Esci dalla barra", callback=rumps.quit_application),
        ]
        self.menu.clear()
        self.menu = voci

    def _intestazione(self, d) -> list:
        claimed, aperti = d.get("claimed", 0), d.get("aperti", 0)
        if d.get("spento"):
            self.title = "◌"
        elif claimed:
            self.title = f"⚠ {claimed}"
        elif aperti:
            self.title = f"● {aperti}"
        else:
            self.title = "●"

        voci = []
        if d.get("spento"):
            voci += [rumps.MenuItem("◌  Cattura in pausa"), rumps.separator]
        elif claimed:
            voci += [
                rumps.MenuItem(f"⚠  {claimed} dichiarati fatti, mai verificati"),
                rumps.separator,
            ]
        else:
            n = len(d.get("progetti", []))
            attivita = sum(p.get("attivita", 0) for p in d.get("progetti", []))
            voci += [
                rumps.MenuItem(f"{attivita} attività su {n} progetti"),
                rumps.separator,
            ]
        return voci

    def _progetto(self, p) -> rumps.MenuItem:
        voce = rumps.MenuItem(self._riga(p))
        radice = p.get("radice", "")

        for t in p.get("task", []):
            segno = {"verified": "✓", "claimed": "⚠", "in_progress": "▸"}.get(
                t.get("stato"), "○")
            titolo = str(t.get("titolo", ""))[:_MAX_TITOLO]
            voce.add(rumps.MenuItem(f"{segno}  {titolo}"))
        if p.get("task"):
            voce.add(rumps.separator)

        for f in (p.get("file") or [])[:6]:
            voce.add(rumps.MenuItem("  " + _breve(f, radice)[:_MAX_TITOLO],
                                    callback=self._mostra_file(f)))
        if p.get("file"):
            voce.add(rumps.separator)

        if p.get("nota"):
            voce.add(rumps.MenuItem("Copia cosa vede Claude qui",
                                    callback=self._copia(p["nota"])))
        voce.add(rumps.MenuItem("Apri il terminale qui",
                                callback=self._terminale(radice)))
        voce.add(rumps.MenuItem("Mostra nel Finder",
                                callback=self._mostra_file(radice)))
        return voce

    def _coda(self, d) -> list:
        voci = [rumps.separator]
        if d.get("errori"):
            voci.append(rumps.MenuItem(
                f"⚠  {d['errori'] / 1024:.1f} kB nel registro errori",
                callback=self._mostra_file(str(d.get("db", "")))))
        u = d.get("ultimo_travaso") or {}
        if u.get("errore"):
            voci.append(rumps.MenuItem(f"⚠  Travaso: {str(u['errore'])[:44]}"))
        voci.append(rumps.MenuItem(
            "Riprendi la cattura" if d.get("spento") else "Metti in pausa la cattura",
            callback=self.pausa))
        return voci

    def _riga(self, p) -> str:
        nome = str(p.get("key", "")).split("/")[-1][:24]
        pezzi = []
        if p.get("claimed"):
            pezzi.append(f"⚠ {p['claimed']}")
        if p.get("aperti"):
            pezzi.append(f"● {p['aperti']}")
        pezzi.append(_eta(p.get("ultima")))
        return f"{nome}   {' · '.join(pezzi)}"

    # -- azioni --------------------------------------------------------------

    def _terminale(self, radice):
        def cb(_):
            if radice and os.path.isdir(radice):
                subprocess.run(["open", "-a", "Terminal", radice], check=False)
        return cb

    def _mostra_file(self, percorso):
        def cb(_):
            if percorso and os.path.exists(percorso):
                subprocess.run(["open", "-R", percorso], check=False)
        return cb

    def _copia(self, testo):
        def cb(_):
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(str(testo).encode("utf-8"))
            rumps.notification("myagents", "Copiato negli appunti",
                               str(testo).splitlines()[0][:60])
        return cb

    def apri_dashboard(self, _):
        subprocess.run(["open", URL], check=False)

    def pausa(self, _):
        """Accende o spegne la cattura creando/rimuovendo un file-sentinella.

        Un file e non una variabile d'ambiente: le variabili valgono solo per i
        processi avviati dopo, mentre cosi' si fermano anche le sessioni gia'
        aperte, subito.
        """
        try:
            if SPENTO.exists():
                SPENTO.unlink()
                rumps.notification("myagents", "Cattura ripresa", "")
            else:
                SPENTO.parent.mkdir(parents=True, exist_ok=True)
                SPENTO.touch()
                rumps.notification("myagents", "Cattura in pausa",
                                   "Nulla verrà registrato finché non la riprendi")
        except OSError as exc:
            rumps.notification("myagents", "Non riesco a cambiare stato", str(exc))
        self.aggiorna(None)

    def avvia_servizio(self, _):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        python = os.path.join(repo, "venv", "bin", "python")
        src = os.path.join(repo, "src")
        subprocess.Popen(
            [python, "-c",
             f"import sys;sys.path.insert(0,{src!r});"
             "from myagents.server import avvia;avvia(in_background=False)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def aggiorna_ora(self, _):
        self.aggiorna(None)

    def aggiorna(self, _):
        self._costruisci(_stato())


def main() -> int:
    Barra().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

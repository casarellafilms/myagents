"""Il Ciottolo: la finestrella che ti avvisa quando un agente aspetta te.

E' un telecomando, mai la fonte. Compare in basso a destra, sopra tutto, senza
rubare il focus: continui a fare quello che facevi, e quando puoi ci dai
un'occhiata. Ti dice CHI aspetta e in quale progetto, e con un clic ti porta al
terminale giusto. Non risponde al posto tuo -- rispondere a una domanda dell'
agente e' una tua decisione, e un popup che la prende per te aggirerebbe proprio
il controllo che rende sicuro tutto il resto.

PERCHE' UN PROCESSO A SE'. La finestra e' PyObjC, e PyObjC vuole il suo run loop.
Tenerla dentro il servizio significherebbe mettere una GUI nel processo che fa
anche da web server e da drainer: un suo blocco fermerebbe tutto. Come processo
separato, un suo crash non tocca ne' la cattura ne' il database.

IL RISCHIO VERO NON E' LA FINESTRA ORFANA -- quella muore con il processo, anche
con SIGKILL, perche' appartiene alla connessione WindowServer del processo. Il
rischio e' il PROCESSO orfano che resta a girare a vuoto, ed e' quasi certamente
cos'e' andato storto la volta scorsa. Due difese:
  1. WATCHDOG: se il servizio non risponde per qualche secondo, il Ciottolo si
     chiude da solo. Nessun servizio, nessuna ragione di esistere.
  2. Il servizio lo lancia una volta sola (tiene il pid) e lo puo' terminare.

MISURE gia' fatte, su cui questo file poggia (niente di assunto):
  - NSPanel, NSVisualEffectView, la maschera NonactivatingPanel: tutti presenti.
  - Il segnale che apre una richiesta e' l'evento Notification, con
    notification_type='idle_prompt', message='Claude is waiting for your input'.
"""
import json
import os
import sys
import urllib.request

URL = "http://127.0.0.1:7777"
# Ogni quanto il Ciottolo chiede al servizio cosa mostrare.
POLL_MS = 600
# Dopo quanti secondi di servizio muto il Ciottolo si suicida. Sopra il ritmo di
# polling con margine: qualche giro perso non lo uccide, un servizio morto si'.
WATCHDOG_S = 6.0
# Dopo quanti secondi con la coda vuota il Ciottolo si chiude.
VUOTO_S = 2.5


def _leggi():
    """Le richieste da mostrare, o None se il servizio non risponde.

    None e vuoto sono cose diverse: None = servizio muto (candidato al
    watchdog), [] = servizio vivo che dice "niente da mostrare" (candidato alla
    chiusura per coda vuota).
    """
    try:
        with urllib.request.urlopen(f"{URL}/api/attesa", timeout=2) as r:
            return json.load(r).get("richieste", [])
    except Exception:
        return None


def _post(comando: str, dati: dict, token: str) -> None:
    try:
        req = urllib.request.Request(
            f"{URL}/api/{comando}", data=json.dumps(dati).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json",
                                    "X-Myagents-Token": token})
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Da qui in giu' e' tutto AppKit. Isolato di proposito: la logica sopra si prova
# senza schermo, questa parte esiste solo su macOS con PyObjC.
# ---------------------------------------------------------------------------

def _costruisci_app(demo_secondi: float = 0.0):
    import AppKit
    import objc
    from Foundation import NSObject, NSMakeRect, NSTimer

    ETICHETTE = {"permesso": "vuole un permesso", "domanda": "ti chiede una cosa",
                 "piano": "aspetta il via a un piano",
                 "elicitation": "aspetta una risposta",
                 "inattivo": "aspetta un tuo input"}

    LARG, ALT_RIGA, MARGINE = 340.0, 62.0, 14.0

    class Ciottolo(NSObject):
        def init(self):
            self = objc.super(Ciottolo, self).init()
            if self is None:
                return None
            self.token = ""
            try:
                self.token = open(
                    os.path.expanduser("~/.myagents/token")).read().strip()
            except OSError:
                pass
            self.muto_da = 0.0
            self.vuoto_da = 0.0
            self.richieste = []
            self._bersagli = []
            self._costruisci_finestra()
            return self

        @objc.python_method
        def _costruisci_finestra(self):
            mask = AppKit.NSWindowStyleMaskNonactivatingPanel  # niente furto focus
            rect = NSMakeRect(0, 0, LARG, 120)
            panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, mask, AppKit.NSBackingStoreBuffered, False)
            panel.setLevel_(25)                       # NSStatusWindowLevel, mai 1000
            panel.setCollectionBehavior_(1 | 16 | 64 | 256)  # AllSpaces|Stationary|IgnoresCycle|FullScreenAux
            panel.setBecomesKeyOnlyIfNeeded_(True)
            panel.setFloatingPanel_(True)
            panel.setHidesOnDeactivate_(False)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(AppKit.NSColor.clearColor())
            panel.setHasShadow_(True)
            panel.setAlphaValue_(0.0)

            vetro = AppKit.NSVisualEffectView.alloc().initWithFrame_(rect)
            vetro.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
            vetro.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            vetro.setState_(AppKit.NSVisualEffectStateActive)
            vetro.setWantsLayer_(True)
            vetro.layer().setCornerRadius_(18.0)
            vetro.layer().setMasksToBounds_(True)
            panel.setContentView_(vetro)

            self.panel = panel
            self.vetro = vetro

        # -- rendering -------------------------------------------------------

        @objc.python_method
        def _riga(self, r, y):
            larg = LARG - 2 * MARGINE
            cont = AppKit.NSView.alloc().initWithFrame_(
                NSMakeRect(MARGINE, y, larg, ALT_RIGA - 8))

            nome = os.path.basename((r.get("cwd") or "").rstrip("/")) or "sessione"
            che = ETICHETTE.get(r.get("tipo"), "aspetta")
            titolo = AppKit.NSTextField.alloc().initWithFrame_(
                NSMakeRect(0, 24, larg, 20))
            titolo.setStringValue_(f"{nome} — {che}")
            titolo.setBezeled_(False); titolo.setDrawsBackground_(False)
            titolo.setEditable_(False); titolo.setSelectable_(False)
            titolo.setFont_(AppKit.NSFont.systemFontOfSize_weight_(
                13.0, AppKit.NSFontWeightSemibold))
            titolo.setTextColor_(AppKit.NSColor.labelColor())
            cont.addSubview_(titolo)

            testo = (r.get("testo") or "").strip()
            if testo:
                sotto = AppKit.NSTextField.alloc().initWithFrame_(
                    NSMakeRect(0, 6, larg - 70, 16))
                sotto.setStringValue_(testo[:60])
                sotto.setBezeled_(False); sotto.setDrawsBackground_(False)
                sotto.setEditable_(False); sotto.setSelectable_(False)
                sotto.setFont_(AppKit.NSFont.systemFontOfSize_(11.0))
                sotto.setTextColor_(AppKit.NSColor.secondaryLabelColor())
                cont.addSubview_(sotto)

            vai = AppKit.NSButton.alloc().initWithFrame_(
                NSMakeRect(larg - 62, 4, 62, 24))
            vai.setTitle_("Vai")
            vai.setBezelStyle_(AppKit.NSBezelStyleRounded)
            vai.setTarget_(self)
            vai.setAction_("vai:")
            self._bersagli.append(r)
            vai.setTag_(len(self._bersagli) - 1)
            cont.addSubview_(vai)
            return cont

        @objc.python_method
        def _ridisegna(self):
            self._bersagli = []
            for v in list(self.vetro.subviews()):
                v.removeFromSuperview()
            n = max(1, len(self.richieste))
            alt = MARGINE + n * ALT_RIGA
            scr = AppKit.NSScreen.mainScreen().visibleFrame()
            x = scr.origin.x + scr.size.width - LARG - 20
            y = scr.origin.y + 20
            self.panel.setFrame_display_animate_(
                NSMakeRect(x, y, LARG, alt), True, False)
            self.vetro.setFrame_(NSMakeRect(0, 0, LARG, alt))
            yy = alt - ALT_RIGA
            for r in self.richieste:
                self.vetro.addSubview_(self._riga(r, yy - 6))
                yy -= ALT_RIGA

        # -- azioni ----------------------------------------------------------

        def vai_(self, sender):
            r = self._bersagli[sender.tag()]
            _post("terminale", {"radice": r.get("cwd") or ""}, self.token)

        # -- ciclo -----------------------------------------------------------

        def tic_(self, _timer):
            import time as _t
            richieste = _leggi()
            adesso = _t.time()
            if richieste is None:
                if not self.muto_da:
                    self.muto_da = adesso
                elif adesso - self.muto_da > WATCHDOG_S:
                    AppKit.NSApp().terminate_(None)   # servizio morto: mi spengo
                return
            self.muto_da = 0.0
            if not richieste:
                if not self.vuoto_da:
                    self.vuoto_da = adesso
                elif adesso - self.vuoto_da > VUOTO_S:
                    AppKit.NSApp().terminate_(None)   # niente da mostrare: esco
                    return
                self._svanisci()
                return
            self.vuoto_da = 0.0
            firma = json.dumps([(r.get("chiave"), r.get("tipo")) for r in richieste])
            if firma != getattr(self, "_firma", None):
                self._firma = firma
                self.richieste = richieste
                self._ridisegna()
                self.panel.orderFrontRegardless()
                self._appari()

        @objc.python_method
        def _appari(self):
            self.panel.animator().setAlphaValue_(1.0)

        @objc.python_method
        def _svanisci(self):
            self.panel.animator().setAlphaValue_(0.0)

    app = AppKit.NSApplication.sharedApplication()
    # accessory: niente icona nel Dock, niente voce nel menu, non ruba il focus.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    c = Ciottolo.alloc().init()

    if demo_secondi > 0:
        # Modalita' prova: mostra due richieste finte, si chiude da sola. Serve a
        # guardarla senza dover provocare un evento reale, e senza restare su.
        c.richieste = [
            {"chiave": "a", "tipo": "inattivo", "cwd": "/Users/tu/progetto-web",
             "testo": "Claude is waiting for your input"},
            {"chiave": "b", "tipo": "permesso", "cwd": "/Users/tu/altro-progetto",
             "testo": "Consento di eseguire il comando?"},
        ]
        c._firma = "demo"
        c._ridisegna()
        c.panel.orderFrontRegardless()
        c._appari()
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            demo_secondi, False, lambda t: AppKit.NSApp().terminate_(None))
    else:
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_MS / 1000.0, c, "tic:", None, True)
    return app


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    demo = 0.0
    if argv and argv[0] == "--demo":
        demo = float(argv[1]) if len(argv) > 1 else 4.0
    try:
        app = _costruisci_app(demo)
    except Exception as exc:  # niente PyObjC, niente finestra: si esce pulito
        sys.stderr.write(f"ciottolo: {type(exc).__name__}: {exc}\n")
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

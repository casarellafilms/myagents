"""La pagina della dashboard: una sola stringa HTML, zero dipendenze.

Sta in un modulo Python invece che in un file .html perche' cosi' viaggia con
il pacchetto e non ci sono percorsi da risolvere a runtime: un file in meno da
non trovare.

Principi di questa pagina, in ordine:

1. **La cosa piu' visibile e' cio' che c'e', non cio' che manca.** La prima
   versione metteva in evidenza "Nessun task" -- un'assenza -- ed era la riga
   piu' prominente di ogni scheda.
2. **I percorsi devono essere leggibili da un umano.** La prima versione li
   troncava dall'inizio con un trucco CSS e produceva stringhe come
   "...onto/61f0b1.../scratchpad/harness.js/". Ora si mostra il percorso
   relativo alla radice del progetto: cartella in tenue, nome file in evidenza.
3. **Il bigliettino e' il prodotto.** E' cio' che Claude riceve davvero: va
   mostrato, non nascosto.
4. **I tre colori sono la ragione d'essere del progetto** e vanno spiegati in
   pagina, non dati per scontati: verde = verificato dal sistema, giallo =
   Claude dice fatto ma nessuno ha controllato, grigio = da fare.
"""

PAGINA = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>myagents</title>
<style>
:root{
  --bg:#f7f6f3; --card:#fff; --bordo:#e8e5df; --bordo-2:#f0eeea;
  --testo:#1a1917; --tenue:#78746c; --tenuissimo:#a8a49b;
  --verde:#2e7d4f; --giallo:#a8760c; --giallo-bg:#fdf5e2;
  --blu:#2b5aa0; --rosso:#b0261d; --accento:#1a1917;
  --ombra:0 1px 2px rgba(20,18,15,.04), 0 8px 24px -12px rgba(20,18,15,.10);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#131215; --card:#1a191e; --bordo:#2a2830; --bordo-2:#232128;
  --testo:#eeecf1; --tenue:#9a96a2; --tenuissimo:#6b6775;
  --verde:#5ecb8d; --giallo:#e3b44a; --giallo-bg:#2b2417;
  --blu:#7ba8ea; --rosso:#f2867b; --accento:#eeecf1;
  --ombra:0 1px 2px rgba(0,0,0,.3), 0 10px 30px -14px rgba(0,0,0,.6);
}}
*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased}
body{margin:0;background:var(--bg);color:var(--testo);
  font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif}
code,.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace}

header{position:sticky;top:0;z-index:9;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:saturate(180%) blur(14px);border-bottom:1px solid var(--bordo);
  padding:14px 28px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.marchio{font-size:16px;font-weight:640;letter-spacing:-.02em;margin-right:2px}
.stato{display:flex;gap:8px;flex-wrap:wrap;align-items:center;flex:1}
.pill{font-size:12px;padding:3px 10px;border-radius:99px;border:1px solid var(--bordo);
  color:var(--tenue);white-space:nowrap;background:var(--card)}
.pill.giallo{color:var(--giallo);border-color:color-mix(in srgb,var(--giallo) 45%,transparent);
  background:var(--giallo-bg)}
.pill.rosso{color:var(--rosso);border-color:color-mix(in srgb,var(--rosso) 45%,transparent)}
/* La legenda usa --tenue e non --tenuissimo: quest'ultimo sta a 2,3:1 di
   contrasto, sotto il minimo leggibile, ed e' il testo che spiega cosa
   significano i tre colori -- cioe' l'ultimo che dovrebbe essere illeggibile. */
.legenda{display:flex;gap:14px;font-size:11.5px;color:var(--tenue);align-items:center}
.legenda span{display:flex;gap:5px;align-items:center}

/* min(420px,100%) e non 420px secco: sotto i 420 di larghezza la traccia non
   puo' piu' restringersi e la pagina scorre in orizzontale. Con min() la
   colonna collassa alla larghezza disponibile e resta tutto leggibile. */
main{padding:26px 28px 8px;display:grid;gap:20px;
  grid-template-columns:repeat(auto-fill,minmax(min(420px,100%),1fr));
  max-width:1600px;align-items:start}
@media (max-width:640px){main{padding:16px 14px 8px;gap:14px}}
/* ---- squadre di agenti ------------------------------------------------- */
#squadre:empty{display:none}
#squadre{padding:22px 28px 0;max-width:1600px}
.titolo-sezione{font-size:12px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--tenue);font-weight:600;margin:0 0 12px}
/* align-items:start, altrimenti aprire UNA scheda stira anche le vicine, che
   restano vuote e alte quanto lei. Chiuse hanno tutte lo stesso contenuto e
   quindi si allineano da sole; aperta cresce solo quella. */
.griglia-squadre{display:grid;gap:14px;align-items:start;
  grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr))}
.squadra{background:var(--card);border:1px solid var(--bordo);border-radius:14px;
  box-shadow:var(--ombra);padding:13px 15px 11px;overflow:hidden;
  display:flex;flex-direction:column}
.squadra.finita{opacity:.72}
/* La testa e' un bottone vero: si raggiunge col Tab e si apre con Invio, senza
   dover reinventare la tastiera con dei listener. */
.testa-squadra{display:flex;align-items:center;gap:8px;width:100%;
  background:none;border:0;padding:0;font:inherit;text-align:left;cursor:pointer;
  color:var(--testo)}
.testa-squadra:focus-visible{outline:2px solid var(--blu);outline-offset:3px;
  border-radius:6px}
.chevron{color:var(--tenuissimo);font-size:10px;flex:none;
  transition:transform .18s cubic-bezier(.22,1,.36,1)}
.squadra.aperta .chevron{transform:rotate(90deg)}
.nome-squadra{font-weight:600;font-size:13.5px;overflow-wrap:anywhere;flex:1;
  min-width:0}
.testa-squadra .pill{margin-left:auto;flex:none}
.scopo{margin:7px 0 0;font-size:12px;line-height:1.45;color:var(--tenue);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden}
.squadra.aperta .scopo{-webkit-line-clamp:unset}
.fasi{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}
.fase{font-size:10.5px;padding:1.5px 7px;border-radius:99px;color:var(--tenue);
  border:1px solid var(--bordo);white-space:nowrap;cursor:help}
.conta{font-size:11.5px;color:var(--tenue);white-space:nowrap}
.piede-squadra{font-size:11px;color:var(--tenuissimo);margin-top:auto;padding-top:8px}
.pill.viva{color:var(--blu);border-color:color-mix(in srgb,var(--blu) 40%,transparent)}
.battito{width:6px;height:6px;border-radius:99px;background:var(--blu);
  display:inline-block;margin-right:5px;animation:respiro 1.6s ease-in-out infinite}
@keyframes respiro{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1)}}
.barra{height:3px;border-radius:99px;background:var(--bordo-2);margin:9px 0 2px;
  overflow:hidden}
.barra i{display:block;height:100%;background:var(--verde);border-radius:99px;
  transition:width .5s cubic-bezier(.22,1,.36,1)}
/* ---- il grafico della squadra ---- */
.tela{margin:10px 0 2px}
.tela svg{display:block;width:100%;height:auto;max-height:150px}
.squadra.aperta .tela svg{max-height:none}
.arco{fill:none;stroke:var(--bordo);stroke-width:1.4}
.arco.attivo{stroke:color-mix(in srgb,var(--blu) 50%,transparent);
  stroke-dasharray:3 3;animation:scorre 1.2s linear infinite}
@keyframes scorre{to{stroke-dashoffset:-6}}
.radice circle{fill:var(--accento)}
.nodo{cursor:pointer}
.nodo .corpo{fill:var(--card);stroke:var(--bordo);stroke-width:1.5;
  transition:r .16s cubic-bezier(.22,1,.36,1)}
.nodo.fatto .corpo{fill:color-mix(in srgb,var(--verde) 12%,var(--card));
  stroke:var(--verde)}
.nodo.attivo .corpo{stroke:var(--blu)}
.nodo .spunta{fill:none;stroke:var(--verde);stroke-width:1.8;
  stroke-linecap:round;stroke-linejoin:round}
.nodo .cuore{fill:var(--blu);animation:respiro 1.6s ease-in-out infinite}
.nodo .alone{fill:none;stroke:var(--blu);stroke-width:1.4;opacity:0;
  transform-origin:center;transform-box:fill-box;animation:onda 2s ease-out infinite}
@keyframes onda{0%{opacity:.55;transform:scale(1)}100%{opacity:0;transform:scale(1.9)}}
.nodo:hover .corpo,.nodo:focus-visible .corpo,.nodo.puntato .corpo{r:13;
  stroke-width:2.2}
.nodo:focus-visible{outline:none}
.nodo:focus-visible .corpo{stroke:var(--blu);stroke-width:2.4}

/* Chiusa mostra tre agenti e si ferma: cosi' una squadra da 21 e una da 2
   occupano la stessa altezza e la griglia non si sfalsa. Aperta li mostra
   tutti. Il max-height e' generoso perche' deve reggere qualunque numero:
   serve a rendere possibile la transizione, non a tagliare. */
.agenti{list-style:none;margin:9px 0 0;padding:0;display:flex;
  flex-direction:column;gap:4px;max-height:74px;overflow:hidden;
  transition:max-height .28s cubic-bezier(.22,1,.36,1)}
.squadra.aperta .agenti{max-height:2400px;overflow-y:auto}
.agente{display:flex;gap:7px;align-items:baseline;font-size:11.5px;
  color:var(--tenue);line-height:1.5}
.agente .chi{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.squadra.aperta .agente .chi{white-space:normal;overflow-wrap:anywhere}
.agenti .segno{width:6px;height:6px;border-radius:99px;flex:none;
  background:var(--tenuissimo);transform:translateY(-1px)}
.agente.fatto .segno{background:var(--verde)}
.agente.attivo .segno{background:var(--blu);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--blu) 16%,transparent);
  animation:respiro 1.6s ease-in-out infinite}
.agente.attivo .chi{color:var(--testo)}
.agente.fermo .segno{background:var(--tenuissimo);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--tenuissimo) 14%,transparent)}
.agente.fermo .chi{opacity:.72}
.agente.puntato{background:color-mix(in srgb,var(--blu) 9%,transparent);
  border-radius:6px;margin:0 -6px;padding:0 6px}
.agente.puntato .chi{color:var(--testo)}
.pill.fermo{color:var(--tenue);border-style:dashed}
.squadra.rotta{opacity:.8}
.nodo.fermo .corpo{stroke:var(--tenuissimo);stroke-dasharray:2.5 2.5}
.nodo .fermo-segno{stroke:var(--tenuissimo);stroke-width:1.8;stroke-linecap:round}
.arco.fermo{stroke:var(--bordo);stroke-dasharray:2 3}
.suggerimento{color:var(--tenue)}
/* Chi ha chiesto meno movimento al sistema non deve vedere niente pulsare. */
@media (prefers-reduced-motion:reduce){
  .battito,.agente.attivo .segno{animation:none}
  .barra i,.agenti,.chevron{transition:none}
}

.card{background:var(--card);border:1px solid var(--bordo);border-radius:14px;
  box-shadow:var(--ombra);overflow:hidden;display:flex;flex-direction:column}
.testa{padding:16px 18px 13px;border-bottom:1px solid var(--bordo-2)}
.nome{font-size:15.5px;font-weight:640;letter-spacing:-.015em;
  display:flex;align-items:center;gap:7px;overflow-wrap:anywhere}
.org{color:var(--tenuissimo);font-weight:450;font-size:13px}
.stat{margin-top:5px;color:var(--tenue);font-size:12.5px;
  display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.stat b{font-weight:560;color:var(--testo)}

.alias{display:flex;gap:5px;margin-left:auto;flex:none}
.badge{font-size:10.5px;padding:1.5px 7px;border-radius:99px;font-weight:520;
  border:1px solid var(--bordo);color:var(--tenue);background:var(--bg);white-space:nowrap}
/* Gli alias aggiuntivi prendono un colore stabile calcolato dal loro nome (vedi
   coloreAlias piu' sotto); l'alias di base resta neutro. Nessun nome di alias
   scritto a mano qui: funzionerebbe solo per chi ha esattamente i miei. */
.badge.a0{color:var(--blu);border-color:color-mix(in srgb,var(--blu) 40%,transparent)}
.badge.a1{color:var(--verde);border-color:color-mix(in srgb,var(--verde) 40%,transparent)}
.badge.a2{color:var(--giallo);border-color:color-mix(in srgb,var(--giallo) 40%,transparent)}
.richiesta{margin:0;padding:12px 18px;border-bottom:1px solid var(--bordo-2);
  font-size:13px;line-height:1.5;color:var(--testo);overflow-wrap:anywhere;
  border-left:2px solid var(--bordo);font-style:italic}
.richiesta .etichetta{display:block;font-size:10.5px;letter-spacing:.07em;
  text-transform:uppercase;color:var(--tenuissimo);margin-bottom:5px;font-style:normal}

ul{list-style:none;margin:0;padding:6px 8px}
li{display:flex;gap:9px;align-items:flex-start;padding:6px 10px;border-radius:8px}
li:hover{background:color-mix(in srgb,var(--bg) 60%,transparent)}
.punto{width:7px;height:7px;border-radius:99px;margin-top:7px;flex:none;
  background:var(--tenuissimo)}
.verified .punto{background:var(--verde)}
.claimed  .punto{background:var(--giallo);box-shadow:0 0 0 3px var(--giallo-bg)}
.in_progress .punto{background:var(--blu)}
/* Il titolo si ferma a due righe. Senza questo un titolo lungo -- e ce ne sono
   stati da 1254 caratteri, la risposta intera di un agente finita nel campo
   sbagliato -- rendeva la scheda alta 759px e sfondava la griglia. */
.titolo{flex:1;font-size:13.5px;overflow-wrap:anywhere;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;line-height:1.42;max-height:2.84em}
.titolo.aperto{-webkit-line-clamp:unset;max-height:none}
.claimed .titolo small{color:var(--giallo);font-size:11.5px;display:block;margin-top:1px}
/* Nascosti con visibility, non con la sola opacita': un elemento a opacity 0
   resta raggiungibile col Tab, e si finiva per premere "conferma" su un
   pulsante invisibile -- cioe' per marcare verificato un task senza vederlo.
   Con :focus-within riappaiono quando ci si arriva da tastiera. */
.azioni{display:flex;gap:4px;opacity:0;visibility:hidden;
  transition:opacity .12s}
li:hover .azioni,li:focus-within .azioni{opacity:1;visibility:visible}
@media (prefers-reduced-motion:reduce){.azioni{transition:none}}
button{font:inherit;font-size:11.5px;padding:2px 9px;border-radius:6px;cursor:pointer;
  border:1px solid var(--bordo);background:var(--card);color:var(--tenue)}
button:hover{color:var(--testo);border-color:var(--tenue)}

.file{padding:11px 18px 15px;border-top:1px solid var(--bordo-2)}
.file .etichetta{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--tenuissimo);margin-bottom:7px;display:block}
.riga-file{font-size:12px;padding:2.5px 0;display:flex;gap:0;align-items:baseline;
  overflow:hidden;white-space:nowrap}
.cartella{color:var(--tenuissimo);flex:0 1 auto;overflow:hidden;text-overflow:ellipsis}
.nomefile{color:var(--testo);flex:none;font-weight:500}

.vuoto{padding:14px 18px;color:var(--tenuissimo);font-size:12.5px}
.niente{grid-column:1/-1;padding:60px 20px;text-align:center;color:var(--tenue);
  font-size:14px;line-height:1.7}
footer{padding:14px 28px 40px;color:var(--tenuissimo);font-size:11.5px}
</style></head><body>
<header>
  <span class="marchio">myagents</span>
  <div class="stato" id="stato"></div>
  <div class="legenda">
    <span><i class="punto" style="background:var(--verde)"></i>verificato</span>
    <span><i class="punto" style="background:var(--giallo)"></i>dice fatto</span>
    <span><i class="punto"></i>da fare</span>
  </div>
</header>
<section id="squadre"></section>
<main id="main"></main>
<footer id="piede"></footer>
<script>
const $ = (s) => document.querySelector(s);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function quando(iso){
  if(!iso) return "mai";
  const min = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
  if(min < 1)   return "adesso";
  if(min < 60)  return min + " min fa";
  if(min < 2880) return Math.floor(min/60) + " h fa";
  return Math.floor(min/1440) + " gg fa";
}

/* Percorso leggibile: relativo alla radice del progetto, cartella in tenue e
   nome file in evidenza. La versione precedente troncava dall'inizio e
   produceva stringhe illeggibili tipo "...onto/61f0b1.../harness.js/". */
function percorso(assoluto, radice){
  let p = String(assoluto ?? "");
  if(radice && p.startsWith(radice)){
    p = p.slice(radice.length).replace(/^\\//, "");
  } else {
    /* Fuori dal progetto: tenere gli ULTIMI segmenti, quelli che dicono
       qualcosa. Troncare dall'inizio conservando la testa del percorso
       (/private/tmp/claude-501/...) mostra solo rumore. */
    const parti = p.split("/").filter(Boolean);
    if(parti.length > 3) p = "…/" + parti.slice(-3).join("/");
  }
  const i = p.lastIndexOf("/");
  const cartella = i >= 0 ? p.slice(0, i + 1) : "";
  const nome = i >= 0 ? p.slice(i + 1) : p;
  return `<div class="riga-file" title="${esc(assoluto)}">
    <span class="cartella">${esc(cartella)}</span><span class="nomefile">${esc(nome)}</span>
  </div>`;
}

function nomeProgetto(key){
  const i = String(key).lastIndexOf("/");
  return i < 0 ? esc(key)
    : `<span class="org">${esc(key.slice(0,i+1))}</span>${esc(key.slice(i+1))}`;
}

async function agisci(comando, task_key){
  await fetch("/api/" + comando, {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify({task_key})});
  carica();
}

function card(p){
  const task = p.task.length ? `<ul>${p.task.map(t => `
    <li class="${esc(t.stato)}">
      <span class="punto"></span>
      <span class="titolo">${esc(t.titolo)}${
        t.stato === "claimed" ? "<small>Claude dice fatto — nessuna prova raccolta</small>" : ""}</span>
      <span class="azioni">
        ${t.stato === "claimed"
          ? `<button onclick="agisci('conferma','${esc(t.key)}')">conferma</button>` : ""}
        <button onclick="agisci('archivia','${esc(t.key)}')">archivia</button>
      </span>
    </li>`).join("")}</ul>` : "";

  const richiesta = p.richiesta
    ? `<div class="richiesta"><span class="etichetta">Ultima cosa che hai chiesto</span>“${esc(p.richiesta)}”</div>`
    : "";

  const file = p.file.length ? `<div class="file">
      <span class="etichetta">File toccati di recente</span>
      ${p.file.map(f => percorso(f, p.radice)).join("")}
    </div>` : "";

  return `<section class="card">
    <div class="testa">
      <div class="nome">${nomeProgetto(p.key)}
        <span class="alias">${(p.alias||[]).map(a =>
          `<span class="badge ${esc(a.replace("claude-",""))}" title="rilevato dall'alias ${esc(a)}">${esc(a)}</span>`).join("")}</span>
      </div>
      <div class="stat"><b>${p.attivita}</b> attività · ultima ${quando(p.ultima)}${
        p.in_coda ? ` · <span style="color:var(--tenuissimo)">${p.in_coda} sessioni da analizzare</span>` : ""}${
        p.claimed ? ` · <span style="color:var(--giallo)"><b>${p.claimed}</b> da confermare</span>` : ""}${
        p.aperti ? ` · <b>${p.aperti}</b> aperti` : ""}</div>
    </div>
    ${richiesta}${task}${file}</section>`;
}

/* Il grafico delle squadre di agenti.
   Disegnato in SVG a mano: nessuna libreria, quindi niente da aggiornare fra
   sei mesi e nessun megabyte scaricato per quattro cerchi e qualche linea.
   Si ridisegna solo quando lo stato CAMBIA davvero (vedi firmaSquadre): un
   ridisegno a ogni giro spegnerebbe le animazioni CSS e farebbe sfarfallare
   la pagina ogni cinque secondi. */
/* Le squadre di agenti.
   Niente ventaglio di nodi: con ventuno agenti gli archi si accavallano e non
   si legge piu' niente. Una griglia di pastiglie regge qualunque numero, sta in
   un'altezza fissa, e ogni pastiglia PORTA IL NOME -- che e' l'informazione che
   serve davvero: non "ci sono ventuno cose", ma "questo sta guardando X".
   Nessuna libreria: quattro forme e una transizione non valgono un megabyte. */

/* Il grafico della squadra.
   I nodi vanno A CAPO invece di stare in fila: in fila, ventuno agenti si
   accavallano e il disegno smette di dire qualcosa. A griglia ne reggi
   quanti ne vuoi, e ogni nodo resta grande abbastanza da essere puntato.
   Ogni nodo e' collegato all'elenco sotto: passandoci sopra si illumina la
   riga col nome, perche' un cerchio senza nome non dice chi sta lavorando. */
function grafico(w, id){
  const n = w.agenti.length;
  if(!n) return "";
  const COL = Math.min(7, n), R = 11, PX = 42, PY = 40;
  const righe = Math.ceil(n/COL);
  const larghezza = COL*PX, alt = 34 + righe*PY;
  const radice = {x: larghezza/2, y: 16};

  const posizione = i => {
    const r = Math.floor(i/COL);
    // L'ultima riga, se incompleta, si centra: allineata a sinistra sembrerebbe
    // un errore di disegno invece di una riga che finisce.
    const inRiga = Math.min(COL, n - r*COL);
    const off = (larghezza - inRiga*PX)/2;
    return {x: off + (i%COL)*PX + PX/2, y: 34 + r*PY + R};
  };

  const archi = w.agenti.map((a,i) => {
    const p = posizione(i);
    return `<path class="arco ${classeAgente(a)}"
      d="M${radice.x},${radice.y+7} C${radice.x},${(radice.y+p.y)/2}
         ${p.x},${(radice.y+p.y)/2} ${p.x},${p.y-R}"/>`;
  }).join("");

  const nodi = w.agenti.map((a,i) => {
    const p = posizione(i);
    const c = classeAgente(a), d = diciturAgente(a);
    return `<g class="nodo ${c}" tabindex="0" role="listitem"
        data-squadra="${esc(id)}" data-agente="${i}"
        aria-label="${esc(a.nome)} — ${d}">
      <title>${esc(a.nome)} — ${d}</title>
      ${c==='attivo' ? `<circle class="alone" cx="${p.x}" cy="${p.y}" r="${R}"/>` : ""}
      <circle class="corpo" cx="${p.x}" cy="${p.y}" r="${R}"/>
      ${c==='fatto' ? `<path class="spunta" d="M${p.x-4.5},${p.y} l3,3 l6,-6.5"/>`
        : c==='attivo' ? `<circle class="cuore" cx="${p.x}" cy="${p.y}" r="3"/>`
        : `<path class="fermo-segno" d="M${p.x-3.5},${p.y} h7"/>`}
    </g>`;
  }).join("");

  return `<div class="tela"><svg viewBox="0 0 ${larghezza} ${alt}"
      preserveAspectRatio="xMidYMin meet" role="list"
      aria-label="agenti di ${esc(w.nome)}">
    ${archi}
    <g class="radice"><circle cx="${radice.x}" cy="${radice.y}" r="7"/></g>
    ${nodi}
  </svg></div>`;
}

/* Tre stati, non due. "fermo" e' un agente avviato che non scrive piu': o e'
   stato interrotto o e' morto. Confonderlo con "sta lavorando" faceva sembrare
   vivo un workflow fermato un'ora prima. */
function classeAgente(a){
  return a.stato === "finito" ? "fatto" : a.stato === "fermo" ? "fermo" : "attivo";
}
function diciturAgente(a){
  return a.stato === "finito" ? "ha finito"
       : a.stato === "fermo" ? "fermo, non risponde piu'" : "sta lavorando";
}

function pastiglia(a, i, id){
  return `<li class="agente ${classeAgente(a)}" role="listitem"
      data-squadra="${esc(id)}" data-agente="${i}"
      title="${esc(a.nome)} — ${diciturAgente(a)}">
    <i class="segno" aria-hidden="true"></i>
    <span class="chi">${esc(a.nome)}</span>
  </li>`;
}

function squadra(w){
  const finita = w.stato === "finito";
  const rotta = w.stato === "interrotto";
  const quota = w.avviati ? Math.round(100*w.conclusi/w.avviati) : 0;
  const aperta = squadreAperte.has(w.id);
  const fasi = (w.fasi||[]).map(f =>
    `<span class="fase" title="${esc(f.dettaglio||'')}">${esc(f.titolo)}</span>`).join("");

  return `<article class="squadra ${finita?'finita':rotta?'rotta':'viva'} ${aperta?'aperta':''}"
      data-id="${esc(w.id)}">
    <button class="testa-squadra" aria-expanded="${aperta}" data-apri="${esc(w.id)}">
      <span class="chevron" aria-hidden="true">▸</span>
      <span class="nome-squadra">${esc(w.nome)}</span>
      ${finita ? `<span class="pill">conclusa</span>`
        : w.stato === "interrotto"
          ? `<span class="pill fermo">interrotta ${w.conclusi}/${w.avviati}</span>`
          : `<span class="pill viva"><i class="battito"></i>${w.conclusi}/${w.avviati}</span>`}
    </button>
    ${w.scopo ? `<p class="scopo">${esc(w.scopo)}</p>` : ""}
    ${fasi ? `<div class="fasi">${fasi}</div>` : ""}
    <div class="barra" role="progressbar" aria-valuenow="${quota}"
         aria-valuemin="0" aria-valuemax="100"><i style="width:${quota}%"></i></div>
    ${grafico(w, w.id)}
    <ul class="agenti" role="list">${
      w.agenti.map((a,i) => pastiglia(a, i, w.id)).join("")}</ul>
    <div class="piede-squadra">${w.conclusi} di ${w.avviati} agenti hanno finito
      ${aperta ? "" : `· <span class="suggerimento">apri per vederli tutti</span>`}</div>
  </article>`;
}

/* Quali schede l'utente ha aperto. Vive fuori dal rendering: altrimenti ogni
   aggiornamento le richiuderebbe sotto le dita. */
const squadreAperte = new Set();

/* Ridisegnare a ogni giro azzererebbe le animazioni e farebbe sfarfallare la
   pagina ogni cinque secondi. Si confronta una firma di cio' che si vede: se
   non e' cambiata, il DOM non si tocca. */
let firmaSquadre = "";
function disegnaSquadre(elenco){
  const vivi = (elenco||[]).filter(w => w.stato === "in corso");
  const rotti = (elenco||[]).filter(w => w.stato === "interrotto").slice(0,2);
  const recenti = (elenco||[]).filter(w => w.stato === "finito").slice(0,3);
  const mostrati = vivi.concat(rotti, recenti);
  const firma = JSON.stringify([[...squadreAperte].sort(), mostrati.map(w =>
    [w.id, w.stato, w.conclusi, w.avviati, w.agenti.map(a=>a.stato)])]);
  if(firma === firmaSquadre) return;
  firmaSquadre = firma;
  $("#squadre").innerHTML = mostrati.length
    ? `<h2 class="titolo-sezione">Squadre di agenti
         <small>${vivi.length} in corso</small></h2>
       <div class="griglia-squadre">${mostrati.map(squadra).join("")}</div>`
    : "";
}

/* Nodo e nome sono la stessa cosa vista in due modi: puntare l'uno illumina
   l'altro. Senza questo legame il grafico resta un insieme di cerchi anonimi e
   l'elenco un muro di righe, e nessuno dei due dice chi sta facendo cosa. */
function illumina(squadra, indice, acceso){
  document.querySelectorAll(
    `[data-squadra="${CSS.escape(squadra)}"][data-agente="${indice}"]`
  ).forEach(n => n.classList.toggle("puntato", acceso));
}
["mouseover","mouseout","focusin","focusout"].forEach(evento =>
  document.addEventListener(evento, e => {
    const t = e.target.closest && e.target.closest("[data-agente]");
    if(!t) return;
    illumina(t.getAttribute("data-squadra"), t.getAttribute("data-agente"),
             evento === "mouseover" || evento === "focusin");
  }, true));

/* Un solo ascoltatore sul contenitore invece di uno per scheda: le schede si
   ridisegnano, e i gestori attaccati a mano sparirebbero con loro. */
document.addEventListener("click", e => {
  const testa = e.target.closest("[data-apri]");
  if(!testa) return;
  const id = testa.getAttribute("data-apri");
  squadreAperte.has(id) ? squadreAperte.delete(id) : squadreAperte.add(id);
  const scheda = testa.closest(".squadra");
  scheda.classList.toggle("aperta");
  testa.setAttribute("aria-expanded", scheda.classList.contains("aperta"));
});

async function carica(){
  let d;
  try{ d = await (await fetch("/api/stato")).json(); }
  catch(e){
    $("#stato").innerHTML = `<span class="pill rosso">servizio non raggiungibile</span>`;
    return;
  }
  const s = [`<span class="pill">${d.progetti.length} progetti attivi</span>`];
  if(d.claimed) s.push(`<span class="pill giallo">${d.claimed} da confermare</span>`);
  if(d.aperti)  s.push(`<span class="pill">${d.aperti} task aperti</span>`);
  if(d.spento)  s.push(`<span class="pill rosso">cattura spenta</span>`);
  if(d.profonda && d.profonda.in_corso)
    s.push(`<span class="pill viva"><i class="battito"></i>revisore profondo</span>`);
  const u = d.ultimo_travaso || {};
  if(u.errore)  s.push(`<span class="pill rosso">travaso: ${esc(u.errore)}</span>`);
  $("#stato").innerHTML = s.join("");

  disegnaSquadre(d.workflow);

  $("#main").innerHTML = d.progetti.length ? d.progetti.map(card).join("")
    : `<div class="niente">Nessuna attività registrata ancora.<br>
       Apri Claude Code in un progetto e lavora normalmente:<br>
       comparirà qui entro una ventina di secondi.</div>`;

  const eta = u.quando ? Math.round(Date.now()/1000 - u.quando) : null;
  $("#piede").textContent = [
    `${d.spool} file in attesa di travaso`,
    `ultimo travaso ${eta === null ? "mai" : eta + "s fa"}`,
    d.errori ? `registro errori ${(d.errori/1024).toFixed(1)} kB` : "nessun errore",
    d.db,
  ].join("  ·  ");
}
carica();
setInterval(carica, 5000);
</script></body></html>
"""

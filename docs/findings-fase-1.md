# Task 0 — misure empiriche sugli hook di Claude Code

**Data:** 2026-08-01 · **Claude Code:** v2.1.220 · **macOS**
Metodo: sonda in `.claude/settings.json` locale al progetto, 31 eventi reali
raccolti in 4 sessioni. Nessuna assunzione: solo campi osservati.

## Forma reale dei payload

Tutti gli eventi portano `session_id`, `cwd`, `hook_event_name`, `transcript_path`.
**`hook_event_name` e' nel payload**: l'evento non va dedotto da `argv`.

| Evento | Campi propri |
|---|---|
| `SessionStart` | `model`, `source` |
| `UserPromptSubmit` | `prompt`, `prompt_id`, `permission_mode` |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms`, `effort`, `permission_mode`, `prompt_id` |
| `SessionEnd` | `reason`, `prompt_id` |

`tool_input` per `Write`: `content`, `file_path` — confermato.
`tool_input` per `Bash`: `command`, `description` — confermato.

## SCOPERTA CHE CAMBIA IL PROGETTO

`tool_response` di `Bash` contiene esattamente:

    stdout · stderr · interrupted · isImage · noOutputExpected

**Non c'e' `exit_code`, e non c'e' nessun altro indicatore di esito** (cercate
le sottostringhe exit/error/status/code/returncode nell'intero evento: assenti).

Il piano faceva passare un task a `verified` leggendo `tool_response.exit_code`.
Su questi dati sarebbe stato sempre `None`: **nessun task sarebbe mai diventato
verde.** Il sistema avrebbe catturato tutto correttamente restando giallo per
sempre — e il verde e' l'unica cosa che lo distingue da una lista di cose da fare.
Nessuno degli 85 test lo avrebbe rilevato: tutti passano `exit_code` a mano.

### Conseguenza di progetto

La verifica non puo' basarsi sull'osservare Claude che lancia un comando.
Taskdb deve **eseguire lui** il comando di verifica e registrarne l'esito reale
insieme al commit corrente. Questo chiude anche l'obiezione di Codex sul
`verify_cmd` debole (test filtrato / worktree sbagliato): il comando lo esegue
sempre lo stesso attore, nello stesso posto.

- Fase 1: l'hook registra che un comando e' stato eseguito, con `stdout`,
  `stderr`, `interrupted`. Nessun passaggio automatico a `verified`.
- Fase successiva: comando esplicito `tk verify`, che esegue e registra.

## TodoWrite: mai scattato

Due sessioni, due richieste esplicite di una lista di todo: **zero eventi
`TodoWrite`**. Claude ha risposto a parole senza usare lo strumento.

Il rischio R1 del piano ("il mirror dipende dal fatto che Claude apra un todo")
e' piu' grave di come era classificato. Il mirror va trattato come un extra
opportunistico, non come una delle due gambe della cattura. Le gambe vere sono
i file modificati e i comandi eseguiti, che scattano sempre.

Non avendo il formato osservato, `hook.py` lo tratta in modo difensivo: accetta
le forme ragionevoli e scrive su `ERROR_LOG` qualsiasi forma non riconosciuta,
cosi' la prima occorrenza reale si scopre dal log invece che dal silenzio.

## Subagent

Non misurato: nessuna sessione della cattura ha lanciato subagent. Resta aperto.

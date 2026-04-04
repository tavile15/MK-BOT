# Plano de ação — Microtransações 2.0

Documento vivo: **atualizar ao fim de cada sessão/prompt** com data, decisões e estado — histórico auditável do projeto e alinhamento com o “plano de negócio” (fases de produto até eventual execução real).

**Comunicação:** perfil do idealizador e tom das explicações — ver **`DIRETRIZES_COMUNICACAO.md`** e `.cursor/rules/microtrans-comunicacao.mdc`.

---

## Estágio no plano de negócio (onde estamos agora)

| Fase | Descrição | Status |
|------|------------|--------|
| **P0 — Instrumentação** | Dados públicos, filtros, logs, config | Concluído |
| **P1 — Motor em papel** | Simulação ida/volta, carteira, métricas, backtest | Concluído |
| **P2 — Operação manual + vigência (UI)** | Tick único, vigília, auditoria/CSV, UI sem bloqueio longo | **Concluído + reforçado** (fragment, estado limpo, `book_top` default) |
| **P3 — Inteligência** | Agente LLM opcional, calibração | **Protótipo avançado** — Gemini (`gemini-2.5-flash`), `agent_gemini` + piso `gemini_min_spread_bps`; snapshot rico (`execution` no contrato v2); evidência `auditoria_ticks.csv` / LOG12 |
| **P3b — Backtest reprodutível + auditoria BT** | Replay explícito, relatório dedicado, registo de decisões do agente, stress no papel | **Concluído** (2026-03-29) — evidência em `auditoria/backtest/stress_batch_20260329_224438.csv` + `bt_run_id` d09a6c6404e8 / 67d73e87704c / e6c6e3e2e5a5. |
| **P4t — Spot Testnet (pré-live)** | REST assinado, ordens LIMIT na rede de testes Binance, soak 24/7, auditoria de fills | **Em andamento** (2026-03-29) — `binance_signed` + `binance_testnet_cli` + `execution` no YAML; próximo: **executor** (motor → ordens) + processo 24/7 + caps de risco. |
| **P4 — Execução real (live)** | Mesma stack com URL + keys **live**, política de risco, compliance | **Bloqueado** até gate ao fim do soak P4t + checklist escrito. |

**Síntese:** **P3b fechado.** **Prioridade atual:** **P4t (Testnet)** como validação final com ordens reais na API de testes e saldo fictício da Binance; soak **24/7** com logs e kill switch. **Em paralelo (menor prioridade):** Frente 6 (IA) com corpus P3b. **P4 live** só após P4t estável e checklist de risco.

---

## Visão do produto

- Pipeline **Binance (API pública)** → **filtros** → **agente (1× por ciclo)** → **motor em papel** → **carteira simulada** + **UI Streamlit**.
- **Nenhuma ordem na conta live** é enviada hoje; papel local + **Spot Testnet** (fictício na corretora) são os caminhos de validação antes de P4 live.

---

## P4t — Spot Testnet: objetivo e fluxo de trabalho (oficial)

**Objetivo:** expor o robô ao **mesmo protocolo** de ordens que a produção (REST assinado, LIMIT/MARKET conforme desenho), com **saldos fictícios** no [Spot Testnet](https://testnet.binance.vision), e observar comportamento **24/7** durante dias — **validação final antes de P4 live**.

**Custos:** API key testnet **gratuita**; custos opcionais = VPS/eletricidade + Gemini se o loop usar LLM.

### Ordem recomendada (dependências)

| Fase | Entregável | Critério de saída |
|------|------------|-------------------|
| **P4t-1 Credenciais** | Conta GitHub → testnet.binance.vision → gerar HMAC key/secret; variáveis `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` (ou secrets Streamlit só se a UI for usar assinado). | `python -m microtrans.binance_testnet_cli account` devolve JSON com `canTrade` e saldos virtuais. |
| **P4t-2 Dados alinhados** | `BinancePublic(base_url=testnet)` para klines/depth no **mesmo** universo que as ordens (já suportado via `execution.testnet_base_url` em código novo). | `binance_testnet_cli ping` e `book --symbol BTCUSDT` OK. |
| **P4t-3 Ordens mínimas** | Usar `BinanceSigned.new_order` / `cancel_order` em script controlado (1 LIMIT longe do mercado + cancel) ou expandir CLI com subcomando protegido. | Uma ordem criada e cancelada no testnet sem erro de assinatura/filters. |
| **P4t-4 Executor** | Camada que traduz decisões do motor (spread, tamanho, ciclo) em **post/cancel** de LIMIT maker, com **notional máximo**, **símbolo allowlist**, **kill switch** (ficheiro ou env). | Documento curto no código + teste manual em par único. |
| **P4t-5 Loop 24/7** | Processo dedicado (não só Streamlit): systemd/Docker/VPS, rotação de log, alerta se o processo morrer. | 48–72 h contínuas sem intervenção, métricas básicas (ordens, fills, erros API). |
| **P4t-6 Soak + relatório** | Comparar PnL/posição testnet vs expectativa do papel; lista de incidentes (resets testnet, rate limit). | Relatório de 1 página no changelog ou `teste/` com recomendação go/no-go **live**. |
| **P4 live (gate)** | Trocar `base_url` + keys **live**, limites mais estritos, confirmação legal/tributária. | Checklist explícito assinado (mesmo que só interno). |

**Regras de segurança:** `BinanceSigned` recusa `base_url` que não seja testnet **salvo** `require_testnet=False` (uso explícito para live no futuro). Nunca commitar secrets.

### Implementação já no repositório (P4t kickoff)

- `src/microtrans/binance_signed.py` — HMAC, `account`, `open_orders`, `new_order`, `cancel_order`.
- `src/microtrans/binance_testnet_cli.py` — `ping`, `account`, `open-orders`, `book`.
- `config/default.yaml` — secção `execution` (`mode`, `testnet_base_url`, `recv_window_ms`).

---

## Evidência de validação (2026-03-29)

### `teste/LOG08.TXT`

- **Sem relaxar:** `liquidity_too_high` (~483k quote vs teto 250k), carteira estável em 10 000, 0 ops — **esperado**.
- **Com relaxamento:** veredito **APTO**, **Ciclo 1**, modo **`synthetic_mid`**, voltas papel com líquido ~**+0,060 USDT** por rodada, taxas contabilizadas, patrimônio **10 000 → 10 000,18** em 3 ticks ativos (6 ops) — **comportamento esperado pós-correções LOG07**.

### Export `2026-03-29T15-42_export.csv` (auditoria)

- Linha inicial `apto_filtro=não` / ciclo 0 / patrimônio 10 000; em seguida `cycle_start` e série de ticks `sim_volta_ok=sim`, `sim_precificacao=synthetic_mid`, `sim_resultado_quote≈0,060132`, `sim_obs=ok`, patrimônio crescendo até ~**10 000,66** — **CSV bate com o desenho da auditoria na UI**.
- **Spread do livro** no CSV (~0,002 bps no arquivo) reflete snapshot microscópico; simulação usa **`synthetic_mid`**, coerente com margem positiva por construção (spread do agente acima do piso de taxas).

**Conclusão:** fluxo **filtro → agente → papel → carteira → auditoria/export** está **OK** para seguir para o próximo incremento lógico (P3).

---

## Decisão estratégica (2026-03-29) — prioridade P3b vs nova onda de IA

**Contexto:** O backtest atual usa candles públicos recentes (`filters.kline_interval`, até `min(bars,1000)` velas), livro **sintético** — não é seleção explícita “1d / 1 mês / 6 mês” na UI, nem L2 histórico real. A auditoria de ticks ao vivo e o replay histórico têm semânticas diferentes; misturá-las dificulta estudar o robô “só no backtest”.

**Decisão (alinhamento explícito com o plano de negócio):**

1. **Não** priorizar agora uma segunda grande investida só em refinamento do LLM **sem** instrumentação de replay e de decisões do agente.
2. **Priorizar** evoluir o **backtest** para par **e** janela temporal / timeframe **explícitos** (e, quando necessário, **paginação** da API Binance para >1000 barras), com **relatório/auditoria dedicada ao backtest** (métricas por corrida: ciclos, PnL, tempo apto, voltas papel, etc.).
3. **Registar** por corrida (`run_id`) as **saídas normalizadas do agente** (`spread_bps`, `order_size_quote`, `max_inventory_base`, `meta.source`, avisos, e opcionalmente `raw_proposal` / contrato) para análise futura: *qual política correlaciona com melhor resultado em que mercado*.
4. **Testes de stress** no **simulador** (spreads sintéticos apertados, taxas/slippage maiores, cenários configuráveis) **antes** de P4 — reduce surpresas de lógica e software; **não** substitui paper com livro real nem risco legal de capital real.

**Limitação que mantemos transparente comercialmente:** stress e PnL em replay continuam ancorados ao **modelo de papel** + proxy de liquidez; não provam performance de fila maker na Binance histórica.

---

## Fluxo lógico de trabalho (ordem recomendada — P3b)

Ordem escolhida para **máximo aprendizado com mínimo retrabalho** e gates claros antes de P4:

| Ordem | Frente | Entregável / critério |
|-------|--------|------------------------|
| **1** | **Contrato do replay** | UI (ou config salva) com: par, intervalo de vela do replay, `bars` ou intervalo de datas; documentação visível do que a API devolve (ex.: teto 1000 velas por chamada; necessidade de paginação). |
| **2** | **Motor de histórico** | `run_backtest` (ou camada nova) consome a janela pedida; se `bars` efetivos > 1000, **paginar** `klines` com `endTime` até cobrir o período. |
| **3** | **Auditoria exclusiva do BT** | Artefacto(s) por `bt_run_id`: linhas por passo de replay (barra índice, apt, evento de ciclo, `paper_step`, património) **separados** do CSV de vigília ao vivo; sumário ao fim (PnL, nº ciclos, contagem de skips, etc.). |
| **4** | **Registo de decisões do agente** | Ficheiro estruturado (JSONL/CSV) por corrida: cada `cycle_start` com params finais + `meta` (e opcional payload bruto da IA); permite cruzar com mercado e janela. |
| **5** | **Presets de stress** | Perfis em YAML ou toggles: `synthetic_book_spread_bps`, `fee_bps`, `paper_slippage_bps`, skip — cenários nomeados (“baseline”, “taxas_altas”, “livro_apertado”). |
| **6** | **Retomar melhoria da IA** | Ajustes de prompt/modelo **só** com corpus de corridas P3b (comparação controlada). |
| **7** | **P4** | Iniciar só com política explícita de risco, dry-run e critérios de aceitação; fora de escopo até fechar evidência P3b relevante. |

**Dependências:** (2) depende de (1); (4) encaixa naturalmente em (3); (5) pode começar em paralelo a partir de (2) com config estática.

---

## Encerramento da fase P3b — estratégia e fluxo de trabalho

Objetivo: **fechar o “laboratório de replay”** com critérios explícitos, evidência arquivada e decisão consciente sobre **próximo investimento** (IA vs papel vs P4), sem misturar escopos.

### Definição de pronto (DoD) para declarar P3b encerrada

- Replay **reprodutível**: pelo menos uma janela fixa (UTC) documentada + um par + intervalo de vela + `replay_spec` guardado (log ou export JSON na UI).
- **Dois modos de agente** demonstrados em ficheiros: (A) heurística forçada em replay longo; (B) Gemini em replay com número de chamadas compreendido (1× por ciclo novo).
- **Stress**: matriz mínima (abaixo) executada — manualmente na UI **ou** via script em lote quando existir.
- **Artefactos**: pasta `auditoria/backtest/` ou downloads com `bt_run_id` referenciados numa linha do changelog ou ficheiro `teste/` (ex.: `EVIDENCIA_P3B.md` opcional — só se quiseres texto extra; o changelog já basta).

### Fluxo em 4 blocos (ordem eficiente)

| Bloco | Nome | O quê | Saída / gate |
|-------|------|--------|----------------|
| **A** | **Baseline congelada** | Escolher 1 par (ex. BTCUSDT), 1 timeframe de replay (ex. 5m ou 3m), 1 janela UTC fixa (ex. 7 dias), preset `baseline`, **heurística forçada** no BT. Correr 1×; guardar CSV+JSONL. | Referência “barata” para comparar tudo o resto. |
| **B** | **Matriz de stress** | Sobre a **mesma** janela UTC e par, correr **cada** preset: `livro_apertado`, `taxas_altas` (e `baseline` já em A). Opcional: repetir com `book_top` vs `synthetic_mid` na UI se quiseres ver sensibilidade ao modo de papel. | Tabela escrita (mesmo que em Excel/Notion): colunas = preset, PnL, ciclos, % apto, `paper_failure_reason_counts` (top 2 motivos). **Gate:** nenhum crash; resultados explicáveis. |
| **C** | **Gemini controlado** | Na **mesma** janela **curta** (ex. 2–3 dias ou N barras modesto) **sem** forçar heurística, 1–2 corridas com Gemini. Anotar nº de linhas no JSONL (= chamadas API). | Confirmação de custo/latência aceitáveis para o teu uso. Se o orçamento for problema, B+A já fecham P3b sem C. |
| **D** | **Encerramento formal** | Atualizar este `PLANO_ACAO.md`: linha P3b → **Concluída**; 1 parágrafo “lições” (ex.: “taxas_altas zerou voltas”; “Gemini variou tamanho entre ciclos”). Decidir **próximo epic**: **(1)** Frente 6 — melhoria IA com corpus; **(2)** endurecer papel; **(3)** rascunho de requisitos P4. | Só **um** epic principal na próxima sprint para não dispersar. |

### Paralelo permitido (não bloqueia P3b)

- **Endurecer papel** (inventário, maker): pode avançar em ramo separado **depois** de B estar feito, para não contaminar a matriz de stress.

### O que **não** fazer nesta fase (evita retrabalho)

- Não misturar **P4** (ordens reais) com fecho P3b.
- Não otimizar prompt Gemini em loop **antes** de ter a tabela do bloco **B** (senão não sabes o que melhorou).

### Fluxo oficial escolhido para fechar P3b (decisão 2026-03-29)

**Critério:** máxima eficiência com o que já está no repositório, sem UI extra, alinhado ao DoD.

| Passo | O quê | Porquê |
|-------|--------|--------|
| **1 — Matriz + baseline numa tacada** | Correr **`batch_stress`** com **`--all-stress-presets`**, **`--force-heuristic`**, **janela UTC fixa** (recomendado: 7 dias no mesmo par e timeframe que vais usar na Frente 6), e **`--interval`** explícito (ex. `5m` ou `3m`). | O preset `baseline` no CSV cumpre o bloco **A**; as outras linhas cumprem **B** na **mesma** janela — comparação limpa, um só comando. |
| **2 — Gemini (opcional, DoD “dois modos”)** | Se quiseres fechar o DoD por completo: na UI ou CLI, **uma** corrida **curta** (2–3 dias ou `bars` modesto), **sem** forçar heurística, com `agent.provider: gemini` e chave válida; guardar `bt_run_id` + JSONL. | Se orçamento for problema, **documenta no changelog** “DoD Gemini adiado” e segue; o plano aceita A+B como núcleo. |
| **3 — Encerramento formal** | (a) Anexar ao changelog **caminho** do `stress_batch_*.csv` do passo 1 + par + `start/end` UTC + intervalo. (b) Linha **P3b** no quadro de fases → **Concluída**. (c) **Próximo epic único:** **Frente 6 — melhoria da IA** com corpus P3b (comparações na **mesma** janela/replay que o passo 1). | P4 e endurecimento papel ficam **fora** da sprint seguinte salvo decisão explícita contrária. |

**Comando modelo (PowerShell)** — ajustar datas, símbolo e pasta do projeto:

```powershell
$ROOT = "F:\AMBIENTE VIRTUAL\CODIGOS\python\MICROSTRANSAÇÕES 2.0"
$env:PYTHONPATH = "$ROOT\src"
Set-Location $ROOT
python -m microtrans.batch_stress `
  --symbol BTCUSDT `
  --interval 5m `
  --all-stress-presets `
  --start-utc 2026-03-22T00:00:00+00:00 `
  --end-utc   2026-03-29T00:00:00+00:00 `
  --force-heuristic
```

Saída esperada: `auditoria\backtest\stress_batch_*.csv` (+ opcionalmente `*_audit.csv` / `*_agent.jsonl` por `bt_run_id` se não usares `--no-audit-files`).

### Próximo incremento de código (se quiseres fechar B com menos cliques)

- **CLI `batch_stress`:** `python -m microtrans.batch_stress` (com `PYTHONPATH` a apontar para `src`) — `--all-stress-presets` ou `--presets a,b`; modo `--bars N` ou `--start-utc` + `--end-utc` opcional; saída `auditoria/backtest/stress_batch_*.csv`. **Sem** expander/UI em lote (decisão produto 2026-03-29).

---

## Changelog (sessões)

### 2026-03-29 — P4t Spot Testnet: kickoff (REST assinado + plano)

- **Código:** `binance_signed.BinanceSigned` (testnet por defeito; bloqueio de URL não-testnet); `binance_testnet_cli` (`ping`, `account`, `open-orders`, `book`); `config/default.yaml` → `execution`.
- **Plano:** nova linha **P4t** no quadro de fases; secção **P4t — Spot Testnet** com fluxo P4t-1 … P4t-6 + gate P4 live; síntese atualizada (prioridade P4t; Frente 6 em paralelo menor).

### 2026-03-29 — P3b encerrada (execução do fluxo oficial)

- **Comando:** `batch_stress` — `BTCUSDT`, `5m`, janela **2026-03-22T00:00:00+00:00** → **2026-03-29T00:00:00+00:00**, `--all-stress-presets`, `--force-heuristic`.
- **Artefacto agregado:** `auditoria/backtest/stress_batch_20260329_224438.csv`.
- **Corridas (heurística):** `d09a6c6404e8` (baseline), `67d73e87704c` (livro_apertado), `e6c6e3e2e5a5` (taxas_altas) — cada uma com `*_audit.csv` e `*_agent.jsonl` na mesma pasta.
- **Números (sumário):** 2017 velas, 1957 passos, 10 ciclos, ~93,8% passos com filtro apto nos três presets; **PnL vs início** baseline **+22,97** USDT, taxas_altas **+22,98** USDT, livro_apertado **0** (0 operações — domina `book_top_spread_smaller_than_fees` no papel).
- **DoD “dois modos de agente”:** modo Gemini em replay já documentado antes (`bt_e9a6597f2c0f` neste ficheiro); não foi repetido nesta sessão para poupar API.
- **Fase:** linha **P3b** → **Concluído**; próxima prioridade de produto: **Frente 6** (IA).

### 2026-03-29 — Fluxo oficial de fecho P3b (decisão produto)

- Subsecção **“Fluxo oficial escolhido para fechar P3b”**: passos 1–3 (batch único = A+B; Gemini opcional; encerramento + próximo epic **Frente 6**); comando PowerShell modelo.

### 2026-03-29 — Fluxo de encerramento P3b (estratégia)

- Secção **“Encerramento da fase P3b — estratégia e fluxo de trabalho”**: DoD, blocos A→D, paralelos, anti-padrões, opcional `batch_stress`.

### 2026-03-29 — P3b implementado (frentes 1–4): replay explícito, paginação, auditoria BT, JSONL do agente

- **`binance_public`:** `klines_fetch_last_n` — últimas N velas com paginação (1000/request); `pause_sec` opcional via YAML.
- **`backtest.run_backtest`:** `deepcopy(cfg)`; parâmetros `kline_interval`, `bars` (default/max no YAML), `step_every`, `stress_preset`, `end_time_ms` (API); `bt_run_id`; `replay_spec`, `summary`, `audit_csv`, `agent_decisions_jsonl`; escrita opcional em `auditoria/backtest/{id}_audit.csv` e `_agent.jsonl`.
- **`config/default.yaml`:** `backtest.default_bars`, `max_bars`, `klines_pagination_pause_sec`, `stress_presets` (`baseline`, `livro_apertado`, `taxas_altas`).
- **`app.py`:** expander lateral **Backtest — replay histórico**; página Backtest com métricas de sumário, JSON sem blobs grandes, botões de download CSV/JSONL.
- **A atualizar na próxima sessão:** presets adicionais opcionais; encerramento formal P3b (secção **Encerramento da fase P3b**, blocos A–D); próximo epic após D (Frente 6 IA vs papel vs prep P4).
- **2026-03-29 (produto):** stress em lote **só CLI**; não haverá expander “Stress em lote” na `app.py`.
- **2026-03-29 (batch stress):** `microtrans.batch_stress` — CSV comparativo por preset (PnL, ciclos, % apto, top2 falhas papel / fins de ciclo, paths de auditoria).
- **2026-03-29 (continuação):** UI — checkbox **Fixar instante final (UTC)** + data/hora → `run_backtest(..., end_time_ms=…)`; `replay_spec.end_time_utc_iso`; log de backtest com `end_utc` quando fixo.
- **2026-03-29 (P3b+):** `BinancePublic.klines_fetch_range` (início→fim UTC, paginação); `run_backtest(start_time_ms=..., force_agent_heuristic=...)`; sumário com `paper_failure_reason_counts`, `cycle_end_reason_counts`, `klines_fetch`; UI: modo **intervalo** vs **últimas N**, checkbox forçar heurística no replay, expander diagnóstico na página Backtest.
- **2026-03-29 (evidência utilizador):** `bt_e9a6597f2c0f` — 5× `agent_gemini` num replay longo (múltiplos ciclos), `book_top`, CSV alinhado ao JSONL. `bt_aa0f6ab91666` — replay longo em vela **3m** (março), **~110** decisões `agent_heuristic`, `cycle_end`/`filters_failed` visíveis no CSV — confirma intervalo + heurística forçada sem custo Gemini.

### 2026-03-29 — Decisão estratégica: P3b (backtest + auditoria + registo de IA) antes de nova onda só em LLM

- **Registo:** secção “Decisão estratégica” e “Fluxo lógico de trabalho” neste ficheiro; linha **P3b** no quadro de fases.
- **Motivo:** reprodutibilidade e comparabilidade entre mercados/janelas; evolução da IA baseada em evidência; stress no papel antes de execução real.
- **A atualizar na próxima sessão:** iniciar implementação pela **frente 1** (contrato do replay na UI + docs) conforme tabela acima.

### 2026-03-29 — Revisão STARK (P2→P3): rede, prompt Gemini, logs 1×

**Evidência:** `auditoria/auditoria_ticks.csv` (`agent_gemini`, PnL papel); `teste/LOG12.TXT` (Gemini 2.5, `RemoteDisconnected` na vigília, logs 2×).

| Tarefa STARK | Sentido? | Implementação |
|---------------|----------|----------------|
| T1 Resiliência vigília | **Sim** — erro Binance não deve matar a sessão | `app.py`: exceções de rede → `WARNING` + `_vigil_backoff_until` +5s; vigília permanece ativa. Tick manual: `st.warning` sem encerrar app. |
| T2 Spread Gemini ≥ ~15 bps | **Sim** para *synthetic* / margem; **book_top** continua usando bid/ask real (paralisia por taxas pode persistir em majors — esperado) | `agent_gemini.py`: bloco CRÍTICO no prompt + `config/agent.gemini_min_spread_bps`; `agent_stub` faz clamp pós-LLM quando `agent_gemini`. |
| T3 Logs duplicados 2× | **Sim** — `setup_logging` punha `propagate=True` nos loggers `filter`/`agent` enquanto o handler da UI permanecia → mensagem no handler **e** no root | `logging_config.py`: `propagate=False` + `StreamHandler` só se logger vazio; `app._attach_ui_loggers`: `handlers.clear()` + um `StreamlitLogHandler`. |

**Execução real (P4):** ainda **fora de escopo**; “modo real” da revisão externa = interpretar como **book_top + taxas**, não ordens na corretora.

---

### 2026-03-29 — P3: agente Gemini

- Código: `src/microtrans/agent_gemini.py` (`propose_strategy_gemini`), `agent_stub.generate_strategy_once` desvia por `agent.provider`; dependência `google-generativeai`; UI: selectbox **Agente (P3) → Provedor**.
- Sem chave ou erro de API: fallback para heurística com `meta.source = agent_heuristic_gemini_fallback` e log de aviso.
- **Orçamento API:** `state.extra["gemini_budget_exhausted"]` — no máximo **uma** requisição Gemini por janela de abertura de ciclo; após sucesso ou `cycle_end` / mercado NÃO APTO, o motor zera o flag. Próximo ciclo pode chamar de novo. Heurística sem API quando o orçamento já foi gasto (`agent_heuristic_gemini_budget_spent`).
- UI: loggers anexados **uma vez** por sessão (`_microtrans_ui_loggers`); auditoria com coluna **`agent_fonte`** (`meta.source`).
- **A atualizar:** testes com chave real e ajuste de `gemini_model` se a API reclamar do id do modelo.

---

### 2026-03-29 — Modelo `gemini-2.5-flash` + chave só via env + loggers por flag

- **Padrão:** `agent.gemini_model: gemini-2.5-flash` (YAML + fallback em `agent_gemini.py`).
- **Segurança:** chaves apenas `GEMINI_API_KEY` / `GOOGLE_API_KEY` no ambiente — não commitar chaves no código.
- **Logs:** remoção de handlers de UI por `_is_microtrans_streamlit` (evita 80× linhas após reruns do Streamlit).
- **Evidência `audi02.csv` / `log11.txt`:** `agent_heuristic_gemini_fallback` ⇒ API não concluiu (chave/modelo/rede); `book_top` + skip de taxas em SOL ⇒ carteira estável; `log11` ainda mistura sessões (`synthetic_mid` no texto vs `book_top` no CSV) + duplicação corrigida no código.

---

### 2026-03-29 — Revisão STARK / JAR.V.I.S. (arquitetura UI + papel)

**Fonte:** relatório interno “Refatoração de motor e UI” (auditoria de engenharia).

| Item | Ação |
|------|------|
| Vigília bloqueante | Substituído `while + sleep` por **`st.fragment(run_every=timedelta(seconds=1))`** (requer Streamlit **≥ 1.33**); um tick por intervalo configurável; **log 1:1** (`VIGIL \| …`) na aba Logs; botão **Parar vigília**. |
| `synthetic_mid` nos CSV | **`config/default.yaml` → `paper_pricing: book_top`**; fallback em código **não** retorna mais silenciosamente a `synthetic_mid` (default `book_top`). UI mantém troca manual para `synthetic_mid`. |
| “State bleed” / carteira fantasma | Ao **trocar o par**, zera-se `last_out`, `audit_rows`, `log_lines`, `vigil_active` e recria carteira/motor (`_ecfg_key` invalidado). |
| Logs duplicados | Handlers: remove só entradas com `_is_microtrans_streamlit`, depois **um** `StreamlitLogHandler` por logger (sem `handlers.clear()` total). |
| LLM nesta etapa | **Não** integrado (conforme critério de aceitação do relatório). **Próximo:** API **Gemini** (`gemini_model` no YAML), não OpenAI. |

**A atualizar na próxima sessão:** validar export CSV com coluna `sim_precificacao=book_top` em sessão real; implementar `provider: gemini` quando P3 abrir.

---

### 2026-03-29 — Validação LOG08 + export CSV (pós-LOG07)

- **O que foi comprovado:** relax de liquidez destrava majors; motor em papel atualiza carteira e taxas; tabela de auditoria exporta corretamente (rastreabilidade humana).
- **Plano de negócio:** fechamento formal da **P2**; risco residual documentado: resultados em `synthetic_mid` não traduzem PnL real sem `book_top` + modelo de fill mais duro.
- **A atualizar na próxima sessão:** iniciar **P3** (LLM) ou endurecer papel vs real conforme prioridade.

---

### 2026-03-29 — Diagnóstico LOG07 / UX sidebar / correção book_top + slippage

**Problema:** carteira parada com `apt=sim` + `book_top` + slippage; vigília/UI; labels pouco claros.

**Causa raiz:** slippage simétrico > spread relativo do livro → `spread_collapsed_after_slip` → 0 ops.

**Correções:** teto + fallback no `book_top`; sidebar reorganizada; vigília só na sidebar (sem `key` duplicada); auditoria com colunas `sim_*`; passo a passo na UI.

---

### 2026-03-29 (manhã) — Papel ancorado ao livro, metas de ciclo

- `paper_pricing`, slippage, fill probabilístico; take profit %/quote; `tick(silent_logs=…)`; `handlers.clear()` nos loggers.

---

### Histórico resumido (iterações anteriores)

- YAML, loggers `filter` / `agent`, cliente Binance, filtros matemáticos, agente heurístico + contrato v1, `enforce_min_spread_for_fees`, backtest, UI estilo Binance, colunas mercado | carteira.

---

## Estado atual do sistema

| Área | Situação |
|------|----------|
| Dados | REST público Binance (klines, depth, ticker 24h) |
| Filtros | YAML + UI (liquidez relaxada para majors) |
| Agente | Heurística + **Gemini** (`gemini_min_spread_bps`, prompt com taxas do YAML); gate `finalize_agent_payload` |
| Motor | Papel; **padrão `book_top`** (slippage limitado + fallback); `synthetic_mid` opcional na UI |
| UI | Sidebar, vigília **fragment** + **backoff** em erro HTTP Binance; auditoria + CSV (`agent_fonte`) |
| Execução real | **Fora de escopo** (P4) |

---

## Próximo passo lógico (recomendado)

1. ~~**Gemini + resiliência + logs**~~ Feito — ver changelog STARK 2026-03-29.
2. ~~**Snapshot rico (`execution`, taxas, preview book_top)**~~ Feito — contrato v2 + `build_agent_execution_context`.
3. ~~**P3b núcleo**~~ Quase fechado — evidência: replays longos com CSV+JSONL coerentes (`bt_e9a6597f2c0f` Gemini; `bt_aa0f6ab91666` heurística + intervalo). Próximo incremento P3b opcional: **stress em lote**.
4. Endurecer papel (inventário, fila maker) **em paralelo opcional** após auditoria BT mínima, se não bloquear P3b.
5. **P4** (ordens reais) só com política explícita de risco, dry-run e critérios fechados **após** stress e evidência P3b.

---

## Como atualizar este arquivo (template)

```text
### AAAA-MM-DD — Título
- Evidências (logs, CSV, screenshots).
- O que mudou no código/config.
- Onde isso nos deixa no quadro P0–P4.
- **A atualizar na próxima sessão:** …
```

---

*Última edição: 2026-03-29 — Estado P3b: núcleo entregue; evidência CSV/JSONL (e9a6597f2c0f / aa0f6ab91666); próximo opcional: stress em lote.*

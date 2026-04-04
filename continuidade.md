# Continuidade — Microtrans 2.0 (P4t)

## Conclusão rápida

Estamos em **P4t (Testnet)** com UI funcional para soak/risco, e a prioridade imediata passou a ser **finalizar o soak em nuvem com login** para remover dependência do PC local.

Implementação desta sessão: login do painel entregue em `app.py` (env/secrets) + checklist de deploy em `DEPLOY_HETZNER_CHECKLIST.md`.
Atualização operacional: criado `scripts/setup_hetzner_remote.ps1` para setup remoto em um fluxo só (menos digitação manual).
Revisão de estratégia: soak longo não deve depender de aba aberta; execução oficial agora via scripts em background no servidor (`soak_start/stop/status`).
Fecho operacional desta etapa: subir UI como serviço `systemd` (auto-start + restart) para uso diário sem terminal.
Atualização mais recente: teste longo em nuvem concluído (~10h36, 2546 ciclos) e correção do botão **Parar teste** para atuar por PID mesmo fora da sessão atual da UI.

## Atualização desta sessão — liberação multi-ativo + vigília compatível

- Removida a barreira de símbolo fixo no executor testnet (`execution.allow_symbols: []`), permitindo explorar pares além de BTCUSDT.
- Vigília ajustada no `app.py` para modo compatível quando `st.fragment` não estiver disponível (mantém execução por `rerun` controlado).
- Objetivo de negócio: ampliar amostra de mercado para diagnóstico da **Frente 7**, reduzindo risco de decisões baseadas em um único ativo.
- Mantidos guardrails de risco: `max_notional_quote_per_order`, kill switch e auditoria por ciclo.

## Atualização desta sessão — Redesign UI sem regressão

- Foi formalizada a frente de redesign visual completo com identidade do produto:
  - nome: **MK BOT - MARKET MAKING**;
  - mascote: **lobo em pele de cordeiro**.
- Foi criado o guia operacional `GUIA_REDESIGN_UI_SEM_REGRESSAO.md` com regras de proteção para evitar quebra de funções consolidadas.
- O guia define:
  - o que pode mudar (visual, layout, copy, navegação);
  - o que não pode mudar (funções, contratos, chaves `session_state`, `key=` de widgets, auditoria/CSV/JSONL);
  - checklist de validação funcional (login, tick/vigília, backtest, soak/testnet, Telegram).
- Estratégia aprovada: executar redesign em paralelo ao P4t, sem contaminar o relatório de negócio.

## Fecho de design (estado atual)

- Ajustes finais de visual aplicados:
  - assinatura visual do mascote no branding;
  - reforço do nome do produto no login/topo;
  - botões com acabamento mais consistente e legível.
- Fluxo de navegação estabilizado:
  - menu lateral apenas como hub;
  - ações de operação concentradas nas páginas dedicadas.
- Resultado: plano de design considerado fechado nesta iteração, mantendo proteção anti-regressão.

## Atualização de validação P4t-6 (3x testes de 1h)

- Consolidado dos três testes manuais (240 ciclos cada): **720 ciclos**, **0 ordens efetivas**, bloqueios por `no_quote_filter_or_params` e `blocked_allowlist`.
- Infraestrutura operacional validada (GO para continuar em P4t), mas sem evidência econômica para live.
- Parecer formal registrado em `RELATORIO_P4T6_2026-04-03.md`:
  - **GO operacional (P4t)**;
  - **NO-GO econômico (P4 live por enquanto)**.

## Ajuste operacional desta sessão — Telegram com arquivos no fim do soak

- O bot agora envia automaticamente no encerramento do soak:
  - mensagem-resumo;
  - anexo `*_report.json`;
  - anexo `*_cycles.csv`.
- Objetivo: acelerar validação de cada teste sem depender de export manual da UI.
- Resultado validado em teste curto na VPS (mensagem + 2 anexos entregues).

## O que ficou pronto nesta sequência

- TestnetView com **start/stop**, limpeza de logs e limpeza de JSONL.
- Linha do tempo humana do soak + CSV resumido para auditoria.
- Card de carteira testnet com:
  - PnL mark-to-market;
  - PnL real do robô por fills (`myTrades`).
- Gestão de risco da sessão na UI:
  - banca;
  - risco máx por ordem;
  - meta de lucro;
  - stop loss.
- Provedor do agente visível no Testnet:
  - heurístico;
  - Gemini;
  - opção de forçar heurística.

## Diagnóstico confirmado (por logs/CSV)

- Em vários testes, o robô "parou" por **filtro de regime lateral** (`range_atr_out_of_band`), não por liquidez.
- Em vários testes, a ordem efetiva ficou pequena (ex.: `ordem_quote_aprox=5`), então o lucro por ciclo ficou irrisório.
- Houve casos de `status=quoted` com `ordens_postadas=0`, causando leitura confusa.

## Pendências críticas (prioridade)

1. **Semântica de status no executor — concluída**
   - `run_executor_once` agora separa:
     - `quoted_placed` (houve ordem efetiva);
     - `quoted_blocked_min_notional_balance` (ciclo apto, sem ordem por mínimo/saldo).
   - Auditoria por ciclo inclui `block_reason` por lado (`buy`/`sell`) para diagnóstico rápido.

2. **Paginação de fills no PnL real — concluída**
   - O cálculo de PnL real do robô na UI deixou de depender de uma única chamada `myTrades(limit=1000)`.
   - Agora há paginação por `fromId`, cobrindo sessões longas.

3. **Relatório P4t-6 (go/no-go) — em fechamento**
   - Relatório de soak já separa:
     - `quoted_total`;
     - `quoted_com_ordem`;
     - `quoted_sem_execução`;
     - ordens postadas, cancelamentos e erros.
   - Próximo passo: consolidar recomendação final de go/no-go com base nesses novos campos.

## Próximo passo recomendado (próximo chat)

Executar na ordem:

1. Configurar UI como serviço `systemd` no servidor (auto-start).
2. Rodar soak em nuvem (mínimo 6h; ideal 24h) com processo desacoplado.
3. Gerar relatório (`executor-report`) e validar consistência entre:
   - `quoted_com_ordem`;
   - `placed_orders_total`;
   - PnL real por fills.
4. Emitir parecer P4t-6 (go/no-go) no plano.
5. Só depois abrir epic dedicado de **estratégia de operação (market making)** para aumentar o lucro por ciclo (evitar lucros irrisórios), usando:
   - evidências de P3b (backtest/stress);
   - evidências de P4t (Testnet) sobre frequência de fills e tamanho efetivo das ordens.
6. Consolidar frente de **operação remota + celular** (monitoramento e alertas) já sobre a infraestrutura em nuvem.

## Setup recomendado para soak de 6h (UI Testnet)

- Símbolo: `BTCUSDT`
- Intervalo entre ciclos: `15` segundos
- Total de ciclos: `1440` (6h × 3600 / 15)
- Forçar heurística: **ligado**
- Provedor do agente: **Heurística local**
- Relaxar filtro de liquidez: **desligado** (simulação mais real/conservadora)
- Banca da sessão (quote): `100`
- Risco máx por ordem (%): `10` (teto por ordem ~`10` quote)
- Meta de lucro sessão (quote): `20`
- Stop loss sessão (quote): `20`

Observação: com filtro sem relaxamento, é esperado ter mais ciclos sem ordem (`no_quote_filter_or_params`), o que é parte da validação realista.

## Critério de pronto para avançar no plano

- O card de PnL real deve bater com o relatório de fills da sessão.
- `quoted` deve significar ordem realmente enviada (sem ambiguidade).
- Meta/stop devem atuar com base no PnL real consolidado, sem drift por limite de histórico.
- UI remota deve exigir login (sem exposição pública sem autenticação).
- Botão **Parar teste** deve encerrar soak mesmo após refresh da UI (controle por PID).

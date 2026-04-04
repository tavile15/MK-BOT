# Guia de Redesign UI sem Regressao — MK BOT - MARKET MAKING

Objetivo: reorganizar e modernizar a interface (visual + fluxo de navegacao) sem quebrar as funcoes consolidadas que ja rodam no servidor.

## 1) Escopo do redesign (permitido)

- Alterar estilo visual (cores, cards, espacamento, tipografia, hierarquia).
- Alterar textos visiveis ao usuario (titulos, descricoes, nomes de secoes).
- Adotar identidade visual do produto:
  - nome: **MK BOT - MARKET MAKING**;
  - mascote: **lobo em pele de cordeiro**;
  - referencias: pasta `indentidade_visual/`.
- Reorganizar layout por pagina, em blocos/containers dedicados.
- Transformar sidebar em **hub de navegacao puro** (sem botoes de acao).

## 2) Escopo bloqueado (proibido)

- Nao alterar assinatura de funcao de negocio (motor, filtros, executor, backtest).
- Nao alterar contratos de dados dos retornos de funcoes (`dict` com chaves ja usadas).
- Nao renomear chaves de `st.session_state` ja consolidadas.
- Nao renomear `key=` de widgets que alimentam logica existente.
- Nao mover logica de execucao para locais que mudem comportamento (tick, vigilia, soak, relatorios).
- Nao alterar formato de auditoria/CSV/JSONL.

## 3) Fluxo alvo de UX (definido pelo produto)

1. **Login**: usuario/senha + logo/mascote.
2. **Tela Inicial (Resumo)**:
   - resumo de carteira Testnet, Backtest e Vigilia;
   - indicadores de mercado (ex.: BTCUSDT, SOLUSDT);
   - foco em leitura executiva.
3. **Sidebar (hub)**:
   - somente navegacao: Tela Inicial, Backtest, Vigilia e Operacoes, Testnet, Logs;
   - sem configuracoes de execucao na sidebar.
4. **Pagina Backtest**:
   - bloco de parametros de replay;
   - bloco de saldos/metricas;
   - grafico principal de resultado;
   - logs exclusivos na pagina Logs.
5. **Pagina Vigilia e Operacoes**:
   - bloco de configuracao de vigilia;
   - bloco de resumo da vigilia;
   - bloco de entrada manual com um clique;
   - bloco de saldo/lucro/acoes + grafico.
6. **Pagina Testnet/Soak**:
   - bloco fixo da carteira completa;
   - bloco setup do soak (ativo, ciclos/tempo, agente, relax filtros);
   - bloco banca/risco;
   - linha de estado + tabela detalhada de operacoes.
7. **Pagina Logs**:
   - centralizar logs operacionais e auditoria textual.

## 4) Plano de execucao seguro (ordem obrigatoria)

1. **Camada visual global**: tema/tokens/identidade sem mudar logica.
2. **Sidebar hub**: somente navegacao, mantendo as mesmas rotas internas.
3. **Tela Inicial**: construir com leitura de dados existentes (read-only primeiro).
4. **Backtest**: reorganizar blocos mantendo os mesmos controles/chaves.
5. **Vigilia e Operacoes**: reorganizar blocos mantendo os mesmos gatilhos.
6. **Testnet/Soak**: reorganizar blocos mantendo start/stop/risk sem mudar regras.
7. **Logs**: consolidar logs visuais numa pagina dedicada.
8. **Deploy gradual**: validar local, depois servidor.

## 5) Checklist anti-regressao (gate de liberacao)

### 5.1 Login e acesso
- [ ] Login continua exigido quando habilitado via env/secrets.
- [ ] Credencial invalida bloqueia acesso.
- [ ] Credencial valida libera paginas.

### 5.2 Operacao manual (papel/vigilia)
- [ ] Botao de tick manual funciona.
- [ ] Vigilia inicia e para corretamente.
- [ ] Troca de par continua limpando estado sem "state bleed".
- [ ] Export CSV de auditoria continua disponivel.

### 5.3 Backtest
- [ ] Replay roda com heuristica.
- [ ] Replay roda com Gemini (quando chave valida).
- [ ] Download de `audit.csv` e `agent.jsonl` continua.
- [ ] Graficos e metricas batem com dados gerados.

### 5.4 Testnet/Soak
- [ ] Start/stop do soak funciona pela UI.
- [ ] Stop por PID continua funcional apos refresh.
- [ ] Meta/stop de sessao continua atuando.
- [ ] PnL real por fills continua calculando.
- [ ] Mensagem de encerramento no Telegram continua chegando.

### 5.5 Logs e observabilidade
- [ ] Logs aparecem apenas na pagina Logs.
- [ ] Sem duplicacao massiva de logs.
- [ ] Tabela/linha de status do soak continua coerente.

## 6) Testes de validacao (roteiro rapido)

## Teste A — Regressao minima (15-20 min)
- Login OK.
- 1 tick manual.
- Vigilia 3 ciclos e parada manual.
- Backtest curto (ex.: 120 barras).
- Soak curto testnet (ex.: 3 ciclos).
- Confirmar 1 alerta Telegram de encerramento.

## Teste B — Soak operacional (manual via UI)
- 1h em 15s: **240 ciclos**.
- Coletar CSV/JSONL.
- Verificar coerencia de `rows`, ordens, erros e PnL fills.

## Teste C — Go/No-Go de UX
- Operador executa fluxo completo sem abrir menus tecnicos escondidos.
- Sem confusao de navegacao.
- Sem impacto no resultado da carteira simulada/testnet.

## 7) Criterio de pronto do redesign

- Redesign entregue com identidade visual nova.
- Fluxo de navegacao aderente ao mapa de paginas aprovado.
- Todos os itens do checklist anti-regressao validados.
- Sem alteracao de comportamento em motor, executor, backtest e auditoria.

## 8) Observacao de negocio

UI bonita melhora operacao, mas nao prova viabilidade economica. A decisao de negocio continua dependente do relatorio P4t-6 (PnL, fills, erros, consistencia operacional).

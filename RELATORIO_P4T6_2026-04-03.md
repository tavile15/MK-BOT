# Relatório P4t-6 — Testnet (2026-04-03)

Conclusão: a operação em testnet está estável no servidor, mas o cenário atual não gerou ordens efetivas; portanto o parecer é **GO operacional para continuar em P4t** e **NO-GO econômico para P4 live neste momento**.

## Escopo avaliado

- Ambiente: Spot Testnet em nuvem (UI + executor desacoplado).
- Janela: 3 testes manuais de 1 hora cada.
- Configuração de tempo: 240 ciclos por teste (15s por ciclo).
- Evidências de entrada:
  - `TESTE 01.01 1 HORA.json` + `TESTE 01.01.csv`
  - `Teste 02 - 1 hora.json` + `Teste 02-1 hora.csv`
  - `TESTE 03.json` + `TESTE 03.csv`

## Resultado consolidado (3h)

- Ciclos totais: **720**
- `placed_orders_total`: **0**
- `quoted_placed_cycles`: **0**
- `no_quote_cycles`: **480**
- `blocked_allowlist_cycles`: **240**
- `error_rows`: **240** (todos associados a `blocked_allowlist` no Teste 02)

## Leitura por teste

### Teste 01 (BTCUSDT, 240 ciclos)

- `no_quote_filter_or_params`: 240
- ordens postadas: 0
- leitura: bloqueio por filtros/params; não houve execução de ordem.

### Teste 02 (SOLUSD, 240 ciclos)

- `blocked_allowlist`: 240
- ordens postadas: 0
- leitura: símbolo fora da allowlist atual de execução; erro operacional de setup, não de infraestrutura.

### Teste 03 (BTCUSDT, 240 ciclos)

- `no_quote_filter_or_params`: 240
- ordens postadas: 0
- leitura: repetição do padrão do Teste 01, sem execução efetiva.

## Parecer formal

- **P4t (operação/infrastrutura): GO**
  - servidor, UI, loop e coleta de evidência funcionam;
  - trilha de auditoria/CSV/JSON está consistente.
- **P4 live (viabilidade econômica): NO-GO**
  - sem ordens executadas, não há base para validar lucro não irrisório;
  - unit economics atual permanece indefinida para produção.

## Riscos e lacunas abertas

- Risco de falso negativo econômico enquanto filtros e allowlist impedem execução.
- PnL do robô segue próximo de zero por ausência de fills.
- Necessidade de separar claramente "bloqueio de configuração" de "bloqueio de mercado".

## Próximo passo recomendado

1. Fechar P4t-6 com este parecer no plano.
2. Abrir **Frente 7 — Estratégia de operação (market making)** para atacar:
   - política de filtros para aumentar taxa de execução útil;
   - parâmetros de tamanho/spread para sair de lucro irrisório;
   - critérios de teste comparáveis por janela (antes/depois).
3. Repetir bateria curta (ex.: 3x1h) após ajustes da Frente 7 e comparar com esta baseline.

## Critério para revisitar GO de live

- Haver execuções reais de ordens em testnet com consistência;
- PnL por fills positivo de forma repetível em múltiplas janelas;
- erros operacionais residuais sob controle (sem bloqueio estrutural de setup).

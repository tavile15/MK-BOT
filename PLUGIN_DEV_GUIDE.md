# Guia de Desenvolvimento de Plugins — Binance MK Engine

> **Versão do motor:** `binance_live_engine.py` v9  
> **Destinado a:** Programadores Python e Agentes de IA  
> Última revisão: 2026-05-07

---

## Índice

1. [Visão geral da arquitetura](#1-visão-geral-da-arquitetura)
2. [Estrutura obrigatória do arquivo plugin](#2-estrutura-obrigatória-do-arquivo-plugin)
3. [Constantes do módulo](#3-constantes-do-módulo)
4. [A função `propose(ctx)`](#4-a-função-proposectx)
   - 4.1 [Contexto de entrada — `ctx`](#41-contexto-de-entrada--ctx)
   - 4.2 [Proposta de saída — o retorno](#42-proposta-de-saída--o-retorno)
5. [Campos obrigatórios da proposta](#5-campos-obrigatórios-da-proposta)
6. [Campos opcionais — Operação assimétrica](#6-campos-opcionais--operação-assimétrica)
7. [Campos opcionais — Bloco `meta`](#7-campos-opcionais--bloco-meta)
8. [Boas práticas e regras de segurança](#8-boas-práticas-e-regras-de-segurança)
9. [Ciclo de vida completo de um ciclo](#9-ciclo-de-vida-completo-de-um-ciclo)
10. [Template mínimo comentado](#10-template-mínimo-comentado)
11. [Template completo com operação assimétrica](#11-template-completo-com-operação-assimétrica)
12. [Erros comuns e como evitá-los](#12-erros-comuns-e-como-evitá-los)
13. [Checklist de validação antes de usar](#13-checklist-de-validação-antes-de-usar)

---

## 1. Visão geral da arquitetura

O motor (`binance_live_engine.py`) é responsável por **toda a comunicação com a Binance**, controle de reservas, auditoria e timing de ciclos.

O plugin é responsável apenas por **decidir os parâmetros de cada ciclo de Market Making**: qual spread usar, qual tamanho de lote, se deve pausar, e se a operação é assimétrica.

```
┌─────────────────────────────────────────────────────────┐
│                    binance_live_engine.py                │
│                                                          │
│  A cada ciclo (interval_sec):                            │
│   1. Coleta dados de mercado (mid, ATR, ADX, liquidez)   │
│   2. Verifica ordens abertas / processa fills            │
│   3. Chama  →  plugin.propose(ctx)  ←                   │
│   4. Interpreta a proposta retornada                     │
│   5. Coloca ordens BID / ASK na exchange                 │
│   6. Registra tudo em auditoria (CSV + JSON)             │
└─────────────────────────────────────────────────────────┘
                           ↑ ↓
           ┌───────────────────────────────┐
           │      seu_plugin.py            │
           │  - STRATEGY_DISPLAY_NAME      │
           │  - TESTNET_SOAK_DEFAULTS      │
           │  - _SYMBOL_INDEX              │
           │  - def propose(ctx) -> dict   │
           └───────────────────────────────┘
```

**Regra absoluta:** O motor nunca modifica o plugin. O plugin nunca acessa a Binance diretamente.

---

## 2. Estrutura obrigatória do arquivo plugin

O arquivo deve ser um módulo Python (`.py`) com:

| Item | Tipo | Obrigatório | Descrição |
|------|------|-------------|-----------|
| `propose(ctx)` | função | **Sim** | Única função chamada pelo motor a cada ciclo |
| `STRATEGY_DISPLAY_NAME` | `str` | Recomendado | Nome exibido no dashboard e nos logs |
| `TESTNET_SOAK_DEFAULTS` | `dict` | Recomendado | Parâmetros de temporização do soak |
| `_SYMBOL_INDEX` | `dict` | Recomendado | Mapa de índice → símbolo negociado |

Um arquivo `.py` que **não contenha** uma função `propose()` callable é **rejeitado** pelo motor na seleção.

---

## 3. Constantes do módulo

### `STRATEGY_DISPLAY_NAME`

Nome de exibição da estratégia, mostrado no dashboard do terminal e na interface gráfica.

```python
STRATEGY_DISPLAY_NAME: str = "MINHA ESTRATÉGIA v1"
```

- Máximo recomendado: 30 caracteres
- Use apenas ASCII para garantir compatibilidade com terminais Windows

---

### `TESTNET_SOAK_DEFAULTS`

Dicionário que controla o comportamento do soak (sessão de execução).

```python
TESTNET_SOAK_DEFAULTS: dict = {
    "interval_sec":             15,   # segundos entre ciclos
    "resting_order_min_age_sec": 30,  # idade mínima para cancelar uma ordem
    "max_cycles":               200,  # 0 = ilimitado
}
```

| Campo | Tipo | Padrão | Descrição |
|-------|------|--------|-----------|
| `interval_sec` | `int` / `float` | `8` | Intervalo em segundos entre ciclos de trading |
| `resting_order_min_age_sec` | `int` / `float` | `16` | Tempo mínimo (segundos) que uma ordem deve ter antes de ser cancelada pelo motor |
| `max_cycles` | `int` | `0` | Número máximo de ciclos; `0` = sem limite |

> **Regra crítica:** `resting_order_min_age_sec` **deve ser ≥ `interval_sec`**.  
> Se for menor, todas as ordens serão canceladas a cada ciclo sem chance de serem executadas.  
> O motor emite aviso mas não bloqueia — cabe ao plugin definir valores coerentes.

**Recomendação:** `resting = interval * 2` como ponto de partida.

---

### `_SYMBOL_INDEX`

Mapa de inteiros para símbolos negociados. Permite que o motor ofereça ao usuário um menu de seleção de ativo.

```python
_SYMBOL_INDEX: dict = {
    0: "BTCUSDT",
    1: "ETHUSDT",
    2: "BNBUSDT",
}
```

- Chaves: inteiros (`int`), começando em `0`
- Valores: strings de símbolo válido na Binance (ex: `"BTCUSDT"`)
- Chaves não precisam ser contíguas (ex: `{0: "BTC...", 5: "ETH..."}` é aceito)
- Se ausente, o motor usa `"BTCUSDT"` como padrão

---

## 4. A função `propose(ctx)`

### Assinatura

```python
def propose(ctx: dict) -> dict:
    ...
```

- **Chamada:** uma vez por ciclo, pelo motor
- **Argumento:** `ctx` — dicionário somente leitura com dados de mercado e estado da banca
- **Retorno:** dicionário com os parâmetros da operação deste ciclo
- **Exceções:** a função **nunca deve lançar exceção**; o motor não tem try/except em torno de `propose()`

---

### 4.1 Contexto de entrada — `ctx`

O dicionário `ctx` tem a seguinte estrutura:

```python
ctx = {
    "symbol": "BTCUSDT",          # str — símbolo sendo negociado

    "metrics": {
        "atr_pct":         0.0082,  # float — ATR como fração do preço (0.82% = 0.0082)
        "adx":             28.5,    # float — ADX (0–100); > 25 = tendência forte
        "liquidity_quote": 145000,  # float — profundidade bid top-5 em USDT
    },

    "execution": {
        "mid":        95234.50,   # float — preço médio (bid+ask)/2
        "mid_price":  95234.50,   # float — mesmo que "mid" (alias)
        "last_price": 95234.50,   # float — mesmo que "mid" (alias)

        "bid_active": True,       # bool — há uma ordem BID ativa no motor
        "ask_active": False,      # bool — há uma ordem ASK ativa no motor

        # Banca virtual
        "virtual_bank_initial_quote":    1000.0,   # float — capital inicial (USDT)
        "virtual_bank_equity_quote_now": 1002.5,   # float — equity atual mark-to-market
        "virtual_bank_pnl_quote":           2.5,   # float — PnL total (pode ser negativo)
        "inventory_base":              0.00105,    # float — saldo base acumulado (ex: BTC)

        "plugin_ui": {
            "target_symbol_idx": 0.0,  # float — índice selecionado pelo usuário
            "preset_mode":       0.0,  # float — modo selecionado (sempre 0 na v9)
        },
    },
}
```

#### Notas importantes sobre `ctx`

- **`atr_pct`** é uma fração, não porcentagem: `0.0082` significa `0.82%`. Para converter: `atr_pct * 100`.
- **`adx`** pode ser `0.0` nos primeiros ciclos (warm-up do indicador). Trate `adx == 0` como "dado indisponível".
- **`mid`** pode ser `0.0` em falha de conectividade — o motor ignora o ciclo antes de chamar `propose()` nesse caso, mas por segurança valide sempre.
- **`bid_active` / `ask_active`** permitem que o plugin tome decisões assimétricas com base no que já está na exchange.
- **`inventory_base`** é o saldo acumulado em base (ex: BTC). Fundamental para estratégias de skew por inventário.

---

### 4.2 Proposta de saída — o retorno

O retorno de `propose()` é um dicionário com os parâmetros que o motor vai usar para operar naquele ciclo.

Estrutura completa:

```python
return {
    # ── Obrigatórios ────────────────────────────────────────────────────
    "spread_bps":        20.0,   # float — spread simétrico em basis points
    "order_size_quote":  15.0,   # float — tamanho do lote em USDT

    # ── Opcionais: operação assimétrica ─────────────────────────────────
    "bid_spread_bps":    18.0,   # float — spread do BID (sobrescreve spread_bps)
    "ask_spread_bps":    25.0,   # float — spread do ASK (sobrescreve spread_bps)
    "bid_size_quote":    20.0,   # float — lote do BID em USDT (sobrescreve order_size_quote)
    "ask_size_quote":    10.0,   # float — lote do ASK em USDT (sobrescreve order_size_quote)
    "only_bid":          False,  # bool  — se True, não posta ASK neste ciclo
    "only_ask":          False,  # bool  — se True, não posta BID neste ciclo

    # ── Opcional: controle de operação ──────────────────────────────────
    "meta": {
        "skip":         False,   # bool  — True = pausa tudo (parking)
        "skip_reason":  "",      # str   — motivo do parking (exibido no dashboard)
        "skew":         "NEUTRO",# str   — rótulo de skew (apenas display)
        "skew_status":  "NEUTRO",# str   — alternativa a "skew" (lido preferencial)
        "profit_lock":  False,   # bool  — True = exibe banner de profit-lock no dashboard
    },
}
```

---

## 5. Campos obrigatórios da proposta

### `spread_bps` — `float`

Spread em **basis points** (bps) a ser aplicado ao mid price para calcular os preços das ordens.

```
bid_price = mid * (1 - spread_bps / 20000)
ask_price = mid * (1 + spread_bps / 20000)
```

> Por que `/ 20000`? Um spread de `N bps` *total* implica `N/2 bps` de cada lado. Um basis point = 0.0001. Portanto: `N_bps / 2 * 0.0001 = N_bps / 20000`.

**Restrições:**
- Deve ser `> 0`
- Valores abaixo de `5 bps` são arriscados (possível spread negativo por slippage de tick)
- Valores acima de `500 bps` costumam impedir execução

**Exemplo:** `spread_bps = 20` → BID 0.10% abaixo, ASK 0.10% acima do mid.

---

### `order_size_quote` — `float`

Tamanho de cada ordem em **USDT** (quote currency).

**Restrições:**
- Deve ser `> 0`
- Deve ser `≥ min_notional` da exchange (geralmente **10 USDT** na Binance)
- Se menor que `min_notional`, a ordem é silenciosamente rejeitada pelo motor
- Não deve exceder `virtual_bank_equity_quote_now` (o motor verifica `available_quote` automaticamente, mas é boa prática validar no plugin)

**Recomendação mínima:** `12.0` USDT para garantir margem de segurança acima do `min_notional`.

---

## 6. Campos opcionais — Operação assimétrica

Quando fornecidos, os campos assimétricos **sobrescrevem** os simétricos para o lado correspondente.

| Campo | Sobrescreve | Descrição |
|-------|------------|-----------|
| `bid_spread_bps` | `spread_bps` para BID | Spread independente para a ordem de compra |
| `ask_spread_bps` | `spread_bps` para ASK | Spread independente para a ordem de venda |
| `bid_size_quote` | `order_size_quote` para BID | Lote independente para BID em USDT |
| `ask_size_quote` | `order_size_quote` para ASK | Lote independente para ASK em USDT |
| `only_bid` | — | `True` = não posta ASK neste ciclo |
| `only_ask` | — | `True` = não posta BID neste ciclo |

**Lógica de fallback do motor:**

```python
bid_spread = proposta.get("bid_spread_bps", spread_bps)   # fallback para simétrico
ask_spread = proposta.get("ask_spread_bps", spread_bps)
bid_size   = proposta.get("bid_size_quote",  size_usdt)
ask_size   = proposta.get("ask_size_quote",  size_usdt)
only_bid   = proposta.get("only_bid", False)
only_ask   = proposta.get("only_ask", False)
```

**Exemplo — skew por inventário:**

```python
# Inventário alto de BTC → forçar mais vendas (ASK menor, BID maior)
inventory = ctx["execution"]["inventory_base"]
mid       = ctx["execution"]["mid"]
inv_usdt  = inventory * mid

if inv_usdt > 50:   # mais de $50 de base acumulada
    return {
        "spread_bps":       20,
        "order_size_quote": 15,
        "bid_spread_bps":   35,   # BID mais distante (menos agressivo em compra)
        "ask_spread_bps":   10,   # ASK mais próximo (mais agressivo em venda)
        "bid_size_quote":   10,
        "ask_size_quote":   20,
    }
```

---

## 7. Campos opcionais — Bloco `meta`

O bloco `meta` controla comportamentos especiais do ciclo.

### `meta["skip"]` — `bool`

Quando `True`, o motor:
1. **Cancela** imediatamente as ordens BID e ASK abertas
2. **Não posta** nenhuma nova ordem neste ciclo
3. Registra o ciclo como "PARKING" nos logs

Use quando o plugin detectar condições desfavoráveis (tendência forte, spread de mercado excessivo, horário de baixa liquidez, etc.).

```python
"meta": {
    "skip": True,
    "skip_reason": "ADX > 40: tendência forte, operação pausada",
}
```

### `meta["skip_reason"]` — `str`

Mensagem exibida no dashboard e nos logs durante o parking. Máximo recomendado: 60 caracteres.

Palavras especiais que alteram a cor de exibição no terminal:
- Se contém `"STOP_LOSS"` → exibido em vermelho
- Outros → exibido em amarelo

### `meta["skew"]` / `meta["skew_status"]` — `str`

Rótulo informativo de skew exibido no dashboard. Apenas cosmético, sem efeito operacional.

- O motor lê `skew_status` primeiro; se ausente, lê `skew`
- Valores sugeridos: `"NEUTRO"`, `"BID_HEAVY"`, `"ASK_HEAVY"`, `"REDUZIDO"`

### `meta["profit_lock"]` — `bool`

Quando `True`, exibe um banner `★ PROFIT LOCK ATIVO` no dashboard. Apenas cosmético.

---

## 8. Boas práticas e regras de segurança

### Regra 1 — `propose()` nunca pode lançar exceção

O motor chama `propose(ctx)` **sem try/except**. Uma exceção não capturada no plugin encerra o ciclo com erro, incrementa o contador de ciclos e pode causar comportamento imprevisível nas ordens abertas.

```python
# ✗ ERRADO
def propose(ctx):
    ratio = ctx["execution"]["virtual_bank_pnl_quote"] / ctx["execution"]["some_field"]
    # KeyError se some_field não existir → motor trava

# ✓ CORRETO
def propose(ctx):
    try:
        ratio = ...
    except Exception:
        ratio = 1.0   # valor seguro de fallback
```

### Regra 2 — Todos os valores numéricos devem ser `float` finitos

Nunca retorne `None`, `float("nan")`, `float("inf")`, ou `0` para campos que o motor espera como `float`.

```python
# ✓ CORRETO — sempre retorne valores finitos e positivos
import math

spread = calcular_spread()
if not math.isfinite(spread) or spread <= 0:
    spread = 20.0   # fallback seguro
```

### Regra 3 — Trate `mid == 0` e indicadores `== 0` no warm-up

Nos primeiros ciclos, `atr_pct` e `adx` podem ser `0.0` (indicadores ainda em warm-up). O plugin deve ter um comportamento seguro para esses casos.

```python
atr_pct = ctx["metrics"]["atr_pct"]
adx     = ctx["metrics"]["adx"]

# Indicadores ainda não disponíveis → operar conservadoramente
if atr_pct == 0.0 or adx == 0.0:
    return {
        "spread_bps":       30,   # spread amplo e conservador
        "order_size_quote": 12,
        "meta": {"skew": "WARMUP"},
    }
```

### Regra 4 — Não acesse a Binance diretamente

O plugin não deve importar `binance.client.Client` nem fazer chamadas de rede. Toda informação necessária está em `ctx`.

```python
# ✗ ERRADO
from binance.client import Client
def propose(ctx):
    c = Client(...)
    ticker = c.get_symbol_ticker(...)   # nunca faça isso

# ✓ CORRETO — use apenas ctx
def propose(ctx):
    mid = ctx["execution"]["mid"]
```

### Regra 5 — `resting_order_min_age_sec >= interval_sec`

Se o resting for menor que o intervalo, o motor cancelará todas as ordens a cada ciclo. Isso esvazia a banca rapidamente via taxas e impede qualquer execução.

```python
# ✓ CORRETO — resting = 2× o intervalo
TESTNET_SOAK_DEFAULTS = {
    "interval_sec":              15,
    "resting_order_min_age_sec": 30,   # sempre >= interval_sec
    "max_cycles":                100,
}
```

### Regra 6 — `order_size_quote >= 10 USDT`

A Binance rejeita ordens com notional abaixo do `min_notional` (geralmente 10 USDT). O motor filtra isso, mas uma `order_size_quote` muito baixa resulta em **zero ordens colocadas**. Use no mínimo `12.0` como margem de segurança.

### Regra 7 — Não use estado global mutável

Se o plugin precisar de estado entre ciclos (ex: contador de fills consecutivos), use variáveis no escopo do módulo de forma segura e documente claramente.

```python
# ✓ CORRETO — estado de módulo bem definido
_consecutive_skips: int = 0

def propose(ctx):
    global _consecutive_skips
    ...
```

### Regra 8 — Evite `only_bid=True` e `only_ask=True` simultaneamente

Se ambos forem `True`, o motor não posta nenhuma ordem e o ciclo fica ocioso. Equivale a `meta["skip"] = True` mas sem cancelar as ordens existentes.

---

## 9. Ciclo de vida completo de um ciclo

```
Motor                                       Plugin
  │                                            │
  ├─ 1. Coleta mid, ATR, ADX, liquidez        │
  ├─ 2. Verifica ordens abertas               │
  │    └─ Processa fills novos                │
  │    └─ Cancela ordens antigas (resting)    │
  ├─ 3. Monta ctx ─────────────────────────► │
  │                                            ├─ Lê ctx
  │                                            ├─ Calcula spread, lote, skew
  │                                            ├─ Decide skip se necessário
  │                                   ◄─────── └─ Retorna proposta (dict)
  ├─ 4. Lê proposta                           │
  │    └─ Se skip=True: cancela e pausa       │
  │    └─ Se skip=False:                      │
  │         └─ Posta BID (se não ativo)       │
  │         └─ Posta ASK (se há base)         │
  ├─ 5. Auditoria (CSV + JSON)                │
  ├─ 6. Dashboard (terminal / GUI)            │
  └─ 7. Sleep(interval_sec - elapsed)         │
```

---

## 10. Template mínimo comentado

```python
"""
meu_plugin_v1.py — Plugin mínimo para o Motor Binance MK Engine
"""

# ─── Metadados do plugin ────────────────────────────────────────────────────

STRATEGY_DISPLAY_NAME: str = "MEU PLUGIN v1"

TESTNET_SOAK_DEFAULTS: dict = {
    "interval_sec":              15,   # ciclo de 15 segundos
    "resting_order_min_age_sec": 30,   # cancela ordens com > 30s (≥ interval)
    "max_cycles":               100,   # 100 ciclos (~25 minutos)
}

_SYMBOL_INDEX: dict = {
    0: "BTCUSDT",
    1: "ETHUSDT",
}

# ─── Função principal ────────────────────────────────────────────────────────

def propose(ctx: dict) -> dict:
    """
    Chamada pelo motor a cada ciclo.
    Recebe o contexto de mercado e retorna os parâmetros da operação.
    """
    # Lê dados do contexto com fallback seguro
    mid     = float(ctx["execution"].get("mid", 0.0))
    atr_pct = float(ctx["metrics"].get("atr_pct", 0.0))
    adx     = float(ctx["metrics"].get("adx", 0.0))

    # Segurança: mid inválido → pausa
    if mid <= 0:
        return {
            "spread_bps":       20,
            "order_size_quote": 12,
            "meta": {"skip": True, "skip_reason": "mid=0"},
        }

    # Spread base = 2× o ATR em bps (mínimo 15 bps, máximo 80 bps)
    atr_bps = atr_pct * 10_000 * 2   # converte ATR fração → basis points (×2 por lado)
    spread  = max(15.0, min(80.0, atr_bps))

    # Pausa em tendência forte
    if adx > 40 and adx != 0.0:
        return {
            "spread_bps":       spread,
            "order_size_quote": 12,
            "meta": {
                "skip":        True,
                "skip_reason": f"ADX={adx:.0f}: tendência forte",
                "skew":        "PAUSA",
            },
        }

    # Operação normal
    return {
        "spread_bps":       spread,
        "order_size_quote": 15.0,
        "meta": {
            "skip":  False,
            "skew":  "NEUTRO",
        },
    }
```

---

## 11. Template completo com operação assimétrica

```python
"""
meu_plugin_asym_v1.py — Plugin com skew de inventário e spread dinâmico
"""
import math

STRATEGY_DISPLAY_NAME: str = "ASSIMÉTRICO v1"

TESTNET_SOAK_DEFAULTS: dict = {
    "interval_sec":              20,
    "resting_order_min_age_sec": 45,
    "max_cycles":               200,
}

_SYMBOL_INDEX: dict = {
    0: "BTCUSDT",
    1: "ETHUSDT",
}

# Parâmetros da estratégia
_BASE_SPREAD_BPS   = 18.0    # spread base em mercado neutro
_MIN_SPREAD_BPS    = 10.0    # piso do spread
_MAX_SPREAD_BPS    = 100.0   # teto do spread
_ORDER_SIZE_USDT   = 15.0    # tamanho padrão do lote
_MAX_INV_USDT      = 60.0    # inventário máximo em USDT antes de inclinar fortemente
_ADX_PAUSE_THRESH  = 45.0    # ADX acima disto → pausa


def propose(ctx: dict) -> dict:
    """Proposta de MK assimétrica com skew por inventário e spread dinâmico por ATR."""

    # ── Leitura segura do contexto ────────────────────────────────────────
    try:
        mid       = float(ctx["execution"]["mid"])
        atr_pct   = float(ctx["metrics"]["atr_pct"])
        adx       = float(ctx["metrics"]["adx"])
        liq       = float(ctx["metrics"]["liquidity_quote"])
        pnl       = float(ctx["execution"]["virtual_bank_pnl_quote"])
        equity    = float(ctx["execution"]["virtual_bank_equity_quote_now"])
        inv_base  = float(ctx["execution"]["inventory_base"])
        bid_alive = bool(ctx["execution"]["bid_active"])
        ask_alive = bool(ctx["execution"]["ask_active"])
    except (KeyError, TypeError, ValueError):
        # Contexto malformado: retorno conservador
        return {
            "spread_bps":       _BASE_SPREAD_BPS,
            "order_size_quote": _ORDER_SIZE_USDT,
            "meta": {"skip": False, "skew": "CTX_ERR"},
        }

    # ── Validações ────────────────────────────────────────────────────────
    if mid <= 0 or not math.isfinite(mid):
        return {
            "spread_bps":       _BASE_SPREAD_BPS,
            "order_size_quote": _ORDER_SIZE_USDT,
            "meta": {"skip": True, "skip_reason": "mid inválido"},
        }

    # ── Pausa por tendência forte ─────────────────────────────────────────
    if adx > _ADX_PAUSE_THRESH and adx != 0.0:
        return {
            "spread_bps":       _BASE_SPREAD_BPS,
            "order_size_quote": _ORDER_SIZE_USDT,
            "meta": {
                "skip":        True,
                "skip_reason": f"ADX={adx:.0f} > {_ADX_PAUSE_THRESH:.0f}",
                "skew":        "PAUSA_TENDÊNCIA",
            },
        }

    # ── Spread dinâmico baseado em ATR ────────────────────────────────────
    # ATR em bps * 2 (cada lado) + margem de 5 bps
    atr_bps = atr_pct * 10_000 * 2 + 5.0 if atr_pct > 0 else _BASE_SPREAD_BPS
    spread  = max(_MIN_SPREAD_BPS, min(_MAX_SPREAD_BPS, atr_bps))

    # ── Skew de inventário ────────────────────────────────────────────────
    inv_usdt   = inv_base * mid
    skew_label = "NEUTRO"
    bid_spread = spread
    ask_spread = spread
    bid_size   = _ORDER_SIZE_USDT
    ask_size   = _ORDER_SIZE_USDT
    only_bid   = False
    only_ask   = False

    if inv_usdt > _MAX_INV_USDT:
        # Inventário excessivo → favorecer vendas
        bid_spread = spread * 1.8    # BID mais distante (menos agressivo em compra)
        ask_spread = spread * 0.6    # ASK mais próximo (mais agressivo em venda)
        bid_size   = _ORDER_SIZE_USDT * 0.5
        ask_size   = _ORDER_SIZE_USDT * 1.5
        skew_label = "ASK_HEAVY"

    elif inv_usdt < -10:
        # Posição vendida acima de $10 → favorecer compras
        bid_spread = spread * 0.7
        ask_spread = spread * 1.5
        skew_label = "BID_HEAVY"

    # ── Proteção de lote mínimo ───────────────────────────────────────────
    bid_size = max(12.0, bid_size)
    ask_size = max(12.0, ask_size)

    # ── Profit lock: reduz exposição se PnL > 2% da equity ───────────────
    profit_lock = False
    if equity > 0 and pnl / equity > 0.02:
        bid_size   = min(bid_size, 10.0)
        ask_size   = min(ask_size, 10.0)
        profit_lock = True

    return {
        "spread_bps":       spread,
        "order_size_quote": _ORDER_SIZE_USDT,

        # Assimétrico
        "bid_spread_bps":   bid_spread,
        "ask_spread_bps":   ask_spread,
        "bid_size_quote":   bid_size,
        "ask_size_quote":   ask_size,
        "only_bid":         only_bid,
        "only_ask":         only_ask,

        "meta": {
            "skip":        False,
            "skew":        skew_label,
            "skew_status": skew_label,
            "profit_lock": profit_lock,
        },
    }
```

---

## 12. Erros comuns e como evitá-los

| Erro | Sintoma | Causa | Solução |
|------|---------|-------|---------|
| `resting < interval` | Nenhuma ordem é executada; todas canceladas imediatamente | `resting_order_min_age_sec` menor que `interval_sec` | Definir `resting >= interval * 2` |
| `order_size_quote < 10` | Zero ordens colocadas, saldo sem uso | Abaixo do `min_notional` da Binance | Usar `>= 12.0` |
| `spread_bps = 0` | Preços bid = ask = mid; ordens rejeitadas pelo motor | Spread zerado | Garantir `spread_bps > 0` sempre |
| Exceção em `propose()` | Ciclo abortado com erro no log; motor continua mas ordens anteriores ficam abertas | Bug no plugin | Envolver todo o corpo em `try/except` |
| Retornar `None` nos campos | `TypeError` no motor ao tentar `float(None)` | Plugin retorna `None` em vez de `0.0` | Usar `or 0.0` nos fallbacks |
| `meta["skip"]` nunca resetado | Motor fica em parking indefinido | Condição de skip sem saída | Garantir que skip=False é alcançável |
| `only_bid=True` + `only_ask=True` | Ciclo ocioso sem ordens | Ambos True ao mesmo tempo | Nunca setar os dois True simultaneamente |
| Estado global entre sessões | Comportamento inesperado na segunda sessão | Variáveis de módulo não resetadas entre soaks | Usar `_reset()` chamado no início de `propose()` ou verificar o `cycle` |

---

## 13. Checklist de validação antes de usar

Antes de colocar um plugin em operação, verifique todos os itens:

### Estrutura
- [ ] Arquivo `.py` com a função `propose(ctx)` definida
- [ ] `propose` é callable (não é uma classe, não é `None`)
- [ ] `STRATEGY_DISPLAY_NAME` definido como string
- [ ] `TESTNET_SOAK_DEFAULTS` definido com os três campos obrigatórios
- [ ] `_SYMBOL_INDEX` definido com pelo menos `{0: "ALGOUSDT"}`

### Temporização
- [ ] `resting_order_min_age_sec >= interval_sec`
- [ ] `interval_sec >= 5` (valores menores podem causar rate-limit na Binance)
- [ ] `max_cycles > 0` (ou `0` para ilimitado, consciente disso)

### `propose()` — Saída
- [ ] Sempre retorna `spread_bps > 0`
- [ ] Sempre retorna `order_size_quote >= 12.0`
- [ ] Nenhum campo retorna `None`, `NaN` ou `Inf`
- [ ] `bid_size_quote >= 12.0` quando fornecido
- [ ] `ask_size_quote >= 12.0` quando fornecido
- [ ] `only_bid` e `only_ask` não são `True` ao mesmo tempo

### `propose()` — Robustez
- [ ] Função inteira protegida por `try/except` com retorno de fallback
- [ ] `mid == 0` tratado (retornar skip ou spread seguro)
- [ ] `atr_pct == 0` tratado (warm-up dos indicadores)
- [ ] `adx == 0` tratado (warm-up dos indicadores)
- [ ] Nenhuma chamada de rede dentro de `propose()`
- [ ] Nenhum `import` dinâmico dentro de `propose()`

### Segurança
- [ ] Testado em modo **Testnet** antes de qualquer operação Live
- [ ] `max_cycles` limitado na primeira execução (ex: `50`) para validar comportamento
- [ ] `order_size_quote` pequeno na primeira execução (ex: `12.0`)
- [ ] Revisado o comportamento de `meta["skip"]`: há condição que retorna `False`?

---

*Este documento descreve a interface do `binance_live_engine.py` v9.*  
*Mudanças na assinatura de `_build_ctx` ou em `_place_orders` podem alterar campos disponíveis — verifique sempre o código-fonte do motor.*

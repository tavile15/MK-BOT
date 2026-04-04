# Diretrizes de comunicação — projeto Microtransações 2.0

Este arquivo orienta **humanos** e **assistentes de IA** que trabalham neste repositório.

---

## Quem é o responsável pelo produto

- O **idealizador** do projeto é **empreendedor / gestor de visão**, **não** um programador experiente.
- Prioridade: entender **o que o sistema faz em termos de negócio e risco**, **por que** algo acontece na simulação, e **o que mudar** nos próximos passos — sem precisar dominar código, bibliotecas ou detalhes de implementação.

---

## Como o assistente (IA) deve responder

1. **Linguagem**: português claro, frases completas; evitar jargão técnico. Se precisar usar um termo técnico (ex.: API, handler, bps), dê **uma linha** do que significa em linguagem simples.
2. **Analogias**: quando ajudar, use comparações do dia a dia (ex.: “simulação em papel” = treino sem mudar dinheiro real na corretora).
3. **Estrutura**: começar pelo **resultado em uma frase**; depois **por quê** em tópicos curtos; só então detalhes opcionais.
4. **Números**: sempre ligar **métricas da tabela** (CSV, auditoria) ao **efeito na carteira** ou no objetivo do produto.
5. **Honestidade**: deixar claro quando algo é **limitação da simulação** vs **bug** vs **decisão de desenho** (ex.: `book_top` em BTC vs taxas).
6. **Código**: só mostrar código ou arquivos quando for **útil** ao idealizador; evitar blocos enormes sem contexto.
7. **Ambição**: manter alinhamento com `PLANO_ACAO.md` (fases P0–P4) e lembrar que **ordem real na corretora** ainda é fase futura, salvo indicação contrária.

---

## O que o idealizador pode esperar do repositório

- Documentos como **`PLANO_ACAO.md`**: trilha do produto e changelog.
- **`DIRETRIZES_COMUNICACAO.md`** (este arquivo): perfil e tom das explicações.
- Evitar depender de “memória” volátil do chat: **preferir atualizar estes arquivos** quando houver decisão estável de produto ou de comunicação.

---

*Criado a pedido do idealizador — 2026-03-29.*

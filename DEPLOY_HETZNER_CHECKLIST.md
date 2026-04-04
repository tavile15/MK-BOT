# Deploy em nuvem (Hetzner) — checklist rápido

Objetivo: deixar o `MK-BOT-IA` rodando 24/7 em Testnet, com acesso por celular e login obrigatório.

## 1) Criar o servidor

- Provedor: Hetzner Cloud.
- Plano inicial recomendado: `CPX11` (2 vCPU, 2 GB RAM) ou superior.
- Sistema: Ubuntu 22.04 LTS.
- Região: a mais barata/estável para ti (latência não é crítica em Testnet).

## 2) Acesso e preparo do sistema

```bash
ssh root@SEU_IP
apt update && apt upgrade -y
apt install -y git python3 python3-venv python3-pip tmux ufw
```

## 3) Clonar projeto e instalar dependências

```bash
mkdir -p /opt && cd /opt
git clone <URL_DO_REPO> mk-bot-ia
cd /opt/mk-bot-ia
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4) Configurar credenciais e login (NÃO commitar)

Crie o arquivo `.env.testnet` na raiz do projeto:

```bash
cat > /opt/mk-bot-ia/.env.testnet << 'EOF'
BINANCE_TESTNET_API_KEY=COLOQUE_AQUI
BINANCE_TESTNET_API_SECRET=COLOQUE_AQUI

# Login obrigatório da UI
MT_UI_LOGIN_REQUIRED=true
MT_UI_LOGIN_USER=admin
MT_UI_LOGIN_PASSWORD=troque-esta-senha-forte
EOF
```

## 5) Teste rápido do ambiente Testnet

```bash
cd /opt/mk-bot-ia
source .venv/bin/activate
export PYTHONPATH=src
python -m microtrans.binance_testnet_cli doctor --env-file .env.testnet
python -m microtrans.binance_testnet_cli account --env-file .env.testnet
```

## 6) Rodar a UI para acesso remoto

```bash
cd /opt/mk-bot-ia
source .venv/bin/activate
export PYTHONPATH=src
set -a; source .env.testnet; set +a
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Abra no celular: `http://SEU_IP:8501`

## 7) Segurança mínima de rede

```bash
ufw allow OpenSSH
ufw allow 8501/tcp
ufw enable
ufw status
```

## 8) Rodar soak longo em tmux

```bash
tmux new -s soak
cd /opt/mk-bot-ia
source .venv/bin/activate
export PYTHONPATH=src
python -m microtrans.binance_testnet_cli executor-loop \
  --symbol BTCUSDT \
  --interval-sec 15 \
  --max-cycles 0 \
  --force-heuristic \
  --jsonl-out auditoria/testnet/soak_cloud_btcusdt.jsonl
```

Comandos úteis:

- Desanexar tmux: `Ctrl+B` e depois `D`
- Voltar ao tmux: `tmux attach -t soak`

### Alternativa recomendada (sem depender de navegador/tmux)

Executar o soak em background com scripts dedicados:

```bash
cd /opt/mk-bot-ia/MK-BOT-IA
chmod +x scripts/soak_*.sh
scripts/soak_start.sh
scripts/soak_status.sh
```

Parar quando necessário:

```bash
cd /opt/mk-bot-ia/MK-BOT-IA
scripts/soak_stop.sh
```

Esse modo mantém o soak rodando no servidor mesmo com navegador fechado.

## 9) Gerar relatório do soak

```bash
cd /opt/mk-bot-ia
source .venv/bin/activate
export PYTHONPATH=src
python -m microtrans.binance_testnet_cli executor-report \
  --input-jsonl auditoria/testnet/soak_cloud_btcusdt.jsonl
```

## 10) Critério para avançar no plano

- `quoted` sem ambiguidade (com e sem execução separados).
- PnL real por fills consistente com o relatório.
- Processo estável durante janela longa (6h+), sem depender de PC local.

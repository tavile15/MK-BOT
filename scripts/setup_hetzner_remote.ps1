param(
    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [string]$LocalProjectPath = "F:\AMBIENTE VIRTUAL\CODIGOS\python\MK-BOT-IA",
    [string]$RemoteBasePath = "/opt/mk-bot-ia",
    [string]$UiUser = "admin",

    [Parameter(Mandatory = $true)]
    [string]$UiPassword,

    [Parameter(Mandatory = $true)]
    [string]$BinanceTestnetApiKey,

    [Parameter(Mandatory = $true)]
    [string]$BinanceTestnetApiSecret
)

$ErrorActionPreference = "Stop"

Write-Host "==> Copiando projeto para o servidor..." -ForegroundColor Cyan
scp -r "$LocalProjectPath" "root@${ServerIp}:$RemoteBasePath"

$remoteCmd = @"
set -e
cd $RemoteBasePath/MK-BOT-IA
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cat > .env.testnet <<'EOF'
BINANCE_TESTNET_API_KEY=$BinanceTestnetApiKey
BINANCE_TESTNET_API_SECRET=$BinanceTestnetApiSecret
MT_UI_LOGIN_REQUIRED=true
MT_UI_LOGIN_USER=$UiUser
MT_UI_LOGIN_PASSWORD=$UiPassword
EOF

export PYTHONPATH=src
python -m microtrans.binance_testnet_cli doctor --env-file .env.testnet
python -m microtrans.binance_testnet_cli account --env-file .env.testnet
echo "SETUP_OK"
"@

Write-Host "==> Executando setup remoto..." -ForegroundColor Cyan
ssh "root@${ServerIp}" $remoteCmd

Write-Host ""
Write-Host "Setup concluido." -ForegroundColor Green
Write-Host "Para iniciar a UI no servidor, execute:" -ForegroundColor Yellow
Write-Host "ssh root@$ServerIp"
Write-Host "cd $RemoteBasePath/MK-BOT-IA"
Write-Host "source .venv/bin/activate"
Write-Host "export PYTHONPATH=src"
Write-Host "set -a; source .env.testnet; set +a"
Write-Host "streamlit run app.py --server.address 0.0.0.0 --server.port 8501"

# One-shot dev setup on Windows. Run from the repo root:  .\scripts\setup.ps1
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env - fill in your keys." }
pytest -q

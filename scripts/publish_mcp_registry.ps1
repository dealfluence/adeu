$ErrorActionPreference = "Stop"

# Ensure we are running from the repository root
if (-not (Test-Path "python\server.json")) {
    Write-Host "[ERROR] Please run this script from the repository root: .\scripts\publish_mcp_registry.ps1" -ForegroundColor Red
    exit 1
}

Write-Host "[INFO] Validating local mcp-publisher installation..." -ForegroundColor Cyan
if (-not (Get-Command "mcp-publisher" -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] mcp-publisher could not be found." -ForegroundColor Red
    Write-Host "Please install it by following the official Windows instructions:"
    Write-Host "https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx#step-3-install-mcp-publisher"
    exit 1
}

Push-Location "python"

try {
    Write-Host "[INFO] Deploying server.json to registry.modelcontextprotocol.io..." -ForegroundColor Cyan
    Write-Host "[NOTE] Because you are using 'ai.adeu/adeu', you must have authenticated via DNS." -ForegroundColor Yellow
    Write-Host "If this fails, run 'mcp-publisher login dns' first." -ForegroundColor DarkGray
    Write-Host ""
    
    # Run the publisher
    mcp-publisher publish
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[ERROR] Publish failed with exit code $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    
    Write-Host "`n[SUCCESS] Successfully deployed!" -ForegroundColor Green
}
finally {
    # Ensure we always return to the root directory even if it crashes
    Pop-Location
}
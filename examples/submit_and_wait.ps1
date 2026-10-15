# Submit a draft over HTTP and wait for the human decision.
# Mirrors what an n8n "HTTP Request" node would do.
#
# Usage:
#   $env:DRAFTGATE_TOKEN = (draftgate token)
#   ./examples/submit_and_wait.ps1

$ErrorActionPreference = "Stop"

$DgUrl = if ($env:DRAFTGATE_URL) { $env:DRAFTGATE_URL } else { "http://127.0.0.1:8010" }
if (-not $env:DRAFTGATE_TOKEN) {
    throw "Set DRAFTGATE_TOKEN first (run: draftgate token)"
}

$headers = @{ Authorization = "Bearer $($env:DRAFTGATE_TOKEN)" }
$body = @{
    title   = "Hello from PowerShell"
    content = "Human, approve me?"
    tags    = @("demo")
} | ConvertTo-Json

$response = Invoke-RestMethod -Method Post -Uri "$DgUrl/api/items" `
    -Headers $headers -ContentType "application/json" -Body $body

Write-Host "submitted item #$($response.id), waiting for review..."

while ($true) {
    Start-Sleep -Seconds 3
    $item = Invoke-RestMethod -Uri "$DgUrl/api/items/$($response.id)" -Headers $headers
    if ($item.status -ne "pending") {
        Write-Host "final status: $($item.status)"
        break
    }
}

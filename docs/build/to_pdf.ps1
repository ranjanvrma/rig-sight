param([string]$In, [string]$Out)
$ext = [System.IO.Path]::GetExtension($In).ToLower()
if ($ext -eq ".pptx") {
    $app = New-Object -ComObject PowerPoint.Application
    $p = $app.Presentations.Open($In, $true, $false, $false)
    $p.SaveAs($Out, 32)
    $p.Close()
    $app.Quit()
} else {
    $app = New-Object -ComObject Word.Application
    $app.Visible = $false
    $d = $app.Documents.Open($In, $false, $true)
    $d.Fields.Update() | Out-Null
    foreach ($t in $d.TablesOfContents) { $t.Update() }
    $d.SaveAs([ref]$Out, [ref]17)
    $d.Close([ref]$false)
    $app.Quit()
}
Write-Output "done"

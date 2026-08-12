# Update every Word field in the generated report and export a PDF.
#
# python-docx can write a TOC field but cannot evaluate it, so a freshly generated document has
# an empty table of contents until something recalculates the fields. This script drives Word to
# do that, saves the result back over the .docx, and exports a PDF for visual inspection.
#
# Usage, from the repository root:
#   powershell -ExecutionPolicy Bypass -File src/finalise_document.ps1
#
# Requires Microsoft Word. If Word is unavailable the .docx is still valid; the reader will be
# prompted to update the field on open, because w:updateFields is set in settings.xml.

param(
    [string]$DocxPath = (Join-Path $PSScriptRoot '..\docs\Sampling_Project_Report.docx'),
    [string]$PdfPath = (Join-Path $PSScriptRoot '..\docs\Sampling_Project_Report.pdf')
)

$ErrorActionPreference = 'Stop'
$DocxPath = (Resolve-Path $DocxPath).Path
$PdfPath = [System.IO.Path]::GetFullPath($PdfPath)

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0

try {
    $document = $word.Documents.Open($DocxPath, $false, $false)

    $document.Fields.Update() | Out-Null
    foreach ($toc in $document.TablesOfContents) { $toc.Update() }
    $document.Fields.Update() | Out-Null
    foreach ($section in $document.Sections) {
        foreach ($footer in $section.Footers) { $footer.Range.Fields.Update() | Out-Null }
    }
    $document.Repaginate()

    Write-Output ("PAGES: " + $document.ComputeStatistics(2))
    if ($document.TablesOfContents.Count -gt 0) {
        $tocText = $document.TablesOfContents.Item(1).Range.Text
        $entries = ($tocText -split "`r" | Where-Object { $_.Trim().Length -gt 0 }).Count
        Write-Output "TOC_ENTRIES: $entries"
    }

    $tableIndex = 0
    foreach ($table in $document.Tables) {
        $tableIndex++
        $rowIndex = 0
        foreach ($row in $table.Rows) {
            $rowIndex++
            $cellText = ''
            foreach ($cell in $row.Cells) { $cellText += $cell.Range.Text -replace "`r|`a|`0", '' }
            if ($cellText.Trim().Length -eq 0) {
                Write-Output "EMPTY_ROW: table $tableIndex row $rowIndex"
            }
        }
    }

    $document.SaveAs([ref]$DocxPath, [ref]16)
    $document.SaveAs([ref]$PdfPath, [ref]17)
    $document.Close($false)
}
finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}

Write-Output "DONE"

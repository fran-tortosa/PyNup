$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourcePath = Join-Path $projectRoot "icono-personalizado\icono.png"
$assetsPath = Join-Path $projectRoot "assets"
$sizes = @(16, 24, 32, 48, 64, 128, 256)

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "No se encuentra la imagen original del icono: $sourcePath"
}

$source = [System.Drawing.Image]::FromFile($sourcePath)
try {
    if ($source.Width -ne $source.Height) {
        throw "El icono debe ser cuadrado; recibido $($source.Width)x$($source.Height)."
    }

    foreach ($size in $sizes) {
        $bitmap = [System.Drawing.Bitmap]::new(
            $size,
            $size,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        try {
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.Clear([System.Drawing.Color]::Transparent)
                $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
                $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
                $graphics.DrawImage(
                    $source,
                    [System.Drawing.Rectangle]::new(0, 0, $size, $size),
                    0,
                    0,
                    $source.Width,
                    $source.Height,
                    [System.Drawing.GraphicsUnit]::Pixel
                )
            }
            finally {
                $graphics.Dispose()
            }
            $bitmap.Save(
                (Join-Path $assetsPath "pynup-$size.png"),
                [System.Drawing.Imaging.ImageFormat]::Png
            )
        }
        finally {
            $bitmap.Dispose()
        }
    }
}
finally {
    $source.Dispose()
}

$images = foreach ($size in $sizes) {
    [pscustomobject]@{
        Size = $size
        Data = [System.IO.File]::ReadAllBytes(
            (Join-Path $assetsPath "pynup-$size.png")
        )
    }
}

$memory = [System.IO.MemoryStream]::new()
try {
    $writer = [System.IO.BinaryWriter]::new($memory)
    $writer.Write([UInt16]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]$images.Count)

    $offset = 6 + 16 * $images.Count
    foreach ($image in $images) {
        $dimension = if ($image.Size -eq 256) { 0 } else { [byte]$image.Size }
        $writer.Write([byte]$dimension)
        $writer.Write([byte]$dimension)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([UInt16]1)
        $writer.Write([UInt16]32)
        $writer.Write([UInt32]$image.Data.Length)
        $writer.Write([UInt32]$offset)
        $offset += $image.Data.Length
    }
    foreach ($image in $images) {
        $writer.Write($image.Data)
    }
    $writer.Flush()
    [System.IO.File]::WriteAllBytes(
        (Join-Path $assetsPath "pynup.ico"),
        $memory.ToArray()
    )
}
finally {
    $memory.Dispose()
}

Write-Host "Iconos generados desde icono-personalizado\icono.png: $($sizes -join ', ') px y pynup.ico."

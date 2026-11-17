# Avisos de terceros / Third-party notices

## PresentMon

iRacing Optimizer incluye el ejecutable de consola de PresentMon (https://github.com/GameTechDev/PresentMon), descargado de su release oficial y verificado por firma digital de Intel durante la compilación.

```
Copyright (C) 2017-2024 Intel Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom
the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.
```

## pyirsdk, psutil, PyYAML, nvidia-ml-py

Dependencias de Python incluidas en el ejecutable, cada una bajo su propia licencia (MIT, BSD-3-Clause, MIT y BSD-3-Clause respectivamente).

## pystray (LGPL-3.0), Pillow (MIT-CMU), six (MIT)

Desde la v0.11, el icono de la bandeja del sistema usa pystray (https://github.com/moses-palmer/pystray), con licencia GNU LGPL v3, y sus dependencias Pillow (https://python-pillow.org, licencia MIT-CMU/HPND) y six (licencia MIT). Se incluyen sin modificar. Como el código de iRacing Optimizer y su script de compilación son públicos, puedes recompilar el .exe con otra versión de pystray (cambia la versión en `requirements.txt` y ejecuta `build.bat`); el texto de la LGPL está en https://www.gnu.org/licenses/lgpl-3.0.html.

Since v0.11 the system tray icon uses pystray (LGPL-3.0) and its dependencies Pillow (MIT-CMU/HPND) and six (MIT), included unmodified. Because the app's source and build script are public, you can rebuild the .exe with a different pystray version (change it in `requirements.txt` and run `build.bat`).

"""Punto de entrada del ejecutable: sin argumentos abre la interfaz; con argumentos, la CLI."""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--tray":  # --tray: inicio con Windows, minimizada
        from iroptimizer.cli import main
    else:
        from iroptimizer.gui import main
    main()

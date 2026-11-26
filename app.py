"""Executable entry point. No arguments: the main window. With arguments: the command line.

--classic opens the older Tk window; --tray (start with Windows, minimised) also uses it.
"""
import sys

if __name__ == "__main__":
    from iroptimizer import selfheal
    selfheal.check()  # restarts cleanly if this copy was started inside an old copy's (deleted) unpack folder
    args = sys.argv[1:]
    if args and args[0] not in ("--tray", "--classic"):
        from iroptimizer.cli import main
        main()
    else:
        opened = False
        if not args:
            from iroptimizer import webapp
            opened = webapp.main()  # False when WebView2 isn't available
        if not opened:
            if "--classic" in sys.argv:
                sys.argv.remove("--classic")
            from iroptimizer.gui import main
            main()

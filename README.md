# iRacing Optimiser

A personal Windows app that **measures** iRacing on this PC and tunes it for smooth frame pacing, image quality and low latency. Nothing is assumed: every setting it recommends has to win an automated A/B test on a repeatable replay.

Built on [empg80/iRacing-Optimizer](https://github.com/empg80/iRacing-Optimizer) (MIT). The original app's features (session recording, diagnostics, profiles, FOV calculator, backups) are still here and are described in [docs/upstream-features.md](docs/upstream-features.md).

## Status

**Milestone 1: prove the measurement can be trusted.** It changes no settings.

The new **Replay bench** tab plays the same replay segment several times and reports:

- **Repeatability:** how much frame time p99 moves between identical runs. If it moves more than 5%, settings can't be compared yet, and finding the noise comes first.
- **Present path:** whether iRacing's frames go straight to the screen (Independent Flip, G-Sync can work) or through the Windows compositor (Composed Flip, extra latency, G-Sync may not engage).
- Per run: average FPS, median and p99 frame time, 1% low, pacing error, stutters, on-screen frame interval, frames never shown, GPU temperature and clock.

Next milestones: automated FPS-cap sweep (iRacing limiter vs NVIDIA limiter), A/B tests for single settings, putting spare GPU time into image quality, and detecting when driver or Windows updates change things.

## Getting the app

You don't need Python. GitHub builds the Windows app on every change:

1. Open the **Actions** tab of this repository.
2. Click the newest green **Build Windows** run.
3. Under **Artifacts**, download **iRacingOptimizer-windows**, unzip it and run `iRacingOptimizer.exe`.

Windows SmartScreen may warn because the exe isn't code-signed: click *More info → Run anyway*.

The first time PresentMon runs it may ask for a Windows permission (adds you to the *Performance Log Users* group). Use **Grant permission…** on the *Session monitor* tab, then sign out and back in once.

## Running the replay bench

1. Start iRacing and open a replay with traffic. A race start is ideal.
2. Pick the car and the cockpit camera, and move the replay to where the segment should start.
3. In the app, open **Replay bench** and click **Start bench**. Leave the PC alone: the default is 1 warm-up run plus 5 measured runs of 60 seconds, about 7 minutes.
4. The report opens in your browser. A `bench_<date>.zip` is saved in `Documents\iRacingOptimizer\bench`; send that file back for analysis.

## Safety

Everything stays outside the game process: the official iRacing telemetry SDK (including its replay controls), PresentMon (Windows event tracing) and NVIDIA's monitoring library. No injection, no overlays, no CPU affinity on the sim. `.ini` files are only written while iRacing is closed, and always backed up first.

## Language

New code and docs are in English. The original code comments are in Spanish; the app's interface follows your Windows language. The original Spanish readme is in [docs/README.es.md](docs/README.es.md).

# iRacing Optimiser

A personal Windows app that **measures** iRacing on this PC, with the goal of tuning it for smooth frame pacing, image quality and low latency.

**Start here: [First run guide](docs/FIRST-RUN.md).**

**Current state, honestly:** the app measures but does not tune yet. The automated A/B test that will prove each setting change is the next milestone. Until then every button that would write iRacing settings is locked (🔒). The original app's suggestions on the Analysis, Profiles, My profiles and FOV tabs are still shown for reference, but they are rules of thumb, not measurements.

Built on [empg80/iRacing-Optimizer](https://github.com/empg80/iRacing-Optimizer) (MIT). The original app's features (session recording, diagnostics, profiles, FOV calculator, backups) are still here and are described in [docs/upstream-features.md](docs/upstream-features.md).

## Status

**Milestone 1: prove the measurement can be trusted.** It changes no settings.

The new **Replay bench** tab plays the same replay segment several times and reports:

- **Repeatability:** how much frame time p99 moves between identical runs. If it moves more than 5%, settings can't be compared yet, and finding the noise comes first.
- **Present path:** whether iRacing's frames go straight to the screen (Independent Flip, G-Sync can work) or through the Windows compositor (Composed Flip, extra latency, G-Sync may not engage).
- Per run: average FPS, median and p99 frame time, 1% low, pacing error, stutters, on-screen frame interval, frames never shown, GPU temperature and clock.

Runs where the frame rate is held at a cap (NVIDIA app, iRacing limiter or V-Sync) are reported as **NOT VALID** rather than passing, because a cap hides the headroom the bench is trying to measure.

Next milestones: interleaved A/B tests for single settings (this is what unlocks the Apply buttons), automated FPS-cap sweep (iRacing limiter vs NVIDIA limiter), putting spare GPU time into image quality, and detecting when driver or Windows updates change things.

## Getting the app

You don't need Python. GitHub builds the Windows app every time `main` changes, and this link always gives the newest build:

**[Download iRacingOptimizer.exe](https://github.com/DeanRussell40/iracing-optimiser/releases/download/latest/iRacingOptimizer.exe)**

Windows SmartScreen may warn because the exe isn't code-signed: click *More info → Run anyway*.

The first time PresentMon runs it may ask for a Windows permission (adds you to the *Performance Log Users* group). Use **Grant permission…** on the *Session monitor* tab, then sign out and back in once.

## Running the replay bench

1. Turn off every frame rate cap (NVIDIA app Max Frame Rate, iRacing FPS limit, V-Sync). Note your usual value so you can put it back afterwards.
2. Start iRacing and open a replay with traffic. A race start is ideal.
3. Pick the car and the cockpit camera, and move the replay to where the segment should start.
4. In the app, open **Replay bench** and click **Start bench**. Leave the PC alone: the default is 1 warm-up run plus 5 measured runs of 60 seconds, about 7 minutes.
5. The report opens in your browser. A `bench_<date>.zip` is saved in `Documents\iRacingOptimizer\bench`; send that file back for analysis.

## Safety

Everything stays outside the game process: the official iRacing telemetry SDK (including its replay controls), PresentMon (Windows event tracing) and NVIDIA's monitoring library. No injection, no overlays, no CPU affinity on the sim. In this version the app does not write iRacing's `.ini` files at all, except to restore a backup. The first time it sees your iRacing folder it saves a copy marked `original`. **Backups → Restore original settings** puts it back in one click.

## Language

New code and docs are in English. The original code comments are in Spanish; the app's interface follows your Windows language. The original Spanish readme is in [docs/README.es.md](docs/README.es.md).

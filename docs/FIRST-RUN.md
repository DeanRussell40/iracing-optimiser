# First run guide

One page. About 20 minutes, most of it the PC running a replay on its own.

## What this version does and doesn't do

- **It measures.** The Replay bench plays the same replay segment several times and checks whether the measurements are steady enough to trust. It also tells you which path your frames take to the screen (the G-Sync check).
- **It does not change your iRacing settings.** All "Apply" buttons are locked (🔒). The suggestions on the Analysis, Profiles, My profiles and FOV tabs come from fixed rules of thumb, not tests on your PC. They unlock one by one once the next version can prove a change helps with an A/B test.
- **It never touches** NVIDIA settings, the Windows registry, the power plan or Windows services.

## What you need

- Windows 10 or 11 with an NVIDIA graphics card and its normal driver.
- iRacing installed, plus one saved replay with traffic. A race start is ideal.
- Nothing else. Python, PresentMon (Intel's frame-timing tool) and everything else are inside the exe.

## 1. Get the app

1. Download the newest build: **[iRacingOptimizer.exe](https://github.com/DeanRussell40/iracing-optimiser/releases/download/latest/iRacingOptimizer.exe)**.
2. Run `iRacingOptimizer.exe`.
3. If Windows SmartScreen warns you, click **More info → Run anyway**. The exe isn't code-signed.
4. It asks whether to notify you of new versions. Either answer is safe: it only checks this repository.

When the app first finds your iRacing folder (`Documents\iRacing`), it saves a copy of your current settings. That copy is marked `original` on the **Backups** tab.

## 2. Allow frame timing (one time)

1. Open the **Session monitor** tab and click **Grant permission…**.
2. Accept the Windows prompt. This adds you to the *Performance Log Users* group, which PresentMon needs.
3. **Sign out of Windows and back in.** It doesn't take effect until you do.

To undo it later, use **Revoke** on the same tab.

## 3. Turn off frame rate caps (for the bench only)

A capped run can't show how much headroom the PC has. The bench marks capped runs **NOT VALID** instead of passing them.

- **NVIDIA app:** Graphics → Program settings → iRacing → **Max Frame Rate: Off**. Note your current value (for example 120) so you can put it back.
- **iRacing:** Options → Graphics → **FPS limit off**.
- **V-Sync:** off in iRacing.

## 4. Run the bench

1. Start iRacing and open your replay. Pick a car with traffic around it and the **cockpit** camera.
2. Move the replay to where the segment should start, and leave it there.
3. Alt-Tab to the app, open **Replay bench** and click **Start bench**. The defaults are 1 warm-up plus 5 runs of 60 s, about 7 minutes.
4. Don't touch the PC until the log says the report is ready. You can click **Stop** at any time.

## 5. Read the result

The report opens in your browser. The headline is one of these:

| Headline | Meaning | What to do |
|---|---|---|
| **PASS** | Runs were steady (worst frames varied by 5% or less). | Send back the zip (below). |
| **FAIL** | Runs varied too much to compare settings. If it says values "move steadily in one direction", the PC was heating up or still loading. | Close background apps (browser, Discord, launchers) and run again. Send the zip either way. |
| **NOT VALID: capped** | Frames were held at a fixed rate. | Recheck step 3, then run again. |
| **Not enough valid runs** | Fewer than 3 runs counted, usually because the replay didn't play at normal speed. | Make sure the replay isn't paused or fast-forwarding and that the app was started *after* opening the replay. |

**Present path** is the G-Sync check:
- *Independent Flip* is good.
- *Composed Flip* means Windows is in the way: about one extra frame of delay, and G-Sync may not engage. That's expected with iRacing's own triple-screen mode across three monitors. It is a real problem if you use NVIDIA Surround.

Results are saved in `Documents\iRacingOptimizer\bench`. Send back the newest `bench_<date>.zip`.

## 6. Put your cap back

Set the NVIDIA app **Max Frame Rate** back to your usual value.

## If something goes wrong

| Message | Fix |
|---|---|
| "PresentMon could not start" / permission | Do step 2 again, and make sure you signed out and back in. |
| "iRacing is not showing a replay" | Open the replay in iRacing first, then click Start bench. |
| "The replay did not move to frame …" | iRacing was loading. Wait until the replay shows, then start again. |
| "PresentMon was not found" | Use the exe from the download link above. A copy built another way may not include PresentMon. |

## Undo everything

- **iRacing settings:** close iRacing, open **Backups**, click **↺ Restore original settings**. Your current files are saved first, so even this can be undone.
- **Frame-timing permission:** **Revoke** on the Session monitor tab, then sign out and back in.
- **Start with Windows:** untick it on the Session monitor tab, if you ticked it.
- **App data:** delete `Documents\iRacingOptimizer`.

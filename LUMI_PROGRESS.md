# Lumi progress

Last updated: 2026-11-13

## Status in one line

The code is written, the workflow is installed, and the first GitHub Actions run built the debug APK successfully. Nothing has been tested on a phone yet.

## Done

| Area | State |
| --- | --- |
| Gradle project, wrapper, two modules | Written |
| `:core` logic: time parsing, tasks, reminders, memory, learning, intent parser, orchestrator, AI clients, notification filtering, briefing | Written; 130 unit tests pass |
| Storage, settings, encrypted API key | Written |
| Alarms, boot restore, safety-net job, geofences | Written |
| Speech recognition and speech output | Written |
| Assistant role services and compact panel | Written |
| Notification listener and accessibility service | Written |
| Android tools (calls, messages, notifications, apps, media, clock, navigation, screen, briefing, places) | Written |
| App screens: Home, Tasks, Memory, Permissions, Places, Settings, Diagnostics | Written |
| CI workflow, failure diagnostics script, signing and release job | Written, as a template in `ci/` |
| Documentation (9 files) | Written |

## How it was verified

| Check | Result |
| --- | --- |
| GitHub Actions run [43466631427](https://github.com/sharveshraam/Lumi/actions/runs/43466631427) on commit `19f2b21` | Succeeded |
| Unit tests (`:core:test`, `:app:testDebugUnitTest`) in that run | Step passed |
| Debug APK build | Step passed; artifact `lumi-debug-apk` (about 10 MB) |
| Android lint | Step ran; findings are non-blocking and have **not been reviewed** (report is in the `lumi-reports` artifact) |
| Signed release build | **Not run** (no signing secrets, no tag) |
| Anything on a phone or emulator | **Not run** |

Before CI was available, `:core` was also compiled and tested locally (130 tests, 0 failures) and `:app` was type-checked against the Android 34 `android.jar`.

## Next steps

1. Download `lumi-debug-apk`, install it, and go through the on-device checklist in TESTING.md. Behaviour that depends on the phone (assistant role, speech, alarms under battery managers, geofences) is unverified until then.
2. Review the lint report.
3. Add signing secrets and tag `v0.1.0` for a signed release (GITHUB_ACTIONS.md).

## Not planned for this version

See KNOWN_LIMITATIONS.md ("Not built").

## "App not installed" investigation (2026-11-13)

- CI now verifies every APK after assembly (see "APK check" in `GITHUB_ACTIONS.md`). Verified on CI for the debug APK: package `com.lumi.assistant.debug`, versionCode 1, minSdk 26, targetSdk 34, ABIs arm64-v8a / armeabi-v7a / x86 / x86_64, single non-split APK, not testOnly, zip-aligned, v2 signature valid, 26 components all with `android:exported`.
- Found and fixed: each CI run signed the debug APK with a different throwaway key (two runs of the same commit gave certificates `d27a6c27…` and `9627aca3…`). Debug builds now use the committed `app/debug.keystore`; certificate `33ea80c9…d622` is pinned in `ci/debug-cert.sha256`.
- Not confirmed: the reason for the failure on the user's phone. The APK itself shows no install blocker, so the phone's `adb install -r` output is needed.
- Still open: `.github/workflows/android_build.yml` on `main` is a second, failing workflow and should be deleted. Release build and on-device behaviour remain untested.

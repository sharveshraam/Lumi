# Lumi progress

Last updated: 2026-11-13

## Status in one line

All planned code and documentation is written and pushed. The core logic is tested (130 passing tests). **The APK has not been built yet**, because the CI workflow file could not be installed by the authoring tool. See "Blocked" below.

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

The development sandbox had no Android SDK, no Gradle distribution and no access to Maven repositories, so Gradle has never been run on this project.

| Check | Result |
| --- | --- |
| `:core` compiled with Kotlin 2.0.20 on JDK 17, tests run with a minimal JUnit-compatible runner | 130 run, 0 failed |
| `:app` sources type-checked with Kotlin 2.0.20 against the real Android 34 `android.jar` and the compiled `:core` | No errors |
| Parts of `:app` that use AndroidX, Compose, WorkManager and Play services | Type-checked against **hand-written stand-ins** for those libraries, not the real ones |
| Manifest and resource XML | Well-formed; not processed by AAPT |
| Gradle build, resource merging, lint, APK packaging | **Not run** |
| Anything on a phone or emulator | **Not run** |

What this means: errors in Lumi's own logic and in its use of the Android framework are unlikely, but the first real Gradle build may still fail on a dependency, resource or Compose API detail. Expect one or more fix rounds once CI runs.

## Blocked

**Installing the workflow.** The authoring tool's GitHub token cannot write to `.github/workflows/` (push rejected; API returned 403). The workflow is at `ci/android-build.yml` and must be copied to `.github/workflows/android-build.yml` by the repository owner. Steps are in GITHUB_ACTIONS.md.

## Next steps

1. Copy the workflow into place (owner).
2. Run the build; fix whatever the first Gradle build reports, using the "CI diagnostics" check.
3. Download `lumi-debug-apk` and go through the on-device checklist in TESTING.md.
4. Add signing secrets and tag `v0.1.0` for a signed release.

## Not planned for this version

See KNOWN_LIMITATIONS.md ("Not built").

# GitHub Actions

## Setup

The workflow is installed at `.github/workflows/android-build.yml`. `ci/android-build.yml` is an identical copy kept for reference; if you change one, change the other.

For the release job to publish a GitHub Release, set **Settings > Actions > General > Workflow permissions** to "Read and write permissions".

## What the workflow does

Triggers: every push, pull requests, tags matching `v*`, and manual runs.

Job **build**:

1. Sets up JDK 17 and the Android SDK (platform 34, build-tools 34.0.0).
2. Runs `:core:test` and `:app:testDebugUnitTest`.
3. Builds the debug APK and uploads it as the artifact **`lumi-debug-apk`** (kept 30 days).
4. Runs Android lint. Findings are reported but do not fail the build.
5. Uploads test and lint reports as **`lumi-reports`**.
6. On failure, publishes a check run named **CI diagnostics** on the commit with the condensed compiler, test and lint errors.

Job **release** runs for `v*` tags and for manual runs with "build_release" ticked. It builds a signed release APK if the signing secrets exist, and for tags publishes a GitHub Release with the APKs attached.

## Downloading the APK

Actions tab > **Android Build** > the latest successful run > **Artifacts** > `lumi-debug-apk`. Unzip it and install the APK. The debug build installs as `com.lumi.assistant.debug`, alongside any release build.

## Signing a release

Create a keystore once and keep it safe; updates must be signed with the same key.

```
keytool -genkeypair -v -keystore lumi-release.jks -alias lumi -keyalg RSA -keysize 2048 -validity 10000
base64 -w0 lumi-release.jks > lumi-release.jks.b64
```

Add these under **Settings > Secrets and variables > Actions**:

| Secret | Value |
| --- | --- |
| `SIGNING_KEYSTORE_BASE64` | Contents of `lumi-release.jks.b64` |
| `SIGNING_KEYSTORE_PASSWORD` | Keystore password |
| `SIGNING_KEY_ALIAS` | `lumi` |
| `SIGNING_KEY_PASSWORD` | Key password |

No keystore or password is stored in the repository. Without the secrets the release job skips the signed APK and prints a warning.

To publish a release:

```
git tag v0.1.0
git push origin v0.1.0
```

The version name comes from the tag and the version code from the run number.

## Reading a failed build

Open the failed run and the failing step, or read the **CI diagnostics** check on the commit:

```
gh api repos/sharveshraam/Lumi/commits/<sha>/check-runs --jq '.check_runs[] | select(.name=="CI diagnostics") | .output.text'
```

## Pinned versions

Gradle 8.9, Android Gradle Plugin 8.5.2, Kotlin 2.0.20, JDK 17, compileSdk and targetSdk 34, minSdk 26, Compose BOM 2024.09.00.

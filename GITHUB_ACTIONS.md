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

## APK check

After `assembleDebug` or `assembleRelease`, Gradle runs `.github/scripts/verify-apk.sh` when `CI=true` (task `:app:verifyDebugApk` / `:app:verifyReleaseApk`). It reports and validates:

| Check | Expected |
| --- | --- |
| Exactly one `.apk` in the output folder, not a split | yes |
| Package | `com.lumi.assistant.debug` (debug), `com.lumi.assistant` (release) |
| minSdk / targetSdk | 26 / 34, no maxSdk |
| Signature verifies for API 26+ | yes, one signer |
| Debug signing certificate SHA-256 | the value in `ci/debug-cert.sha256` |
| `testOnly`, `sharedUserId` | absent |
| Components with an intent filter declare `android:exported` | all |
| Zip alignment | aligned |

A failed check fails the build, so a bad APK is never uploaded. The result appears in three places: the run's **Summary** page (full table), an **APK metadata** annotation on the job, and `build-logs/40-apk-metadata.md` in the `lumi-reports` artifact. From the command line:

```
gh api repos/sharveshraam/Lumi/actions/runs/<run-id>/jobs --jq '.jobs[0].check_run_url' \
  | xargs -I{} gh api {}/annotations --jq '.[] | select(.title=="APK metadata") | .message'
```

To compare with the file you downloaded: unzip the artifact and run `sha256sum app-debug.apk`; it must equal `apkSha256` in the annotation.

### Debug signing key

Debug builds are signed with `app/debug.keystore` (password `android`, alias `androiddebugkey`), which is committed on purpose. It gives every debug build the same certificate, so a newer debug build installs over an older one. It is not a secret and protects nothing; release builds use the key from the signing secrets.

Debug builds made **before** this key was added were each signed with a different throwaway key. Uninstall any of those once before installing a newer build.

## Downloading the APK

Actions tab > **Android Build** > the latest successful run > **Artifacts** > `lumi-debug-apk`. The download is a zip: unzip it and install the `app-debug.apk` inside (the zip itself cannot be installed). The debug build installs as `com.lumi.assistant.debug`, alongside any release build.

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

## If the phone says "App not installed"

That message hides the real reason. Get it with a computer and a USB cable:

1. On the phone: Settings > About phone > tap "Build number" seven times, then Settings > Developer options > enable "USB debugging". On Xiaomi, Redmi and Poco also enable "Install via USB".
2. Install [platform-tools](https://developer.android.com/tools/releases/platform-tools), connect the phone, accept the prompt on it.
3. Run:

```
adb devices
adb shell getprop ro.build.version.sdk
adb shell getprop ro.product.cpu.abilist
adb shell pm list packages | grep -i lumi
adb install -r app-debug.apk
```

The last command prints `Success` or a line such as `Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: ...]`.

| Reason | Meaning | What to do |
| --- | --- | --- |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | A Lumi build signed with another key is installed | `adb uninstall com.lumi.assistant.debug`, then install again |
| `INSTALL_FAILED_OLDER_SDK` | Phone is older than Android 8.0 (API 26) | Not supported |
| `INSTALL_FAILED_INSUFFICIENT_STORAGE` | Not enough free space | Free space |
| `INSTALL_FAILED_USER_RESTRICTED` | The phone blocked installation over USB or from this source | Enable "Install via USB" / allow the source |
| `INSTALL_FAILED_VERIFICATION_FAILURE`, `INSTALL_FAILED_ABORTED` | Play Protect or the phone's security app blocked it | Choose "Install anyway", or disable the scan for this install |
| `INSTALL_PARSE_FAILED_*`, `INSTALL_FAILED_INVALID_APK` | The file is damaged or is not the APK (for example the zip) | Download again, unzip, compare `sha256sum` with the CI value |
| `INSTALL_FAILED_DUPLICATE_PERMISSION`, `INSTALL_FAILED_CONFLICTING_PROVIDER` | Another installed package claims the same name | Report the full line |

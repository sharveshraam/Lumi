#!/usr/bin/env bash
# Reports the metadata of a built APK and fails if it is not what an installable Lumi build
# must be. Usage:
#   verify-apk.sh verify <apk-directory> <expected-package> <expected-minSdk> <expected-targetSdk> [expected-cert-sha256]
#   verify-apk.sh publish <check-run-name>      (posts the report as a check run on the commit)
# The report is written to build-logs/40-apk-metadata.md and the job summary.
set -uo pipefail

mode="${1:-}"
report=build-logs/40-apk-metadata.md
mkdir -p build-logs

if [[ "$mode" == "publish" ]]; then
  name="${2:-APK metadata}"
  [[ -f "$report" ]] || echo "No APK report was produced (the build probably failed before assembly)." > "$report"
  if [[ -n "${ARTIFACT_ID:-}" ]]; then
    {
      echo
      echo "### Uploaded artifact"
      echo "- artifact id: \`$ARTIFACT_ID\`"
      echo "- artifact zip sha256: \`${ARTIFACT_DIGEST:-unknown}\`"
      echo "- The artifact is a zip that contains the APK above. Unzip it, then install the \`.apk\` inside."
    } >> "$report"
  fi
  conclusion=success
  grep -q '^RESULT: FAIL' "$report" && conclusion=failure
  jq -n --arg sha "${HEAD_SHA:-$GITHUB_SHA}" --arg name "$name" --arg c "$conclusion" --rawfile text "$report" \
    '{name: $name, head_sha: $sha, status: "completed", conclusion: $c,
      output: {title: "APK metadata and install checks", summary: ("Run " + env.GITHUB_RUN_ID + ", event " + env.GITHUB_EVENT_NAME), text: $text}}' \
    | gh api "repos/$GITHUB_REPOSITORY/check-runs" --input - > /dev/null \
    && echo "Published check run '$name'." || echo "Could not publish the check run."
  exit 0
fi

if [[ "$mode" != "verify" || $# -lt 5 ]]; then
  echo "usage: $0 verify <apk-dir> <package> <minSdk> <targetSdk> [cert-sha256] | publish <name>" >&2
  exit 2
fi

dir="$2"; want_pkg="$3"; want_min="$4"; want_target="$5"; want_cert="${6:-}"
want_cert="$(echo "$want_cert" | tr -d ': ' | tr 'A-F' 'a-f')"

sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
bt="$(ls -d "$sdk"/build-tools/* 2>/dev/null | sort -V | tail -n 1)"
aapt2="$bt/aapt2"; apksigner="$bt/apksigner"; zipalign="$bt/zipalign"

fail=0
problems=()
bad() { problems+=("$1"); fail=1; }

: > "$report"
out() { echo "$@" >> "$report"; }

out "## APK check: \`$dir\`"
out
out "- commit: \`${GITHUB_SHA:-local}\`  run: \`${GITHUB_RUN_ID:-local}\`  event: \`${GITHUB_EVENT_NAME:-local}\`"
out "- build-tools: \`$(basename "${bt:-missing}")\`"
out
out "### Files in the output directory"
out '```'
ls -l "$dir" >> "$report" 2>&1
out '```'

shopt -s nullglob
apks=("$dir"/*.apk)
shopt -u nullglob
if [[ ${#apks[@]} -ne 1 ]]; then
  bad "expected exactly one .apk in $dir, found ${#apks[@]} (a split or multi-APK output is not directly installable)"
fi
if [[ ${#apks[@]} -eq 0 || ! -x "$aapt2" || ! -x "$apksigner" ]]; then
  [[ -x "$aapt2" && -x "$apksigner" ]] || bad "aapt2/apksigner not found under '$sdk/build-tools'"
  out
  out "RESULT: FAIL"
  for p in "${problems[@]}"; do out "- $p"; done
  cat "$report"; [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && cat "$report" >> "$GITHUB_STEP_SUMMARY"
  exit 1
fi
apk="${apks[0]}"

badging="$("$aapt2" dump badging "$apk" 2>&1)"
pkg_line="$(grep -m1 '^package:' <<<"$badging")"
field() { sed -n "s/.*[ :]$1='\([^']*\)'.*/\1/p" <<<"$2" | head -n 1; }
pkg="$(field name "$pkg_line")"
vcode="$(field versionCode "$pkg_line")"
vname="$(field versionName "$pkg_line")"
split="$(field split "$pkg_line")"
min="$(sed -n "s/^\(sdkVersion\|minSdkVersion\):'\([0-9]*\)'.*/\2/p" <<<"$badging" | head -n 1)"
target="$(sed -n "s/^targetSdkVersion:'\([0-9]*\)'.*/\1/p" <<<"$badging" | head -n 1)"
maxsdk="$(sed -n "s/^maxSdkVersion:'\([0-9]*\)'.*/\1/p" <<<"$badging" | head -n 1)"
native="$(grep -m1 '^native-code:' <<<"$badging" | sed 's/^native-code: *//')"
libs="$(unzip -Z1 "$apk" 2>/dev/null | grep -E '^lib/[^/]+/' | cut -d/ -f2 | sort -u | tr '\n' ' ')"
dex="$(unzip -Z1 "$apk" 2>/dev/null | grep -cE '^classes[0-9]*\.dex$')"
required_features="$(grep "^uses-feature: name=" <<<"$badging" | sed "s/^uses-feature: name='\([^']*\)'.*/\1/" | tr '\n' ' ')"
libraries="$(grep "^uses-library:" <<<"$badging" | tr '\n' ' ')"

manifest="$("$aapt2" dump xmltree --file AndroidManifest.xml "$apk" 2>&1)"
attr_true() { grep -E "android:$1\(0x[0-9a-f]+\)=(true|\(type 0x12\)0xffffffff)" <<<"$manifest" > /dev/null; }
test_only=no; attr_true testOnly && test_only=yes
debuggable=no; attr_true debuggable && debuggable=yes
split_required=no; attr_true isSplitRequired && split_required=yes
shared_uid="$(grep -m1 'android:sharedUserId' <<<"$manifest" | sed 's/.*="\([^"]*\)".*/\1/')"

sig="$("$apksigner" verify --verbose --print-certs --min-sdk-version "$want_min" "$apk" 2>&1)"; sig_rc=$?
cert="$(sed -n 's/^Signer #1 certificate SHA-256 digest: *//p' <<<"$sig" | head -n 1)"
cert_dn="$(sed -n 's/^Signer #1 certificate DN: *//p' <<<"$sig" | head -n 1)"
schemes="$(grep -E '^Verified using v[0-9.]+ scheme' <<<"$sig" | sed 's/^Verified using //; s/ scheme ([^)]*)//' | tr '\n' ';')"
signers="$(grep -c 'certificate SHA-256 digest' <<<"$sig")"
aligned=unknown
if [[ -x "$zipalign" ]]; then "$zipalign" -c 4 "$apk" > /dev/null 2>&1 && aligned=yes || aligned=no; fi

out
out "### Metadata of \`$(basename "$apk")\`"
out
out "| Field | Value |"
out "| --- | --- |"
out "| size (bytes) | $(stat -c %s "$apk") |"
out "| file sha256 | \`$(sha256sum "$apk" | cut -d' ' -f1)\` |"
out "| package (application ID) | \`$pkg\` |"
out "| versionCode / versionName | $vcode / $vname |"
out "| minSdk / targetSdk / maxSdk | $min / $target / ${maxsdk:-none} |"
out "| native ABIs (lib/ folders) | ${libs:-none: pure Java/Kotlin, installs on every ABI} |"
out "| native-code (aapt) | ${native:-none} |"
out "| dex files | $dex |"
out "| split name / isSplitRequired | ${split:-none (base APK)} / $split_required |"
out "| testOnly | $test_only |"
out "| debuggable | $debuggable |"
out "| sharedUserId | ${shared_uid:-none} |"
out "| uses-library | ${libraries:-none} |"
out "| required features | ${required_features:-none} |"
out "| zip aligned | $aligned |"
out "| signature verifies (minSdk $want_min) | $([[ $sig_rc -eq 0 ]] && echo yes || echo NO) |"
out "| signature schemes | $schemes |"
out "| signers | $signers |"
out "| certificate DN | $cert_dn |"
out "| certificate SHA-256 | \`$cert\` |"
out "| expected certificate SHA-256 | \`${want_cert:-not pinned}\` |"

[[ "$pkg" == "$want_pkg" ]] || bad "package is '$pkg', expected '$want_pkg'"
[[ "$min" == "$want_min" ]] || bad "minSdk is '$min', expected '$want_min'"
[[ "$target" == "$want_target" ]] || bad "targetSdk is '$target', expected '$want_target'"
[[ -z "$maxsdk" ]] || bad "maxSdkVersion=$maxsdk would block installation on newer Android versions"
[[ $sig_rc -eq 0 ]] || bad "apksigner could not verify the signature for minSdk $want_min"
[[ -n "$cert" ]] || bad "no signing certificate found"
[[ "$signers" -le 1 ]] || bad "APK has $signers signers; expected one"
[[ "$test_only" == "no" ]] || bad "android:testOnly=true: such an APK only installs with 'adb install -t'"
[[ "$split_required" == "no" && -z "$split" ]] || bad "APK is a split or requires splits; it cannot be installed on its own"
[[ -z "$shared_uid" ]] || bad "sharedUserId='$shared_uid' can conflict with other installed packages"
[[ "$dex" -ge 1 ]] || bad "APK contains no classes.dex"
[[ "$aligned" != "no" ]] || bad "APK is not zip-aligned"
[[ -z "$vcode" || "$vcode" -ge 1 ]] || bad "versionCode '$vcode' is not a positive integer"
if [[ -n "$want_cert" && "$cert" != "$want_cert" ]]; then
  bad "signing certificate differs from the pinned one; an update over an existing install would be rejected (INSTALL_FAILED_UPDATE_INCOMPATIBLE)"
fi

out
if [[ $fail -eq 0 ]]; then
  out "RESULT: PASS"
else
  out "RESULT: FAIL"
  for p in "${problems[@]}"; do out "- $p"; done
  out
  out '<details><summary>apksigner output</summary>'
  out
  out '```'
  head -n 40 <<<"$sig" >> "$report"
  out '```'
  out '</details>'
fi

cat "$report"
[[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && cat "$report" >> "$GITHUB_STEP_SUMMARY"
exit $fail

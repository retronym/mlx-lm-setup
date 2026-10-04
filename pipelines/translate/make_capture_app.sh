#!/bin/zsh
# Builds ~/Applications/TranslateCapture.app: a background applet that runs `screencapture -i` and quits.
#
# Why: a Quick Action's shell script is a child of whatever app is in front, so macOS asks *that* app for Screen Recording
# permission. Launched through LaunchServices (`open -W -a`), the applet is its own responsible process, so one grant
# (System Settings → Privacy & Security → Screen & System Audio Recording → TranslateCapture) covers every app.
# translate.py uses it when present. Rebuilding changes the ad-hoc signature, so macOS asks again.
set -euo pipefail

APP="$HOME/Applications/TranslateCapture.app"
mkdir -p "$HOME/Applications"
rm -rf "$APP"

/usr/bin/osacompile -o "$APP" <<'APPLESCRIPT'
on run
	set dir to POSIX path of (path to home folder) & "Library/Caches/mlx-translate/"
	do shell script "mkdir -p " & quoted form of dir & "; /usr/sbin/screencapture -i -x " & quoted form of (dir & "shot.png") & " 2> " & quoted form of (dir & "shot.err") & " || true"
end run
APPLESCRIPT

PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string local.mlx-lm-setup.translate-capture" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"         # no Dock icon, no menu bar
/usr/bin/codesign --force --deep --sign - "$APP"
echo "$APP"

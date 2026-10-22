#!/bin/sh
# Idempotent install; --upgrade explicitly reloads stale code/configuration.
set -eu
test "$(id -u)" != 0 || { echo 'Run as your normal user, not root.'; exit 1; }
task_gui=0
task_upgrade=0
for task_arg in "$@"; do
    case "$task_arg" in --gui) task_gui=1;; --upgrade) task_upgrade=1;; *) exit 64;; esac
done
task_configure="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/configure-background-mac.sh"
task_user=$(id -un)
task_uid=$(id -u)
task_home=$(dscl . -read "/Users/$task_user" NFSHomeDirectory | cut -d ' ' -f2-)
task_app='/Applications/LinkRadar.app/Contents/MacOS/LinkRadar'
task_data="$task_home/Library/Application Support/VCS Network Watch"
task_label="tech.vcs.network-watch.engine.$task_uid"
task_plist="/Library/LaunchDaemons/$task_label.plist"
test -x "$task_app" || { echo 'Copy LinkRadar.app to Applications first.'; exit 1; }
if test "$task_upgrade" = 0 && /bin/launchctl print "system/$task_label" >/dev/null 2>&1; then
    echo 'Background engine already registered; kept running.'
    exit 0
fi
mkdir -p "$task_data"
# Freeze the privileged program before authorization. No elevated file reads
# from the app bundle or user-created plist follow the system password dialog.
task_payload=$(/bin/cat "$task_configure")
if test "$task_gui" = 1; then
    /usr/bin/osascript - "$task_payload" "$task_uid" "$task_upgrade" <<'APPLESCRIPT'
on run argv
    set command to "/bin/sh -c " & quoted form of item 1 of argv & " sh " & quoted form of item 2 of argv & " " & quoted form of item 3 of argv
    do shell script command with administrator privileges
end run
APPLESCRIPT
else
    /usr/bin/sudo /bin/sh -c "$task_payload" sh "$task_uid" "$task_upgrade"
fi
echo 'Background engine installed/updated. Open LinkRadar; active monitoring will show ФОН.'

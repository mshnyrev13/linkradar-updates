#!/bin/sh
# Root creates only a fixed, user-identity launchd configuration. Never app code as root.
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077
test "$(/usr/bin/id -u)" = 0 || exit 77
test "$#" = 2 || exit 64
task_uid=$1
task_upgrade=$2
case "$task_uid" in ''|*[!0-9]*) exit 64;; esac
test "$task_uid" -ge 501 || exit 64
case "$task_upgrade" in 0|1) ;; *) exit 64;; esac
task_user=$(/usr/bin/id -un "$task_uid")
task_home=$(/usr/bin/dscl . -read "/Users/$task_user" NFSHomeDirectory | /usr/bin/cut -d ' ' -f2-)
case "$task_home" in /*) ;; *) exit 64;; esac
task_label="tech.vcs.network-watch.engine.$task_uid"
task_job="system/$task_label"
task_directory='/Library/LaunchDaemons'
task_plist="$task_directory/$task_label.plist"
# The parent is a protected system directory. Refuse redirection and hard links.
for task_path in "$task_directory" "$task_plist"; do
    test ! -L "$task_path" || exit 77
    if test -e "$task_path"; then
        test "$(/usr/bin/stat -f %u "$task_path")" = 0 || exit 77
        task_mode=$(/usr/bin/stat -f %Lp "$task_path")
        test "$((0$task_mode & 022))" = 0 || exit 77
        # POSIX mode bits do not describe delegated macOS ACL write access.
        # These fixed system destinations normally have no extended ACL.
        test "$(/bin/ls -lde "$task_path" | /usr/bin/wc -l)" -eq 1 || exit 77
    fi
done
if test -e "$task_plist"; then
    test -f "$task_plist" || exit 77
    test "$(/usr/bin/stat -f %l "$task_plist")" = 1 || exit 77
    test "$(/usr/bin/plutil -extract UserName raw "$task_plist")" = "$task_user" || exit 77
fi
if test "$task_upgrade" = 0 && /bin/launchctl print "$task_job" >/dev/null 2>&1; then exit 0; fi
# Root-owned staging directory: no caller-writable plist is read after elevation.
task_stage=$(/usr/bin/mktemp -d /private/tmp/linkradar-launchd.XXXXXX)
trap '/bin/rm -rf "$task_stage"' EXIT HUP INT TERM
task_source="$task_stage/new.plist"
task_backup="$task_stage/previous.plist"
cat > "$task_source" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict/></plist>
PLIST
/usr/bin/plutil -insert Label -string "$task_label" "$task_source"
/usr/bin/plutil -insert UserName -string "$task_user" "$task_source"
/usr/bin/plutil -insert ProgramArguments -array "$task_source"
/usr/bin/plutil -insert ProgramArguments.0 -string '/Applications/LinkRadar.app/Contents/MacOS/LinkRadar' "$task_source"
/usr/bin/plutil -insert ProgramArguments.1 -string '--engine-service' "$task_source"
/usr/bin/plutil -insert ProgramArguments.2 -string '--data-dir' "$task_source"
/usr/bin/plutil -insert ProgramArguments.3 -string "$task_home/Library/Application Support/VCS Network Watch" "$task_source"
/usr/bin/plutil -insert RunAtLoad -bool true "$task_source"
/usr/bin/plutil -insert KeepAlive -bool true "$task_source"
# launchd can open stdio before dropping privilege: never open user-owned paths.
/usr/bin/plutil -insert StandardOutPath -string /dev/null "$task_source"
/usr/bin/plutil -insert StandardErrorPath -string /dev/null "$task_source"
/usr/bin/plutil -lint "$task_source" >/dev/null
task_had_plist=0
if test -f "$task_plist"; then
    /bin/cp -p "$task_plist" "$task_backup"
    # A rollback must not restore privileged opens of user-controlled log paths.
    /usr/bin/plutil -replace StandardOutPath -string /dev/null "$task_backup"
    /usr/bin/plutil -replace StandardErrorPath -string /dev/null "$task_backup"
    task_had_plist=1
fi
if /bin/launchctl print "$task_job" >/dev/null 2>&1; then /bin/launchctl bootout "$task_job"; fi
if /bin/launchctl print "$task_job" >/dev/null 2>&1; then exit 1; fi
if /usr/bin/install -o root -g wheel -m 644 "$task_source" "$task_plist" &&
   /bin/launchctl bootstrap system "$task_plist"; then exit 0; fi
if test "$task_had_plist" = 1; then
    /usr/bin/install -o root -g wheel -m 644 "$task_backup" "$task_plist"
    /bin/launchctl bootstrap system "$task_plist" || true
else
    /bin/rm -f "$task_plist"
fi
exit 1

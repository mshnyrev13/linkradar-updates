#!/bin/sh
set -eu
task_label="tech.vcs.network-watch.engine.$(id -u)"
sudo -v
sudo launchctl bootout "system/$task_label" 2>/dev/null || true
sudo rm -f "/Library/LaunchDaemons/$task_label.plist"
echo 'Background engine removed; session measurements remain on disk.'

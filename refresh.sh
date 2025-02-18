#!/bin/bash

# This script runs the content_resolvere.py with the right configs and pushes out the results

### NOTE: Before running, create the dockerfile with
# podman build -t localhost/asamalik/fedora-env .



WORK_DIR=$(mktemp -d -t content-resolver-XXXXXXXXXX)

if [[ ! "$WORK_DIR" || ! -d "$WORK_DIR" ]]; then
  echo "Could not create temp dir"
  exit 1
fi

function cleanup {      
  rm -rf "$WORK_DIR"
  echo "Deleted temp working directory $WORK_DIR"
}

trap cleanup EXIT

cd $WORK_DIR

# Get the latest code repo and configs
git clone -b buildroot-extras git@github.com:tdawson/content-resolver.git || exit 1
cd content-resolver || exit 1
git clone -b no-c10s git@github.com:tdawson/content-resolver-input.git || exit 1

# Local output dir. Includes a dir for the history data, too.
mkdir -p $WORK_DIR/content-resolver/out/history || exit 1

# make sure we have a log dir
mkdir -p ~/logs/ || exit 1

# Build the site
build_started=$(date +"%Y-%m-%d-%H%M")
echo ""
echo "Building..."
echo "$build_started"
echo "(Logging into ~/logs/$build_started.log)"
CMD="./content_resolver.py --dnf-cache-dir /dnf_cachedir content-resolver-input/configs out" || exit 1
podman run --rm -it --tmpfs /dnf_cachedir -v $WORK_DIR/content-resolver:/workspace:z localhost/asamalik/fedora-env $CMD > ~/logs/$build_started.log || exit 1

# Save the root log cache
cp $WORK_DIR/content-resolver/cache_root_log_deps.json $WORK_DIR/content-resolver/out/cache_root_log_deps.json || exit 1


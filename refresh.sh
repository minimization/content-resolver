#!/bin/bash

# This script runs the feedback_pipeline.py with the right configs and pushes out the results



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
git clone https://github.com/minimization/content-resolver || exit 1
cd content-resolver || exit 1
git clone https://github.com/minimization/content-resolver-input || exit 1
git clone https://github.com/minimization/content-resolver-input-additional || exit 1
cp -f content-resolver-input-additional/configs/* content-resolver-input/configs/

# Local output dir. Includes a dir for the history data, too.
mkdir -p $WORK_DIR/content-resolver/out/history || exit 1

# Get a copy of the historic data
aws s3 sync s3://tiny.distro.builders/history $WORK_DIR/content-resolver/out/history --exclude "*" --include="historic_data*" || exit 1

# Get the root log cache
# (there's no exit one because that file might not exist)
aws s3 cp s3://tiny.distro.builders/cache_root_log_deps.json $WORK_DIR/content-resolver/cache_root_log_deps.json

# Build the site
build_started=$(date +"%Y-%m-%d-%H%M")
echo ""
echo "Building..."
echo "$build_started"
echo "(Logging into ~/logs/$build_started.log)"
CMD="./feedback_pipeline.py --dnf-cache-dir /dnf_cachedir content-resolver-input/configs out" || exit 1
podman run --rm -it --tmpfs /dnf_cachedir -v $WORK_DIR/content-resolver:/workspace:z localhost/asamalik/fedora-env $CMD > ~/logs/$build_started.log || exit 1

# Save the root log cache
cp $WORK_DIR/content-resolver/cache_root_log_deps.json $WORK_DIR/content-resolver/out/cache_root_log_deps.json || exit 1

# Publish the site
aws s3 sync --delete $WORK_DIR/content-resolver/out s3://tiny.distro.builders || exit 1

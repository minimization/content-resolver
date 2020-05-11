#!/bin/bash

# This script runs the feedback_pipeline.py with the right configs and pushes out the results

WORK_DIR=$(mktemp -d -t feedback-pipeline-XXXXXXXXXX)

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
git clone git@github.com:minimization/feedback-pipeline.git || exit 1
cd feedback-pipeline || exit 1
git clone git@github.com:minimization/feedback-pipeline-config.git configs || exit 1

# Local output dir. Includes a dir for the history data, too.
mkdir -p $WORK_DIR/feedback-pipeline/out/history || exit 1

# Get a copy of the historic data
aws s3 sync s3://tiny.distro.builders/history WORK_DIR/feedback-pipeline/out/history || exit 1

# Build the site
CMD="./feedback_pipeline.py configs out" || exit 1
podman run --rm -it -v $WORK_DIR/feedback-pipeline:/workspace:z asamalik/feedback-pipeline-env $CMD || exit 1

# Publish the site
aws s3 sync $WORK_DIR/feedback-pipeline/out s3://tiny.distro.builders || exit 1
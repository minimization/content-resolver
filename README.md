# feedback-pipeline

Feedback Pipeline provides reporting and notifications regarding dependencies and sizes of defined RPM installations.

## Developer preview

If you want to contribute to this project, you can run the `devel_feedback_pipeline.py` script that will automatically load pregenerated RPM data so you can see the output much faster. This script also skips generating the graphs which would also take a long time.

To run the script, you'll need Python 3 and the following dependencies:

* `yaml`
* `jinja2`

... or you can leverage the `Dockerfile` included in this repository that has all the dependencies pre-installed. You can get it pre-built from Dockerhub as `asamalik/feedback-pipeline-env`.

Option 1: on Fedora natively:

```
$ sudo dnf install python3-yaml python3-jinja2
$ mkdir output
$ ./devel_feedback_pipeline.py output
```

Option 2: on Fedora in a container

```
$ podman pull asamalik/feedback-pipeline-env
$ podman run --rm -it -v $(pwd):/workspace:z asamalik/feedback-pipeline-env bash
$ mkdir output
$ ./devel_feedback_pipeline.py output
```

Option 3: on a Mac using Docker:

```
$ docker pull asamalik/feedback-pipeline-env
$ docker run --rm -it -v $(pwd):/workspace asamalik/feedback-pipeline-env bash
$ mkdir output
$ ./devel_feedback_pipeline.py output
```

In both cases, the output would be in the `output` directory. Open the `output/index.html` in your web browser of choice to see the result.

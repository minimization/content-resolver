# Feedback Pipeline

Reporting and notifications regarding dependencies and sizes of Fedora-based workloads.

[See it live! (https://tiny.distro.builders)](https://tiny.distro.builders)

## Developer preview

If you want to contribute and test your changes, run the `feedback_pipeline.py` script with test configs in the `test_configs` directory.

To run the script, you'll need Python 3 and the following dependencies:

* `yaml`
* `jinja2`

Option 1: on Fedora natively:

```
$ sudo dnf install python3-yaml python3-jinja2
$ mkdir output
$ ./feedback_pipeline.py test_configs output
```

... or you can leverage the `Dockerfile` included in this repository that has all the dependencies pre-installed. You can get it pre-built from Dockerhub as `asamalik/feedback-pipeline-env`.

Option 2: on Fedora in a container

```
$ podman pull asamalik/feedback-pipeline-env
$ podman run --rm -it -v $(pwd):/workspace:z asamalik/feedback-pipeline-env bash
$ mkdir output
$ ./feedback_pipeline.py test_configs output
```

Option 3: on a Mac using Docker:

```
$ docker pull asamalik/feedback-pipeline-env
$ docker run --rm -it -v $(pwd):/workspace asamalik/feedback-pipeline-env bash
$ mkdir output
$ ./feedback_pipeline.py test_configs output
```

In both cases, the output will be generated in the `output` directory. Open the `output/index.html` in your web browser of choice to see the result.

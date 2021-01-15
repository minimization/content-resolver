from registry.fedoraproject.org/fedora:33

run dnf -y update fedora-gpg-keys && \
    dnf -y install python3-jinja2 python3-yaml git && \
    dnf clean all 

run mkdir /app /configs /output /dnf_cachedir && \
    git clone https://github.com/minimization/content-resolver /app

cmd cd /app && \
    ./feedback_pipeline.py --dnf-cache-dir /dnf_cachedir /configs /output

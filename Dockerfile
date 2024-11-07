FROM registry.fedoraproject.org/fedora:41

RUN dnf -y update fedora-gpg-keys && \
    dnf -y install git python3-jinja2 python3-koji python3-yaml python3-dnf && \
    dnf clean all

WORKDIR /workspace


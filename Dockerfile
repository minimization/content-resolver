from fedora:30

run dnf -y update fedora-gpg-keys && \
    dnf -y install graphviz podman python3-jinja2 python3-yaml && \
    dnf clean all

workdir /workspace

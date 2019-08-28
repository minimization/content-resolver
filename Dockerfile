from fedora:30

run dnf -y install graphviz podman python3-jinja2 && \
    dnf clean all

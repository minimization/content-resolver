FROM registry.fedoraproject.org/fedora:44

RUN dnf -y update fedora-gpg-keys \
      --setopt=tsflags=nodocs \
      --setopt=install_weak_deps=False && \
    dnf -y install \
        git \
        python3-jinja2 \
        python3-koji \
        python3-libdnf5 \
        python3-yaml \
        --setopt=tsflags=nodocs \
        --setopt=install_weak_deps=False && \
    dnf clean all && \
    rm -rf /var/cache/dnf

WORKDIR /workspace

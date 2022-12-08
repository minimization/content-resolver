from registry.fedoraproject.org/fedora:37

run dnf -y update fedora-gpg-keys && \
    dnf -y install git python3-jinja2 python3-koji python3-yaml && \
    dnf clean all 
    
workdir /workspace


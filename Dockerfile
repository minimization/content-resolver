from registry.fedoraproject.org/fedora:33

run dnf -y update fedora-gpg-keys && \
    dnf -y install git python3-pytest python3-pytest-cov python3-jsondiff python3-jinja2 python3-koji python3-yaml && \
    dnf clean all 
    
workdir /workspace


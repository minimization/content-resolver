from registry.fedoraproject.org/fedora:33

run dnf -y update fedora-gpg-keys && \
    dnf -y install python3-jinja2 python3-yaml git && \
    dnf clean all 
    
workdir /workspace


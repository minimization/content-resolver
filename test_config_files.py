#!/usr/bin/python3

import feedback_pipeline

def create_mock_settings():
    settings = {}
    settings["configs"] = "input/configs"
    settings["allowed_arches"] = ["armv7hl","aarch64","ppc64le","s390x","x86_64"]

    return settings
  
def main():
    feedback_pipeline.get_configs(create_mock_settings())
    
if __name__ == "__main__":
    main()

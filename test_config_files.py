#!/usr/bin/python3

import content_resolver.config_manager

def create_mock_settings():
    settings = {}
    settings["configs"] = "input/configs"
    settings["strict"] = True
    settings["allowed_arches"] = ["aarch64","ppc64le","s390x","x86_64"]

    return settings
  
def main():
    config_manager = content_resolver.config_manager.ConfigManager()
    config_manager.get_configs(create_mock_settings())
    
if __name__ == "__main__":
    main()

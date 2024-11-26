#!/usr/bin/python3

from content_resolver.config_manager import ConfigManager

def create_mock_settings():
    settings = {}
    settings["configs"] = "input/configs"
    settings["strict"] = True
    settings["allowed_arches"] = ["aarch64","ppc64le","s390x","x86_64"]

    return settings
  
def main():
    config_manager = ConfigManager(create_mock_settings())
    config_manager.get_configs()
    
if __name__ == "__main__":
    main()

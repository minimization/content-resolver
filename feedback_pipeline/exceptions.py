class SettingsError(Exception):
    # Error in global settings for Feedback Pipeline
    # Settings to be implemented, now hardcoded below
    pass


class ConfigError(Exception):
    # Error in user-provided configs
    pass


class RepoDownloadError(Exception):
    # Error in downloading repodata
    pass


class BuildGroupAnalysisError(Exception):
    # Error while processing buildroot build group
    pass


class KojiRootLogError(Exception):
    pass


class AnalysisError(Exception):
    pass

import asyncio
import datetime
import json
import multiprocessing
import os
import sys
import tempfile
import time
import traceback
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed

import koji
from libdnf5.base import Base, Goal, GoalJobSettings
from libdnf5.exception import BaseTransactionError
from libdnf5.exception import Error as DnfErr
from libdnf5.exception import RepoDownloadError as Dnf5RepoDownloadError
from libdnf5.exception import UserAssertionError
from libdnf5.common import QueryCmp_GLOB
from libdnf5.repo import RepoQuery
from libdnf5.rpm import PackageQuery

from content_resolver.exceptions import (
    AnalysisError,
    BuildGroupAnalysisError,
    KojiRootLogError,
    RepoDownloadError,
)
from content_resolver.utils import (
    dnf5_base,
    dump_data,
    err_log,
    load_data,
    log,
    pkg_id_to_name,
    size,
    url_to_id,
    workload_id_to_conf_id,
)


def pkg_placeholder_name_to_id(placeholder_name):
    return f"{placeholder_name}-000-placeholder.placeholder"


def pkg_placeholder_name_to_nevr(placeholder_name):
    return f"{placeholder_name}-000-placeholder"


def is_package_resolvable(base: Base, pkg_name: str) -> bool:
    """
    Check if a package name is resolvable, either as a real package or via provides.

    Args:
        base: DNF5 base object
        pkg_name: Package name to check

    Returns:
        bool: True if the package can be resolved, False otherwise
    """
    query = PackageQuery(base)

    # DNF5's filter_name() defaults to exact matching, so we must
    # explicitly use QueryCmp_GLOB when the name contains wildcards.
    # TODO: do we need more precise wildcard chars?
    is_glob = any(c in pkg_name for c in ('*', '?'))

    # First check if it exists as a real package name
    if is_glob:
        query.filter_name([pkg_name], QueryCmp_GLOB)
    else:
        query.filter_name([pkg_name])
    if not query.empty():
        return True

    # If not found by name, check if any package provides it.
    # Note: Must create a new query because DNF5 filters are cumulative.
    # Reusing the filtered query would search for provides within the
    # already-filtered (possibly empty) result set, not all packages.
    query = PackageQuery(base)
    if is_glob:
        query.filter_provides([pkg_name], QueryCmp_GLOB)
    else:
        query.filter_provides([pkg_name])
    if not query.empty():
        return True

    return False


#####################################################
### Find build dependencies from a Koji root log ###
####################################################

def _get_build_deps_from_a_root_log(root_log):
    """
    Given a packages Koji root_log, find its build dependencies.
    """
    required_pkgs = []

    # The individual states are nicely described inside the for loop.
    # They're processed in order
    state = 0

    for file_line in root_log.splitlines():
        split_line = file_line.split()
        line_len = len(split_line)

        # 0/
        # parts of the log I don't really care about
        if state == 0:
            # The next installation is the build deps!
            # So I start caring. Next state!
            if "'builddep', '--installroot'" in file_line:
                state += 1

        # 1/
        # getting the "already installed" packages to the list
        elif state == 1:
            # "Package already installed" indicates it's directly required,
            # so save it.
            # DNF5 does this after "Repositories loaded" and quotes the NVR;
            # DNF4 does this before "Dependencies resolved" without the quotes.
            if "is already installed." in file_line:
                pkg_name = split_line[3].strip('"').rsplit("-", 2)[0]
                required_pkgs.append(pkg_name)

            # That's all! Next state! (DNF4)
            elif "Dependencies resolved." in file_line:
                state += 1

            # That's all! Next state! (DNF5)
            elif "Repositories loaded." in file_line:
                state += 1

        # 2/
        # going through the log right before the first package name
        elif state == 2:
            # "Package already installed" indicates it's directly required,
            # so save it.
            # DNF4 does this before "Dependencies resolved" without the quotes;
            # DNF5 does this after "Repositories loaded" and quotes the NVR, but
            # sometimes prints this in the middle of a dependency line.
            if "is already installed." in file_line:
                pkg_index = split_line.index("already") - 2
                pkg_name = split_line[pkg_index].strip('"').rsplit("-", 2)[0]
                required_pkgs.append(pkg_name)

            # The next line will be the first package. Next state!
            # DNF5 reports "Installing: ## packages" in the Transaction Summary,
            # which we need to ignore
            if "Installing:" in file_line and line_len == 3:
                state += 1

        # 3/
        # And now just saving the packages until the "installing dependencies" part
        # or the "transaction summary" part if there's no dependencies
        elif state == 3:
            if "Installing dependencies:" in file_line:
                state = 2

            elif "Transaction Summary" in file_line:
                state = 2

            # Sometimes DNF5 prints "Package ... is already installed" in middle of the output.
            elif split_line[2] == "Package" and split_line[-1] == "installed.":
                pkg_name = split_line[3].strip('"').rsplit("-", 2)[0]
                required_pkgs.append(pkg_name)

            else:
                # We need to deal with the following...
                #
                # DEBUG util.py:446:   gobject-introspection-devel     aarch64 1.70.0-1.fc36              build 1.1 M
                # DEBUG util.py:446:   graphene-devel                  aarch64 1.10.6-3.fc35              build 159 k
                # DEBUG util.py:446:   gstreamer1-plugins-bad-free-devel
                # DEBUG util.py:446:                                   aarch64 1.19.2-1.fc36              build 244 k
                # DEBUG util.py:446:   json-glib-devel                 aarch64 1.6.6-1.fc36               build 173 k
                # DEBUG util.py:446:   libXcomposite-devel             aarch64 0.4.5-6.fc35               build  16 k
                #
                # The "gstreamer1-plugins-bad-free-devel" package name is too long to fit in the column,
                # so it gets split on two lines.
                #
                # When using the usual split_line[2] we get the correct name,
                # but the next line gives us "aarch64" as a package name which is wrong.
                #
                # So the usual line has line_len == 8
                # The one with the long package name has line_len == 3
                # and the one following it has line_len == 7
                #
                # One more thing... long release!
                #
                # DEBUG util.py:446:   qrencode-devel               aarch64 4.0.2-8.fc35                  build  13 k
                # DEBUG util.py:446:   systemtap-sdt-devel          aarch64 4.6~pre16291338gf2c14776-1.fc36
                # DEBUG util.py:446:                                                                      build  71 k
                # DEBUG util.py:446:   tpm2-tss-devel               aarch64 3.1.0-4.fc36                  build 315 k
                #
                # So the good one here is line_len == 5.
                # And the following is also line_len == 5. Fun!
                #
                # So if it ends with B, k, M, G it's the wrong line, so skip, otherwise take the package name.
                #
                # we can also anticipate both get long... that would mean we need to skip line_len == 4.

                if line_len in (10, 11):
                    # Sometimes DNF5 prints "Package ... is already installed" in the middle of a line
                    pkg_index = split_line.index("already") - 2
                    pkg_name = split_line[pkg_index].strip('"').rsplit("-", 2)[0]
                    required_pkgs.append(pkg_name)
                    pkg_name = split_line[7] if pkg_index == 3 else split_line[2]
                    required_pkgs.append(pkg_name)

                # TODO: line_len == 9 ??

                elif line_len in (3, 8):
                    pkg_name = split_line[2]
                    required_pkgs.append(pkg_name)

                elif line_len in (4, 7):
                    continue

                elif line_len in (5, 6):
                    # DNF5 uses B/KiB/MiB/GiB, DNF4 uses B/k/M/G
                    if split_line[4] in ["B", "KiB", "k", "MiB", "M", "GiB", "G"]:
                        continue
                    else:
                        pkg_name = split_line[2]
                        required_pkgs.append(pkg_name)

                else:
                    raise KojiRootLogError

        # 4/
        # I'm done. So I can break out of the loop.
        elif state == 4:
            break

    return required_pkgs


def _get_koji_log_path(srpm_id, arch, koji_session):
    """
    Get koji log path for a given SRPM.
    """
    max_tries = 10
    attempts = 0

    while attempts < max_tries:
        try:
            koji_pkg_data = koji_session.getRPM(f"{srpm_id}.src")
            koji_logs = koji_session.getBuildLogs(koji_pkg_data["build_id"])
            break
        except koji.GenericError:
            attempts += 1
            if attempts == max_tries:
                raise KojiRootLogError("Could not talk to Koji API")
            time.sleep(1)

    # TODO: use `next` and comprehension for lazy evaluation next(x for x in koji_logs)
    koji_log_path = None
    for koji_log in koji_logs:
        if koji_log["name"] == "root.log":
            if koji_log["dir"] == arch or koji_log["dir"] == "noarch":
                koji_log_path = koji_log["path"]
                break

    return koji_log_path


def _download_root_log_with_retry(root_log_url):
    """
    Download root.log file with retry logic.
    """
    max_tries = 10
    attempts = 0

    request = urllib.request.Request(root_log_url)
    request.add_header("Accept", "text/plain")
    request.add_header("User-Agent", "ContentResolver/1.0")

    while attempts < max_tries:
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                root_log_data = response.read()
                return root_log_data.decode("utf-8")
        except Exception:
            attempts += 1
            if attempts == max_tries:
                raise KojiRootLogError(f"Could not download root.log from {root_log_url}")
            time.sleep(1)


def process_single_srpm_root_log(work_item):
    """
    Process a single SRPM's root.log file.

    Args:
        work_item (dict): Contains koji_api_url, koji_files_url, srpm_id, arch, dev_buildroot

    Returns:
        dict: Contains srpm_id, arch, deps (list), error (str or None)
    """
    try:
        koji_api_url = work_item["koji_api_url"]
        koji_files_url = work_item["koji_files_url"]
        srpm_id = work_item["srpm_id"]
        arch = work_item["arch"]
        dev_buildroot = work_item.get("dev_buildroot", False)

        # Handle development buildroot mode
        if dev_buildroot:
            # Making sure there are 3 passes at least, but that it won't get overwhelmed
            srpm_name = srpm_id.rsplit("-", 2)[0]
            if srpm_name in ["bash", "make", "unzip"]:
                return {
                    'srpm_id': srpm_id,
                    'arch': arch,
                    'deps': ["gawk", "xz", "findutils"],
                    'error': None
                }
            elif srpm_name in ["gawk", "xz", "findutils"]:
                return {
                    'srpm_id': srpm_id,
                    'arch': arch,
                    'deps': ['cpio', 'diffutils'],
                    'error': None
                }
            return {
                'srpm_id': srpm_id,
                'arch': arch,
                'deps': ["bash", "make", "unzip"],
                'error': None
            }

        # Handle special cases
        if srpm_id.rsplit("-", 2)[0] in ["shim"]:
            return {
                'srpm_id': srpm_id,
                'arch': arch,
                'deps': [],
                'error': None
            }

        # Create koji session
        koji_session = koji.ClientSession(koji_api_url, opts={"timeout": 20})

        # Get koji log path
        koji_log_path = _get_koji_log_path(srpm_id, arch, koji_session)

        if not koji_log_path:
            return {
                'srpm_id': srpm_id,
                'arch': arch,
                'deps': [],
                'error': None
            }

        # Download root.log
        root_log_url = f"{koji_files_url}/{koji_log_path}"
        try:
            root_log_contents = _download_root_log_with_retry(root_log_url)
        except KojiRootLogError as e:
            # Download failed
            return {
                'srpm_id': srpm_id,
                'arch': arch,
                'deps': [],
                'error': f"Download failed: {str(e)}"
            }

        # Parse dependencies
        try:
            deps = _get_build_deps_from_a_root_log(root_log_contents)
        except Exception as e:
            # Parsing failed
            return {
                'srpm_id': srpm_id,
                'arch': arch,
                'deps': [],
                'error': f"Parse failed: {str(e)}"
            }

        # Check if parsing yielded suspiciously few dependencies
        warning = None
        if len(deps) == 0:
            warning = "WARNING: Zero dependencies found (possible truncated/corrupted log)"
        elif len(deps) < 3:
            warning = f"WARNING: Only {len(deps)} dependencies found (expected more)"

        return {
            'srpm_id': srpm_id,
            'arch': arch,
            'deps': deps,
            'error': None,
            'warning': warning
        }

    except Exception as e:
        return {
            'srpm_id': work_item.get('srpm_id', 'unknown'),
            'arch': work_item.get('arch', 'unknown'),
            'deps': [],
            'error': str(e)
        }

class Analyzer:

    ###############################################################################
    ### Analyzing stuff! ##########################################################
    ###############################################################################

    # Configs:
    #   TYPE:           KEY:          ID:
    # - repo            repos         repo_id
    # - env_conf        envs          env_id
    # - workload_conf   workloads     workload_id
    # - label           labels        label_id
    # - conf_view       views         view_id
    #
    # Data:
    #   TYPE:         KEY:                 ID:
    # - pkg           pkgs/repo_id/arch    NEVR
    # - env           envs                 env_id:repo_id:arch_id
    # - workload      workloads            workload_id:env_id:repo_id:arch_id
    # - view          views                view_id:repo_id:arch_id
    #
    # self.tmp_dnf_cachedir is either "dnf_cachedir" in TemporaryDirectory or set by --dnf-cache-dir
    # contents:
    # - "dnf_cachedir-{repo}-{arch}"                     <-- internal DNF cache
    #
    # self.tmp_installroots is "installroots" in TemporaryDirectory
    # contents:
    # - "dnf_generic_installroot-{repo}-{arch}"          <-- installroots for _analyze_pkgs
    # - "dnf_env_installroot-{env_conf}-{repo}-{arch}"   <-- installroots for envs and workloads and buildroots
    #
    #

    def __init__(self, configs, settings):
        self.workload_queue = {}
        self.workload_queue_counter_total = 0
        self.workload_queue_counter_current = 0
        self.current_subprocesses = 0

        self.configs = configs
        self.settings = settings
        self.data = {}
        self.cache = {}

        self.cache["root_log_deps"] = {}
        self.cache["root_log_deps"]["current"] = {}
        self.cache["root_log_deps"]["next"] = {}

        # Cache for compose metadata (composeinfo.json)
        # Maps repo_id -> {arch -> [list of available variant names]}
        self.compose_metadata_cache = {}

        self.metrics_data = []

        # When analysing buildroot, we don't need metadata about
        # recommends. So this gets flipped and we don't collect them anymore.
        # Saves more than an hour.
        self._global_performance_hack_run_recommends_queries = True

        try:
            self.cache["root_log_deps"]["current"] = load_data(self.settings["root_log_deps_cache_path"])
            # Validate cached root.log data for issues
            self._validate_root_log_cache()
        except FileNotFoundError:
            pass

    def _record_metric(self, name):
        this_record = {
            "name": name,
            "timestamp": datetime.datetime.now(),
        }
        self.metrics_data.append(this_record)

    def print_metrics(self):
        log("Additional metrics:")
        # TODO: use `enumerate(self.metrics_data)` instead of incrementing counter
        counter = 0

        for this_record in self.metrics_data:

            if counter == 0:
                prev_timestamp = this_record["timestamp"]
            else:
                self.metrics_data[counter-1]["timestamp"]

            time_diff = this_record["timestamp"] - prev_timestamp

            print(
                f"  {this_record['timestamp'].strftime('%H:%M:%S')} "
                f"(+{str(int(time_diff.seconds / 60)).zfill(3)} mins): "
                f"{this_record['name']}"
            )

            counter += 1

    def _validate_root_log_cache(self):
        """
        Validate cached root.log dependency data for invalid entries.
        Reports packages with zero or very low dependency counts.
        """
        cache = self.cache["root_log_deps"]["current"]
        if not cache:
            return

        zero_deps_cached = []
        low_deps_cached = []

        for koji_id, arches in cache.items():
            for arch, srpms in arches.items():
                for srpm_id, deps in srpms.items():
                    if not isinstance(deps, list):
                        continue

                    if len(deps) == 0:
                        zero_deps_cached.append({
                            'srpm_id': srpm_id,
                            'arch': arch,
                            'koji_id': koji_id
                        })
                    elif len(deps) < 3:
                        low_deps_cached.append({
                            'srpm_id': srpm_id,
                            'arch': arch,
                            'deps': deps,
                            'koji_id': koji_id
                        })

        # Some verbose logging for debugging
        # TODO: Enable some form of log level to increase/decrease verbosity of logs
        if zero_deps_cached or low_deps_cached:
            log("")
            log("=" * 80)
            log("⚠️  ROOT LOG CACHE VALIDATION")
            log("=" * 80)

        if zero_deps_cached:
            log(f"Found {len(zero_deps_cached)} cached packages with ZERO dependencies")
            log("These packages may have had root.log download/parse failures in previous runs:")
            log("")
            for item in zero_deps_cached[:15]:
                log(f"  ⚠️  {item['srpm_id']} ({item['arch']})")
            if len(zero_deps_cached) > 15:
                log(f"  ... and {len(zero_deps_cached) - 15} more")
            log("")
            log("Consider clearing cache for these packages to retry root.log download")

        if low_deps_cached:
            log("")
            log(f"Found {len(low_deps_cached)} cached packages with very few dependencies (<3)")
            for item in low_deps_cached[:10]:
                log(f"  ⚠️  {item['srpm_id']} ({item['arch']}): {item['deps']}")
            if len(low_deps_cached) > 10:
                log(f"  ... and {len(low_deps_cached) - 10} more")

        if zero_deps_cached or low_deps_cached:
            log("=" * 80)
            log("")

    def _get_available_compose_variants(self, repo, arch):
        """
        Fetch and parse composeinfo.json to determine which variants are available
        for a given repository and architecture.

        Returns:
            dict: Mapping of config repo names to boolean availability
                  e.g., {'BaseOS': True, 'HA': True, 'RS': False, 'Rawhide': True}
                  Returns empty dict if composeinfo is not available or on error.

        Caches results in self.compose_metadata_cache[repo_id][arch].

        Note: Maps config repo names (like 'HA', 'buildroot') to compose variant names
        (like 'HighAvailability', 'Buildroot') and marks external fallback repos like
        'Rawhide' as always available.
        """
        repo_id = repo["id"]

        # Check cache first
        if repo_id not in self.compose_metadata_cache:
            self.compose_metadata_cache[repo_id] = {}

        if arch in self.compose_metadata_cache[repo_id]:
            return self.compose_metadata_cache[repo_id][arch]

        # If no composeinfo URL is configured, return empty dict (no filtering)
        composeinfo_url = repo["source"].get("composeinfo")
        if not composeinfo_url:
            self.compose_metadata_cache[repo_id][arch] = {}
            return {}

        # Mapping of config repo names to compose variant names
        # This handles cases where the names differ
        repo_name_mapping = {
            "HA": "HighAvailability",
            "buildroot": "Buildroot",
        }

        # External fallback repos that aren't part of the compose
        # These should always be marked as available
        external_repos = {"Rawhide"}

        try:
            # Fetch composeinfo.json
            with urllib.request.urlopen(composeinfo_url, timeout=10) as response:
                compose_data = json.loads(response.read().decode('utf-8'))

            # Extract variants available for this architecture
            compose_variants = set()
            variants = compose_data.get("payload", {}).get("variants", {})

            for variant_name, variant_data in variants.items():
                variant_arches = variant_data.get("arches", [])
                if arch in variant_arches:
                    compose_variants.add(variant_name)

            # Build availability map for all config repo names
            availability = {}
            for config_name in repo["source"]["repos"].keys():
                # Check if this is an external fallback repo
                if config_name in external_repos:
                    availability[config_name] = True
                    continue

                # Map config name to compose variant name
                compose_name = repo_name_mapping.get(config_name, config_name)

                # Check if the variant exists in the compose
                availability[config_name] = compose_name in compose_variants

            # Cache and return
            self.compose_metadata_cache[repo_id][arch] = availability
            return availability

        except Exception as e:
            # On any error (network, parsing, etc.), log and return empty dict
            # Empty dict means no filtering - we'll try all repos as before
            log(f"  Warning: Could not fetch composeinfo from {composeinfo_url}: {e}")
            self.compose_metadata_cache[repo_id][arch] = {}
            return {}

    def _load_repo_cached(self, base, repo, arch):
        """
        Load repository configuration for the given base, repo, and architecture.

        DNF5 MIGRATION NOTE - Repo Caching Disabled:
        =============================================

        DNF5: Repo caching code removed (see _load_repo_cached docstring for explanation)
        In DNF4, we cached repo objects here for reuse across Base instances:
        DNF5's architecture prevents this - repos are tightly bound to their parent Base
        and cannot be safely transferred between instances without memory corruption.

        DNF4 Behavior (OLD):
        -------------------
        This function cached repo objects in self.global_dnf_repo_cache and reused them
        across multiple dnf.Base() instances. This was a major performance optimization:
        - First analysis: ~10s to load repos from network
        - Subsequent analyses: ~1s using cached repo objects
        - For 1000+ analyses, this saved ~150 minutes of execution time

        DNF5 Limitation (CURRENT):
        --------------------------
        DNF5 has tighter coupling between Repo objects and their parent Base instance:
        1. Repos are created via repo_sack.create_repo(), not standalone constructors
        2. Repo objects hold internal references to their parent Base
        3. RepoSack owns the repos and manages their lifecycle
        4. Attempting to reuse a Repo from Base1 in Base2 may cause
            Segmentation faults, Memory corruption or other Undefined behavior

        Future Solutions (TODO):
        ------------------------
        1. Use DNF5's built-in metadata cache (set metadata_expire to cache for longer)
        2. Keep a single long-lived Base instance for all analyses (requires state isolation)
        3. Wait for DNF5 upstream to add repo serialization/deserialization API
        4. Cache downloaded metadata files manually, let DNF5 reload from cache

        """

        repo_id = repo["id"]

        # Get available variants from composeinfo.json (if configured)
        available_variants = self._get_available_compose_variants(repo, arch)

        for repo_name, repo_data in repo["source"]["repos"].items():
            if repo_data["limit_arches"] and arch not in repo_data["limit_arches"]:
                continue

            # Check if variant exists in compose (if composeinfo is available)
            if available_variants and not available_variants.get(repo_name, True):
                log(f"  Skipping {repo_name} on {arch} (not in compose)")
                continue

            config = base.get_config()
            repo_sack = base.get_repo_sack()
            additional_repo = repo_sack.create_repo(repo_name)
            repo_config = additional_repo.get_config()
            repo_config.get_baseurl_option().set([repo_data["baseurl"]])
            repo_config.get_priority_option().set(repo_data["priority"])
            # DNF5: Set excludes via repo config, not repo_sack
            if repo_data["exclude"]:
                repo_config.get_excludepkgs_option().set(repo_data["exclude"])


    def _analyze_pkgs(self, repo, arch):
        log(f"Analyzing pkgs for {repo['name']} ({repo['id']}) {arch}")

        # TODO: Move away from context manager, only implemented to reduce changes from DNF4-> DNF5
        with dnf5_base() as base:
            config = base.get_config()
            config.get_debuglevel_option().set(0)
            # Note: DNF5 doesn't have errorlevel/logfilelevel in same way

            # Local DNF cache
            cachedir_name = f"dnf_cachedir-{repo['id']}-{arch}"
            config.get_cachedir_option().set(os.path.join(self.tmp_dnf_cachedir, cachedir_name))

            # Generic installroot
            root_name = f"dnf_generic_installroot-{repo['id']}-{arch}"
            config.get_installroot_option().set(os.path.join(self.tmp_installroots, root_name))

            # Architecture and Releasever
            vars = base.get_vars()
            vars.set("arch", arch)
            vars.set("basearch", arch)
            vars.set("releasever", repo["source"]["releasever"])

            config.get_ignorearch_option().set(True)

            # DNF5: setup() must be called after configuration but before using repo_sack
            base.setup()
            # Get available variants from composeinfo.json (if configured)
            available_variants = self._get_available_compose_variants(repo, arch)

            # Initialize repo_sack & repo_names_to_load
            repo_sack = base.get_repo_sack()
            repo_names_to_load = []
            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"] and arch not in repo_data["limit_arches"]:
                    log(f"  Skipping {repo_name} on {arch}")
                    continue

                # Check if variant exists in compose (if composeinfo is available)
                if available_variants and not available_variants.get(repo_name, True):
                    log(f"  Skipping {repo_name} on {arch} (not in compose)")
                    continue


                log(f"  Including {repo_name}")

                new_repo = repo_sack.create_repo(repo_name)
                repo_config = new_repo.get_config()
                repo_config.get_baseurl_option().set([repo_data["baseurl"]])
                repo_config.get_priority_option().set(repo_data["priority"])
                repo_names_to_load.append(repo_name)

            # Load repos
            log("  Loading repos...")

            # At this stage, we need to get all packages from the repo listed.
            # That also includes modular packages. Modular packages in non-enabled
            # streams would be normally hidden. So we mark all the available repos as
            # hotfix repos to make all packages visible, including non-enabled streams.
            # DNF5: Iterate repos using RepoQuery
            repo_query = RepoQuery(base)
            for repo_weak_ptr in repo_query:
                repo_obj = repo_weak_ptr.get()
                # DNF5: module_hotfixes is a config option
                repo_obj.get_config().get_module_hotfixes_option().set(True)

            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            # If individual repos fail, disable them and continue with others
            max_tries = 10
            attempts = 0
            success = False
            failed_repos = []

            while attempts < max_tries and not success:
                try:
                    # DNF5: load repos instead of fill_sack
                    repo_sack.load_repos()
                    success = True
                    break
                except (UserAssertionError, DnfErr) as err:
                    attempts += 1
                    error_msg = str(err)
                    log(f"  Failed to download repodata (attempt {attempts}/{max_tries}). Error: {error_msg}")

                    # Try to identify which repo failed and disable it
                    for repo_name in repo_names_to_load:
                        if repo_name in error_msg:
                            if repo_name not in failed_repos:
                                log(f"  Disabling problematic repository: {repo_name}")
                                failed_repos.append(repo_name)
                                try:
                                    repo_query = RepoQuery(base)
                                    for repo_weak_ptr in repo_query:
                                        repo_obj = repo_weak_ptr.get()
                                        if repo_obj.get_id() == repo_name:
                                            repo_obj.disable()
                                            break
                                except Exception as disable_err:
                                    log(f"  Warning: Could not disable repo {repo_name}: {disable_err}")
                            break

            if not success:
                # If we still failed after trying to disable problematic repos, give up
                err = f"Failed to download repodata while analyzing repo '{repo['name']} ({repo['id']}) {arch}'"
                err_log(err)
                raise RepoDownloadError(err)

            if failed_repos:
                log(f"  WARNING: Proceeding without repositories: {', '.join(failed_repos)}")

            # DNF5: Create PackageQuery instead of using base.sack.query
            query = PackageQuery(base)

            # Get all packages
            # DNF5: PackageQuery is directly iterable, don't call it
            all_pkgs_set = set(query)
            pkgs = {}
            for pkg_object in all_pkgs_set:
                pkg_nevra = f"{pkg_object.get_name()}-{pkg_object.get_evr()}.{pkg_object.get_arch()}"
                pkg_nevr = f"{pkg_object.get_name()}-{pkg_object.get_evr()}"

                pkgs[pkg_nevra] = {
                    "id": pkg_nevra,
                    "name": pkg_object.get_name(),
                    "evr": pkg_object.get_evr(),
                    "nevr": pkg_nevr,
                    "arch": pkg_object.get_arch(),
                    "installsize": pkg_object.get_install_size(),
                    "description": pkg_object.get_description(),
                    "summary": pkg_object.get_summary(),
                    "source_name": pkg_object.get_source_name(),
                    "sourcerpm": pkg_object.get_sourcerpm(),
                    "reponame": pkg_object.get_repo_id(),
                }

            # There shouldn't be multiple packages of the same NVR
            # But the world isn't as simple! So add all reponames
            # to every package, in case it's in multiple repos

            repo_priorities = {
                repo_name: repo_data["priority"] for repo_name, repo_data in repo["source"]["repos"].items()
            }

            for pkg_object in all_pkgs_set:
                pkg_nevra = f"{pkg_object.get_name()}-{pkg_object.get_evr()}.{pkg_object.get_arch()}"
                reponame = pkg_object.get_repo_id()
                pkgs[pkg_nevra].setdefault("all_reponames", set()).add(reponame)

            for pkg_nevra, pkg in pkgs.items():
                all_repo_priorities = {repo_priorities[reponame] for reponame in pkg["all_reponames"]}

                highest_repo_priority = min(all_repo_priorities)

                pkgs[pkg_nevra]["highest_priority_reponames"] = {
                    reponame for reponame in pkg["all_reponames"] if repo_priorities[reponame] == highest_repo_priority
                }

            log(f"  Done!  ({len(pkgs)} packages in total)")
            log("")

        return pkgs

    def _analyze_repos(self):
        self.data["repos"] = {}
        for repo in self.configs["repos"].values():
            repo_id = repo["id"]
            self.data["pkgs"][repo_id] = {
                arch: self._analyze_pkgs(repo, arch) for arch in repo["source"]["architectures"]
            }
            self.data["repos"][repo_id] = {}

            # Reading the optional composeinfo
            self.data["repos"][repo_id]["compose_date"] = None
            self.data["repos"][repo_id]["compose_days_ago"] = 0
            if repo["source"]["composeinfo"]:
                # At this point, this is all I can do. Hate me or not, it gets us
                # what we need and won't break anything in case things go badly.
                request = urllib.request.Request(repo["source"]["composeinfo"])
                request.add_header("Accept", "application/json")
                request.add_header("User-Agent", "ContentResolver/1.0")
                try:
                    with urllib.request.urlopen(request) as response:
                        composeinfo_raw_response = response.read()

                    composeinfo_data = json.loads(composeinfo_raw_response)
                    self.data["repos"][repo_id]["composeinfo"] = composeinfo_data

                    compose_date = datetime.datetime.strptime(composeinfo_data["payload"]["compose"]["date"], "%Y%m%d").date()
                    self.data["repos"][repo_id]["compose_date"] = compose_date.strftime("%Y-%m-%d")

                    date_now = datetime.datetime.now().date()
                    self.data["repos"][repo_id]["compose_days_ago"] = (date_now - compose_date).days

                except:
                    pass

    @staticmethod
    def _resolve_dep_names(reldeps, pkg_id, provides_index):
        """Resolve reldeps to matching pkg_ids via the provides index."""
        matched = set()
        for dep in reldeps:
            matched.update(provides_index.get(dep.get_name(), set()))
        matched.discard(pkg_id)
        return matched

    def _analyze_package_relations(self, packages, package_placeholders=None):
        """Analyze package relationships for the given set of packages.

        Builds a provides index from all packages, then iterates each package's
        requires/recommends/supplements to populate reverse dependency maps.

        Args:
            packages: Iterable of DNF5 package objects (e.g., set, PackageQuery)
            package_placeholders: Optional dict of placeholder packages

        Returns:
            dict: Package relations mapping pkg_id -> relation data
        """
        relations = {}
        pkg_cache = {}

        # Build a provides index: provide_name -> set of pkg_ids
        provides_index = {}

        for pkg in packages:
            pkg_id = f"{pkg.get_name()}-{pkg.get_evr()}.{pkg.get_arch()}"
            pkg_cache[pkg_id] = pkg
            relations[pkg_id] = {
                "required_by": set(),
                "recommended_by": set(),
                "suggested_by": set(),
                "supplements": set(),
                "source_name": pkg.get_source_name(),
                "reponame": pkg.get_repo_id(),
            }

            # Index by package name (implicit provide)
            pkg_name = pkg.get_name()
            provides_index.setdefault(pkg_name, set()).add(pkg_id)

            # Index by explicit provides
            for prov in pkg.get_provides():
                provides_index.setdefault(prov.get_name(), set()).add(pkg_id)

        # Build reverse dependency maps
        for pkg_id, pkg in pkg_cache.items():
            for provider_id in self._resolve_dep_names(pkg.get_requires(), pkg_id, provides_index):
                relations[provider_id]["required_by"].add(pkg_id)

            if self._global_performance_hack_run_recommends_queries:
                for provider_id in self._resolve_dep_names(pkg.get_recommends(), pkg_id, provides_index):
                    relations[provider_id]["recommended_by"].add(pkg_id)

            # Supplements is the opposite direction: pkg supplements the targets
            for target_id in self._resolve_dep_names(pkg.get_supplements(), pkg_id, provides_index):
                relations[pkg_id]["supplements"].add(target_id)

        # Convert sets to sorted lists for JSON serialization
        for pkg_id in relations:
            relations[pkg_id]["required_by"] = sorted(relations[pkg_id]["required_by"])
            relations[pkg_id]["recommended_by"] = sorted(relations[pkg_id]["recommended_by"])
            relations[pkg_id]["supplements"] = sorted(relations[pkg_id]["supplements"])
            relations[pkg_id]["suggested_by"] = []

        if package_placeholders:
            for placeholder_name, placeholder_data in package_placeholders.items():
                placeholder_id = pkg_placeholder_name_to_id(placeholder_name)
                relations[placeholder_id] = {
                    "required_by": [],
                    "recommended_by": [],
                    "suggested_by": [],
                    "supplements": [],
                    "reponame": None,
                }
                for placeholder_dependency_name in placeholder_data["requires"]:
                    for pkg_id in relations:
                        if pkg_id_to_name(pkg_id) == placeholder_dependency_name:
                            relations[pkg_id]["required_by"].append(placeholder_id)

        return relations


    def _analyze_env_without_leaking(self, env_conf, repo, arch):

        # DNF leaks memory and file descriptors :/
        #
        # So, this workaround runs it in a subprocess that should have its resources
        # freed when done!

        queue_result = multiprocessing.Queue()
        process = multiprocessing.Process(target=self._analyze_env_process, args=(queue_result, env_conf, repo, arch))
        process.start()
        process.join()

        # This basically means there was an exception in the processing and the process crashed
        if queue_result.empty():
            raise AnalysisError

        return queue_result.get()


    def _analyze_env_process(self, queue_result, env_conf, repo, arch):

        env = self._analyze_env(env_conf, repo, arch)
        queue_result.put(env)

    def _analyze_env(self, env_conf, repo, arch):
        env = {}

        env["env_conf_id"] = env_conf["id"]
        env["pkg_ids"] = []
        env["repo_id"] = repo["id"]
        env["arch"] = arch

        env["pkg_relations"] = []

        env["errors"] = {}
        env["errors"]["non_existing_pkgs"] = []

        env["succeeded"] = True

        # TODO: migrate away from context manager
        with dnf5_base() as base:
            config = base.get_config()
            config.get_debuglevel_option().set(0)

            # Local DNF cache
            cachedir_name = f"dnf_cachedir-{repo['id']}-{arch}"
            config.get_cachedir_option().set(os.path.join(self.tmp_dnf_cachedir, cachedir_name))

            # Environment installroot
            root_name = f"dnf_env_installroot-{env_conf['id']}-{repo['id']}-{arch}"
            config.get_installroot_option().set(os.path.join(self.tmp_installroots, root_name))

            # Architecture and Releasever
            vars = base.get_vars()
            vars.set("arch", arch)
            vars.set("basearch", arch)
            vars.set("releasever", repo["source"]["releasever"])

            config.get_ignorearch_option().set(True)

            # Additional DNF Settings
            # Note: DNF5 tsflags are handled differently - these affect transaction behavior
            # justdb and noscripts are implicit in how DNF5 handles test transactions

            # Environment config
            if "include-weak-deps" not in env_conf["options"]:
                config.get_install_weak_deps_option().set(False)
            # Note: nodocs tsflags - DNF5 handles this via separate config

            # DNF5: setup() must be called after configuration but before using repo_sack
            base.setup()

            # Load repos
            self._load_repo_cached(base, repo, arch)

            # Build list of repo names for error handling
            repo_names_to_load = []
            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"] and arch not in repo_data["limit_arches"]:
                    continue
                repo_names_to_load.append(repo_name)

            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            max_tries = 10
            attempts = 0
            success = False
            failed_repos = []
            while attempts < max_tries:
                try:
                    # DNF5: load repos instead of fill_sack
                    repo_sack = base.get_repo_sack()
                    repo_sack.load_repos()
                    success = True
                    break
                except (UserAssertionError, DnfErr, RuntimeError) as err:
                    attempts += 1
                    error_msg = str(err)
                    log(f"  Failed to download repodata (attempt {attempts}/{max_tries}). Error: {err}")

                    # Try to identify which repo failed and disable it
                    for repo_name in repo_names_to_load:
                        if repo_name in error_msg:
                            if repo_name not in failed_repos:
                                log(f"  Disabling problematic repository: {repo_name}")
                                failed_repos.append(repo_name)
                                try:
                                    repo_query = RepoQuery(base)
                                    for repo_weak_ptr in repo_query:
                                        repo_obj = repo_weak_ptr.get()
                                        if repo_obj.get_id() == repo_name:
                                            repo_obj.disable()
                                            break
                                except Exception as disable_err:
                                    log(f"  Warning: Could not disable repo {repo_name}: {disable_err}")
                            break

            if not success:
                err = f"Failed to download repodata while analyzing environment '{env_conf['id']}' from '{repo['id']}' {arch}:"
                err_log(err)
                raise RepoDownloadError(err)

            if failed_repos:
                log(f"  WARNING: Proceeding without repositories: {', '.join(failed_repos)}")

            # DNF5: Create a Goal for package operations
            goal = Goal(base)

            # Packages
            log("  Adding packages...")
            for pkg in env_conf["packages"]:
                # DNF5: Check if package is resolvable (by name or provides) before adding
                if not is_package_resolvable(base, pkg):
                    env["errors"]["non_existing_pkgs"].append(pkg)
                    continue
                goal.add_install(pkg)

            # Groups
            log("  Adding groups...")
            if env_conf["groups"]:
                # DNF5: Groups are loaded as part of repos
                pass
            for grp_spec in env_conf["groups"]:
                try:
                    # DNF5: add_group_install takes spec and options
                    settings = GoalJobSettings()
                    goal.add_group_install(grp_spec, settings)
                except (UserAssertionError, DnfErr):
                    env["errors"]["non_existing_pkgs"].append(grp_spec)
                    continue

            # Architecture-specific packages
            for pkg in env_conf["arch_packages"][arch]:
                # DNF5: Check if package is resolvable (by name or provides) before adding
                if not is_package_resolvable(base, pkg):
                    env["errors"]["non_existing_pkgs"].append(pkg)
                    continue
                goal.add_install(pkg)

            # Resolve dependencies
            log("  Resolving dependencies...")
            try:
                # DNF5: resolve via goal
                transaction = goal.resolve()
            except DnfErr as err:
                err_log(f"Failed to analyze environment '{env_conf['id']}' from '{repo['id']}' {arch}:")
                err_log(f"  - {err}")
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            # Write the result into RPMDB.
            # The transaction needs us to download all the packages. :(
            # So let's do that to make it happy.
            log("  Downloading packages...")
            try:
                # DNF5: download packages from transaction
                transaction.download()
            except Dnf5RepoDownloadError as err:
                err_log(f"Failed to analyze environment '{env_conf['id']}' from '{repo['id']}' {arch}:")
                err_log(f"  - {err}")
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            log("  Running DNF transaction, writing RPMDB...")
            try:
                # DNF5: run transaction
                transaction.run()
            except BaseTransactionError as err:
                err_log(f"Failed to analyze environment '{env_conf['id']}' from '{repo['id']}' {arch}:")
                err_log(f"  - {err}")
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            # Get packages from transaction
            log("  Extracting packages from transaction...")
            # DNF5: transaction packages vector is directly indexable
            pkg_list = []
            trans_pkgs = transaction.get_transaction_packages()
            for i in range(trans_pkgs.size()):
                trans_pkg = trans_pkgs[i]
                pkg_list.append(trans_pkg.get_package())

            # Use the actual packages from the transaction.
            # Don't re-query by name - that would return ALL versions from ALL repos,
            # including lower-priority duplicates (e.g., both ELN and Rawhide versions).
            for pkg in pkg_list:
                pkg_id = f"{pkg.get_name()}-{pkg.get_evr()}.{pkg.get_arch()}"
                env["pkg_ids"].append(pkg_id)

            env["pkg_relations"] = self._analyze_package_relations(pkg_list)

            log(f"  Done!  ({len(env['pkg_ids'])} packages in total)")
            log("")

        return env

    def _analyze_envs(self):
        envs = {}

        # Look at all env configs...
        for env_conf_id, env_conf in self.configs["envs"].items():
            # For each of those, look at all repos it lists...
            for repo_id in env_conf["repositories"]:
                # And for each of the repo, look at all arches it supports.
                repo = self.configs["repos"][repo_id]
                for arch in repo["source"]["architectures"]:
                    # Now it has
                    #    all env confs *
                    #    repos each config lists *
                    #    archeas each repo supports
                    # Analyze all of that!
                    log(f"Analyzing {env_conf['name']} ({env_conf_id}) from {repo['name']} ({repo_id}) {arch}...")

                    env_id = f"{env_conf_id}:{repo_id}:{arch}"
                    envs[env_id] = self._analyze_env(env_conf, repo, arch)

        self.data["envs"] = envs

    def _return_failed_workload_env_err(self, workload_conf, env_conf, repo, arch):
        workload = {
            "workload_conf_id": workload_conf["id"],
            "env_conf_id": env_conf["id"],
            "repo_id": repo["id"],
            "arch": arch,
            "pkg_env_ids": [],
            "pkg_added_ids": [],
            "pkg_placeholder_ids": [],
            "pkg_relations": [],
            "errors": {
                "non_existing_pkgs": [],
                "message": """
        Failed to analyze this workload because of an error while analyzing the environment.

        Please see the associated environment results for a detailed error message.
        """,
            },
            "succeeded": False,
            "env_succeeded": False,
        }

        return workload


    def _analyze_workload(self, workload_conf, env_conf, repo, arch):

        # Figure out the workload labels
        # It can only have labels that are in both the workload_conf and the env_conf
        workload = {
            "workload_conf_id": workload_conf["id"],
            "env_conf_id": env_conf["id"],
            "repo_id": repo["id"],
            "arch": arch,
            "pkg_env_ids": [],
            "pkg_added_ids": [],
            "pkg_placeholder_ids": [],
            "srpm_placeholder_names": [],
            "pkg_relations": [],
            "errors": {
                "non_existing_pkgs": [],
                "non_existing_placeholder_deps": [],
            },
            "warnings": {
                "non_existing_pkgs": [],
                "non_existing_placeholder_deps": [],
                "message": None,
            },
            "succeeded": True,
            "env_succeeded": True,
            "labels": list(set(workload_conf["labels"]) & set(env_conf["labels"])),
        }

        with dnf5_base() as base:
            config = base.get_config()
            config.get_debuglevel_option().set(0)

            # Local DNF cache
            cachedir_name = f"dnf_cachedir-{repo['id']}-{arch}"
            config.get_cachedir_option().set(os.path.join(self.tmp_dnf_cachedir, cachedir_name))

            # Environment installroot
            # Since we're not writing anything into the installroot,
            # let's just use the base image's installroot!
            root_name = f"dnf_env_installroot-{env_conf['id']}-{repo['id']}-{arch}"
            config.get_installroot_option().set(os.path.join(self.tmp_installroots, root_name))

            # Architecture and Releasever
            vars = base.get_vars()
            vars.set("arch", arch)
            vars.set("basearch", arch)
            vars.set("releasever", repo["source"]["releasever"])

            config.get_ignorearch_option().set(True)

            # Environment config
            if "include-weak-deps" not in workload_conf["options"]:
                config.get_install_weak_deps_option().set(False)
            # Note: nodocs is handled differently in DNF5

            # DNF5: setup() must be called after configuration but before using repo_sack
            base.setup()

            # Load repos
            self._load_repo_cached(base, repo, arch)

            # Build list of repo names for error handling
            repo_names_to_load = []
            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"] and arch not in repo_data["limit_arches"]:
                    continue
                repo_names_to_load.append(repo_name)

            # 0 %

            # Now I need to load the local RPMDB.
            # However, if the environment is empty, it wasn't created, so I need to treat
            # it differently. So let's check!
            repo_sack = base.get_repo_sack()
            if len(env_conf["packages"]) or len(env_conf["arch_packages"][arch]) or len(env_conf["groups"]):
                # It's not empty! Load local data.
                # DNF5: This loads both repos and system data
                # This sometimes fails, so let's try at least N times with repo-disabling logic
                max_tries = 10
                attempts = 0
                success = False
                failed_repos = []
                while attempts < max_tries:
                    try:
                        repo_sack.load_repos()
                        success = True
                        break
                    except (UserAssertionError, DnfErr, RuntimeError, Dnf5RepoDownloadError) as err:
                        attempts += 1
                        error_msg = str(err)
                        # log(f"  Failed to download repodata (attempt {attempts}/{MAX_TRIES}). Error: {error_msg}")

                        # Try to identify which repo failed and disable it
                        for repo_name in repo_names_to_load:
                            if repo_name in error_msg:
                                if repo_name not in failed_repos:
                                    # log(f"  Disabling problematic repository: {repo_name}")
                                    failed_repos.append(repo_name)
                                    try:
                                        repo_query = RepoQuery(base)
                                        for repo_weak_ptr in repo_query:
                                            repo_obj = repo_weak_ptr.get()
                                            if repo_obj.get_id() == repo_name:
                                                repo_obj.disable()
                                                break
                                    except Exception as disable_err:
                                        pass
                                        # log(f"  Warning: Could not disable repo {repo_name}: {disable_err}")
                                break

                if not success:
                    err = (f"Failed to download repodata while analyzing workload '{workload['workload_conf_id']}' on "
                           f"'{workload['env_conf_id']}' from '{workload['repo_id']}' {workload['arch']}...")
                    err_log(err)
                    raise RepoDownloadError(err)
            else:
                # It's empty. Treat it like we're using an empty installroot.
                # This sometimes fails, so let's try at least N times
                # before totally giving up!
                max_tries = 10
                attempts = 0
                success = False
                failed_repos = []
                while attempts < max_tries:
                    try:
                        repo_sack.load_repos()
                        success = True
                        break
                    except (UserAssertionError, DnfErr, RuntimeError, Dnf5RepoDownloadError) as err:
                        attempts += 1
                        error_msg = str(err)

                        # Try to identify which repo failed and disable it
                        for repo_name in repo_names_to_load:
                            if repo_name in error_msg:
                                if repo_name not in failed_repos:
                                    failed_repos.append(repo_name)
                                    try:
                                        repo_query = RepoQuery(base)
                                        for repo_weak_ptr in repo_query:
                                            repo_obj = repo_weak_ptr.get()
                                            if repo_obj.get_id() == repo_name:
                                                repo_obj.disable()
                                                break
                                    except Exception:
                                        pass
                                break

                if not success:
                    err = (f"Failed to download repodata while analyzing workload '{workload['workload_conf_id']}' on "
                           f"'{workload['env_conf_id']}' from '{workload['repo_id']}' {workload['arch']}...")
                    err_log(err)
                    raise RepoDownloadError(err)

            # 37 %

            # DNF5: Create Goal for package operations
            goal = Goal(base)

            # Packages
            for pkg in workload_conf["packages"]:
                # DNF5: Check if package is resolvable (by name or provides) before adding
                if not is_package_resolvable(base, pkg):
                    if pkg in self.settings["weird_packages_that_can_not_be_installed"]:
                        continue
                    else:
                        if "strict" in workload_conf["options"]:
                            workload["errors"]["non_existing_pkgs"].append(pkg)
                        else:
                            workload["warnings"]["non_existing_pkgs"].append(pkg)
                        continue
                goal.add_install(pkg)

            # Groups
            if workload_conf["groups"]:
                # DNF5: Groups are loaded automatically with repos
                pass
            for grp_spec in workload_conf["groups"]:
                try:
                    # DNF5: add_group_install with settings
                    settings = GoalJobSettings()
                    goal.add_group_install(grp_spec, settings)
                except (UserAssertionError, DnfErr):
                    workload["errors"]["non_existing_pkgs"].append(grp_spec)
                    continue

                # TODO: Mark group packages as required...

            # Filter out the relevant package placeholders for this arch
            package_placeholders = {}
            for placeholder_name, placeholder_data in workload_conf["package_placeholders"]["pkgs"].items():
                # If this placeholder is not limited to just a usbset of arches, add it
                if not placeholder_data["limit_arches"]:
                    package_placeholders[placeholder_name] = placeholder_data
                # otherwise it is limited. In that case, only add it if the current arch is on its list
                elif arch in placeholder_data["limit_arches"]:
                    package_placeholders[placeholder_name] = placeholder_data

            # Same for SRPM placeholders
            srpm_placeholders = {}
            for placeholder_name, placeholder_data in workload_conf["package_placeholders"]["srpms"].items():
                # If this placeholder is not limited to just a usbset of arches, add it
                if not placeholder_data["limit_arches"]:
                    srpm_placeholders[placeholder_name] = placeholder_data
                # otherwise it is limited. In that case, only add it if the current arch is on its list
                elif arch in placeholder_data["limit_arches"]:
                    srpm_placeholders[placeholder_name] = placeholder_data

            # Dependencies of package placeholders
            for placeholder_name, placeholder_data in package_placeholders.items():
                for pkg in placeholder_data["requires"]:
                    # DNF5: Check if package is resolvable (by name or provides) before adding
                    if not is_package_resolvable(base, pkg):
                        if "strict" in workload_conf["options"]:
                            workload["errors"]["non_existing_placeholder_deps"].append(pkg)
                        else:
                            workload["warnings"]["non_existing_placeholder_deps"].append(pkg)
                        continue
                    goal.add_install(pkg)

            # Architecture-specific packages
            for pkg in workload_conf["arch_packages"][arch]:
                # DNF5: Check if package is resolvable (by name or provides) before adding
                if not is_package_resolvable(base, pkg):
                    if "strict" in workload_conf["options"]:
                        workload["errors"]["non_existing_pkgs"].append(pkg)
                    else:
                        workload["warnings"]["non_existing_pkgs"].append(pkg)
                    continue
                goal.add_install(pkg)

            if workload["errors"]["non_existing_pkgs"] or workload["errors"]["non_existing_placeholder_deps"]:
                error_message_list = []
                if workload["errors"]["non_existing_pkgs"]:
                    error_message_list.append("The following required packages are not available:")
                    for pkg_name in workload["errors"]["non_existing_pkgs"]:
                        pkg_string = f"  - {pkg_name}"
                        error_message_list.append(pkg_string)
                if workload["errors"]["non_existing_placeholder_deps"]:
                    error_message_list.append("The following dependencies of package placeholders are not available:")
                    for pkg_name in workload["errors"]["non_existing_placeholder_deps"]:
                        pkg_string = f"  - {pkg_name}"
                        error_message_list.append(pkg_string)
                error_message = "\n".join(error_message_list)
                workload["succeeded"] = False
                workload["errors"]["message"] = str(error_message)
                return workload

            if workload["warnings"]["non_existing_pkgs"] or workload["warnings"]["non_existing_placeholder_deps"]:
                error_message_list = []
                if workload["warnings"]["non_existing_pkgs"]:
                    error_message_list.append("The following required packages are not available (and were skipped):")
                    for pkg_name in workload["warnings"]["non_existing_pkgs"]:
                        pkg_string = f"  - {pkg_name}"
                        error_message_list.append(pkg_string)
                    error_message_list.append("")
                    error_message_list.append("Note: Add 'strict' to the workload options to treat missing packages as errors.")
                if workload["warnings"]["non_existing_placeholder_deps"]:
                    error_message_list.append("The following dependencies of package placeholders are not available (and were skipped):")
                    # TODO: use comprehension and `error_message_list.extend([])`
                    for pkg_name in workload["warnings"]["non_existing_placeholder_deps"]:
                        pkg_string = f"  - {pkg_name}"
                        error_message_list.append(pkg_string)
                error_message = "\n".join(error_message_list)
                workload["warnings"]["message"] = str(error_message)
                log(f"  Warning: {len(workload['warnings']['non_existing_pkgs'])} packages not found and were skipped")

            # Resolve dependencies
            try:
                # DNF5: resolve via goal
                transaction = goal.resolve()
            except (DnfErr, RuntimeError, Exception) as err:
                workload["succeeded"] = False
                # Enhanced error message for dependency failures
                error_message = str(err)

                # Check if this is a dependency chain failure (missing transitive dependency)
                if "nothing provides" in error_message.lower() or "but none of the providers can be installed" in error_message.lower():
                    error_lines = ["Dependency resolution failed:", ""]
                    error_lines.append("This workload requires packages that have unmet dependencies.")
                    error_lines.append("Common causes:")
                    error_lines.append("  - A required package has been retired from Fedora")
                    error_lines.append("  - A dependency is missing or not yet built")
                    error_lines.append("  - Package maintainer needs to update dependencies")
                    error_lines.append("")
                    error_lines.append("Detailed error from DNF:")
                    error_lines.append("-" * 70)
                    error_lines.append(error_message)
                    workload["errors"]["message"] = "\n".join(error_lines)
                else:
                    workload["errors"]["message"] = error_message

                log(f"  Failed to resolve dependencies for {workload_conf['id']}")
                # Show full error for debugging (truncate at 2000 chars if too long)
                error_display = error_message if len(error_message) <= 2000 else error_message[:2000] + "...(truncated)"
                log(f"  Error: {error_display}")
                return workload

            # CRITICAL DNF4 vs DNF5 DIFFERENCE:
            # DNF4 raises exceptions when packages can't be resolved due to dependency failures.
            # DNF5 does NOT raise exceptions - it returns a transaction with problems recorded.
            # If we don't check transaction.get_problems(), packages with unresolvable dependencies
            # will be silently skipped, showing as "succeeded" with 0 packages installed.
            # If not used it causes the packages that have unresolvable deps to show NO errors in DNF5 while we
            # expect "nothing provides" errors.
            if transaction.get_problems() > 0:
                # Get error messages from resolve logs
                resolve_logs = transaction.get_resolve_logs_as_strings()
                error_message = "\n".join(resolve_logs)

                # DNF5 reports repo priority conflicts that DNF4 silently resolved.
                # Filter out version conflicts between repos (expected with multi-repo setups).
                # Keep real dependency failures (missing packages, broken deps).

                # Check if entire error message is about version conflicts between repos
                error_lower = error_message.lower()

                # Pattern 1: "cannot install both X from RepoA and X from RepoB"
                has_cannot_install_both = "cannot install both" in error_lower and " from " in error_lower

                # Pattern 2: Multi-line version conflicts like:
                #   "package X from RepoA requires Y = v1, but none of the providers can be installed"
                #   "package Z from RepoB requires Y = v2, but none of the providers can be installed"
                # These indicate different repos wanting different versions of same dependency
                lines = error_message.split('\n')
                requires_lines = [l for l in lines if 'requires' in l.lower() and 'from' in l.lower()]
                has_multi_version_conflict = len(requires_lines) >= 2

                # Pattern 3: "conflicting requests" or "cannot install the best candidate"
                has_conflict_markers = ("conflicting requests" in error_lower or
                                       "cannot install the best candidate" in error_lower)

                # Real dependency errors that should NOT be filtered
                has_nothing_provides = "nothing provides" in error_lower
                has_package_already_installed = "already installed" in error_lower

                # Filter logic: ignore if it's ONLY repo conflicts, no real missing deps
                is_repo_conflict = (has_cannot_install_both or
                                  (has_multi_version_conflict and has_conflict_markers))
                is_real_error = has_nothing_provides

                if is_repo_conflict and not is_real_error:
                    # Repo priority conflict - expected behavior, ignore
                    log("  Ignoring repository priority conflict (version mismatch between repos)")
                else:
                    # Real dependency failure
                    workload["succeeded"] = False
                    workload["errors"]["message"] = error_message
                    log(f"  Failed to resolve dependencies for {workload_conf['id']}")
                    # Show truncated error for debugging
                    error_display = error_message if len(error_message) <= 500 else error_message[:500] + "...(truncated)"
                    log(f"  Error: {error_display}")
                    return workload

            # DNF Query
            # Get installed packages from the system repo
            query_env = PackageQuery(base)
            query_env.filter_installed()
            pkgs_env = set(query_env)

            # Get packages being installed from transaction
            # DNF5: transaction packages vector is directly indexable
            pkgs_added = []
            trans_pkgs = transaction.get_transaction_packages()
            for i in range(trans_pkgs.size()):
                trans_pkg = trans_pkgs[i]
                pkgs_added.append(trans_pkg.get_package())
            pkgs_added = set(pkgs_added)

            pkgs_all = set.union(pkgs_env, pkgs_added)

            # OK all good so save stuff now
            # TODO: use comprehensions & .extend
            for pkg in pkgs_env:
                pkg_id = f"{pkg.get_name()}-{pkg.get_evr()}.{pkg.get_arch()}"
                workload["pkg_env_ids"].append(pkg_id)

            for pkg in pkgs_added:
                pkg_id = f"{pkg.get_name()}-{pkg.get_evr()}.{pkg.get_arch()}"
                workload["pkg_added_ids"].append(pkg_id)

            # No errors so far? That means the analysis has succeeded,
            # so placeholders can be added to the list as well.
            # (Failed workloads need to have empty results, that's why)
            for placeholder_name in package_placeholders:
                workload["pkg_placeholder_ids"].append(pkg_placeholder_name_to_id(placeholder_name))

            for srpm_placeholder_name in srpm_placeholders:
                workload["srpm_placeholder_names"].append(srpm_placeholder_name)

            # Use the actual packages that were installed/selected by DNF's priority logic.
            # Don't re-query by name - that would return ALL versions from ALL repos,
            # including lower-priority duplicates (e.g., both ELN and Rawhide versions).
            workload["pkg_relations"] = self._analyze_package_relations(pkgs_all, package_placeholders)


        return workload

    def _analyze_workload_process(self, queue_result, workload_conf, env_conf, repo, arch):
        try:
            workload = self._analyze_workload(workload_conf, env_conf, repo, arch)
            queue_result.put(workload)
        except Exception as e:
            # Create a failed workload result instead of crashing
            workload = {
                "workload_conf_id": workload_conf["id"],
                "env_conf_id": env_conf["id"],
                "repo_id": repo["id"],
                "arch": arch,
                "pkg_env_ids": [],
                "pkg_added_ids": [],
                "pkg_placeholder_ids": [],
                "srpm_placeholder_names": [],
                "pkg_relations": [],
                "errors": {
                    "non_existing_pkgs": [],
                    "non_existing_placeholder_deps": [],
                    "message": f"Workload analysis failed with exception:\n{type(e).__name__}: {str(e)}\n\nTraceback:\n{traceback.format_exc()}",
                },
                "warnings": {
                    "non_existing_pkgs": [],
                    "non_existing_placeholder_deps": [],
                    "message": None,
                },
                "succeeded": False,
                "env_succeeded": False,
                "labels": list(set(workload_conf["labels"]) & set(env_conf["labels"])),
            }
            queue_result.put(workload)
            # Log error to stderr so it appears in logs
            err_log(f" ERROR analyzing workload {workload_conf['id']}:{env_conf['id']}:{repo['id']}:{arch}-> {e}", file=sys.stderr)


    async def _analyze_workloads_subset_async(self, task_queue, results):

        for task in task_queue:
            workload_conf = task["workload_conf"]
            env_conf = task["env_conf"]
            repo = task["repo"]
            arch = task["arch"]

            workload_id = f"{workload_conf['id']}:{env_conf['id']}:{repo['id']}:{arch}"

            # Max processes
            while True:
                if self.current_subprocesses < self.settings["max_subprocesses"]:
                    self.current_subprocesses += 1
                    break
                else:
                    await asyncio.sleep(0.1)

            # Log progress
            self.workload_queue_counter_current += 1
            log(f"[{self.workload_queue_counter_current} of {self.workload_queue_counter_total}]")
            log(f"Analyzing workload: {workload_id}")
            log("")

            queue_result = multiprocessing.Queue()
            process = multiprocessing.Process(target=self._analyze_workload_process, args=(queue_result, workload_conf, env_conf, repo, arch), daemon=True)
            process.start()

            # Now wait a bit for the result.
            # This is a terrible way to implement an async way to
            # wait for the result with a 222 seconds timeout.
            # But it works. If anyone knows how to make it nicer, let me know! :D

            # 2 seconds
            for _ in range(1, 20):
                if queue_result.empty():
                    await asyncio.sleep(0.1)
                else:
                    break

            # 20 seconds
            for _ in range(1, 20):
                if queue_result.empty():
                    await asyncio.sleep(1)
                else:
                    break

            # 200 seconds
            for _ in range(1, 20):
                if queue_result.empty():
                    await asyncio.sleep(10)
                else:
                    break

            self.current_subprocesses -= 1

            # This basically means there was an exception in the processing and the process crashed
            # or timed out (222 seconds total timeout)
            if queue_result.empty():
                log("")
                log("")
                log("--------------------------------------------------------------------------")
                log("")
                log("WARNING: Workload analysis timed out or crashed")
                log("")
                log("Details:")
                log(f"  workload_conf: {workload_conf['id']}")
                log(f"  env_conf:      {env_conf['id']}")
                log(f"  repo:          {repo['id']}")
                log(f"  arch:          {arch}")
                log("")
                log("Creating failed workload entry and continuing...")
                log("")
                log("--------------------------------------------------------------------------")
                log("")
                log("")

                # Create a failed workload result instead of crashing
                results[workload_id] = {
                    "workload_conf_id": workload_conf["id"],
                    "env_conf_id": env_conf["id"],
                    "repo_id": repo["id"],
                    "arch": arch,
                    "pkg_env_ids": [],
                    "pkg_added_ids": [],
                    "pkg_placeholder_ids": [],
                    "srpm_placeholder_names": [],
                    "pkg_relations": [],
                    "errors": {
                        "non_existing_pkgs": [],
                        "non_existing_placeholder_deps": [],
                        "message": f"Workload analysis timed out after 222 seconds or subprocess crashed",
                    },
                    "warnings": {
                        "non_existing_pkgs": [],
                        "non_existing_placeholder_deps": [],
                        "message": None,
                    },
                    "succeeded": False,
                    "env_succeeded": False,
                    "labels": list(set(workload_conf["labels"]) & set(env_conf["labels"])),
                }
            else:
                workload = queue_result.get()
                results[workload_id] = workload


    async def _analyze_workloads_async(self, results):

        tasks = []

        for repo in self.workload_queue:
            for arch in self.workload_queue[repo]:
                task_queue = self.workload_queue[repo][arch]
                tasks.append(asyncio.create_task(self._analyze_workloads_subset_async(task_queue, results)))

        for task in tasks:
            await task

        log("DONE!")

    def _queue_workload_processing(self, workload_conf, env_conf, repo, arch):

        repo_id = repo["id"]

        if repo_id not in self.workload_queue:
            self.workload_queue[repo_id] = {}

        if arch not in self.workload_queue[repo_id]:
            self.workload_queue[repo_id][arch] = []

        workload_task = {
            "workload_conf": workload_conf,
            "env_conf" : env_conf,
            "repo" : repo,
            "arch" : arch
        }

        self.workload_queue[repo_id][arch].append(workload_task)
        self.workload_queue_counter_total += 1

    def _reset_workload_processing_queue(self):
        self.workload_queue = {}
        self.workload_queue_counter_total = 0
        self.workload_queue_counter_current = 0

    def _analyze_workloads(self):

        # Initialise
        self.data["workloads"] = {}
        self._reset_workload_processing_queue()

        # Here, I need to mix and match workloads & envs based on labels
        workload_env_map = {}
        # Look at all workload configs...
        for workload_conf_id, workload_conf in self.configs["workloads"].items():
            workload_env_map[workload_conf_id] = set()
            # ... and all of their labels.
            for label in workload_conf["labels"]:
                # And for each label, find all env configs...
                for env_conf_id, env_conf in self.configs["envs"].items():
                    # ... that also have the label.
                    if label in env_conf["labels"]:
                        # And save those.
                        workload_env_map[workload_conf_id].add(env_conf_id)

        # And now, look at all workload configs...
        for workload_conf_id, workload_conf in self.configs["workloads"].items():
            # ... and for each, look at all env configs it should be analyzed in.
            for env_conf_id in workload_env_map[workload_conf_id]:
                # Each of those envs can have multiple repos associated...
                env_conf = self.configs["envs"][env_conf_id]
                for repo_id in env_conf["repositories"]:
                    # ... and each repo probably has multiple architecture.
                    repo = self.configs["repos"][repo_id]
                    arches = repo["source"]["architectures"]

        # And now, look at all workload configs...
        for workload_conf_id, workload_conf in self.configs["workloads"].items():
            # ... and for each, look at all env configs it should be analyzed in.
            for env_conf_id in workload_env_map[workload_conf_id]:
                # Each of those envs can have multiple repos associated...
                env_conf = self.configs["envs"][env_conf_id]
                for repo_id in env_conf["repositories"]:
                    # ... and each repo probably has multiple architecture.
                    repo = self.configs["repos"][repo_id]
                    for arch in repo["source"]["architectures"]:

                        # And now it has:
                        #   all workload configs *
                        #   all envs that match those *
                        #   all repos of those envs *
                        #   all arches of those repos.
                        # That's a lot of stuff! Let's analyze all of that!

                        # Before even started, look if the env succeeded. If not, there's
                        # no point in doing anything here.
                        env_id = f"{env_conf['id']}:{repo['id']}:{arch}"
                        env = self.data["envs"][env_id]

                        if env["succeeded"]:
                            self._queue_workload_processing(workload_conf, env_conf, repo, arch)

                        else:
                            workload_id = f"{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}"
                            self.data["workloads"][workload_id] = self._return_failed_workload_env_err(workload_conf, env_conf, repo, arch)

        asyncio.run(self._analyze_workloads_async(self.data["workloads"]))

    def _init_view_pkg(self, input_pkg, arch, placeholder=False, level=0):
        if placeholder:
            pkg = {
                "id": pkg_placeholder_name_to_id(input_pkg["name"]),
                "name": input_pkg["name"],
                "evr": "000-placeholder",
                "nevr": pkg_placeholder_name_to_nevr(input_pkg["name"]),
                "arch": "placeholder",
                "installsize": 0,
                "description": input_pkg["description"],
                "summary": input_pkg["description"],
                "source_name": input_pkg["srpm"],
                "sourcerpm": f"{input_pkg['srpm']}-000-placeholder",
                "q_arch": input_pkg,
                "reponame": "n/a",
                "all_reponames": set(),
                "highest_priority_reponames": set()
            }

        else:
            pkg = dict(input_pkg)

        pkg["view_arch"] = arch

        pkg["placeholder"] = placeholder

        pkg["in_workload_ids_all"] = set()
        pkg["in_workload_ids_req"] = set()
        pkg["in_workload_ids_dep"] = set()
        pkg["in_workload_ids_env"] = set()

        pkg["in_buildroot_of_srpm_id_all"] = set()
        pkg["in_buildroot_of_srpm_id_req"] = set()
        pkg["in_buildroot_of_srpm_id_dep"] = set()
        pkg["in_buildroot_of_srpm_id_env"] = set()

        pkg["unwanted_completely_in_list_ids"] = set()
        pkg["unwanted_buildroot_in_list_ids"] = set()

        pkg["level"] = []

        # Level 0 is runtime
        pkg["level"].append({
            "all": pkg["in_workload_ids_all"],
            "req": pkg["in_workload_ids_req"],
            "dep": pkg["in_workload_ids_dep"],
            "env": pkg["in_workload_ids_env"],
        })

        # FIXME: Use comprehension & extend instead on for loop
        # Level 1 and higher is buildroot
        for _ in range(level):
            pkg["level"].append({
                "all": set(),
                "req": set(),
                "dep": set(),
                "env": set()
            })

        pkg["required_by"] = set()
        pkg["recommended_by"] = set()
        pkg["suggested_by"] = set()
        pkg["supplements"] = set()

        return pkg

    def _init_view_srpm(self, pkg, level=0):

        srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

        srpm = {}
        srpm["id"] = srpm_id
        srpm["name"] = pkg["source_name"]
        srpm["reponame"] = pkg["reponame"]
        srpm["pkg_ids"] = set()

        srpm["placeholder"] = False
        srpm["placeholder_directly_required_pkg_names"] = []

        srpm["in_workload_ids_all"] = set()
        srpm["in_workload_ids_req"] = set()
        srpm["in_workload_ids_dep"] = set()
        srpm["in_workload_ids_env"] = set()

        srpm["in_buildroot_of_srpm_id_all"] = set()
        srpm["in_buildroot_of_srpm_id_req"] = set()
        srpm["in_buildroot_of_srpm_id_dep"] = set()
        srpm["in_buildroot_of_srpm_id_env"] = set()

        srpm["unwanted_completely_in_list_ids"] = set()
        srpm["unwanted_buildroot_in_list_ids"] = set()

        srpm["level"] = []

        # Level 0 is runtime
        srpm["level"].append({
            "all": srpm["in_workload_ids_all"],
            "req": srpm["in_workload_ids_req"],
            "dep": srpm["in_workload_ids_dep"],
            "env": srpm["in_workload_ids_env"],
        })

        # Level 1 and higher is buildroot
        for _ in range(level):
            srpm["level"].append({
                "all": set(),
                "req": set(),
                "dep": set(),
                "env": set()
            })

        return srpm

    def _analyze_view(self, view_conf, arch, views):
        view_conf_id = view_conf["id"]

        log(f"Analyzing view: {view_conf['name']} ({view_conf_id}) for {arch}")

        view_id = f"{view_conf_id}:{arch}"

        repo_id = view_conf["repository"]

        # Setting up the data buckets for this view
        view = {}

        view["id"] = view_id
        view["view_conf_id"] = view_conf_id
        view["arch"] = arch

        view["workload_ids"] = []
        view["pkgs"] = {}
        view["source_pkgs"] = {}

        # Workloads
        for workload_id, workload in self.data["workloads"].items():
            if workload["repo_id"] != repo_id:
                continue

            if workload["arch"] != arch:
                continue

            if not set(workload["labels"]) & set(view_conf["labels"]):
                continue

            view["workload_ids"].append(workload_id)

        log(f"  Includes {len(view['workload_ids'])} workloads.")

        # Packages
        log(f"  Processing packages from {len(view['workload_ids'])} workloads...")
        workload_counter = 0
        total_workloads = len(view['workload_ids'])

        for workload_id in view["workload_ids"]:
            workload_counter += 1
            if workload_counter % 50 == 0:
                log(f"    Progress: {workload_counter}/{total_workloads} workloads processed")

            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            # Packages in the environment
            for pkg_id in workload["pkg_env_ids"]:
                # Initialise
                if pkg_id not in view["pkgs"]:
                    pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                    view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch)

                # It's in this wokrload
                view["pkgs"][pkg_id]["in_workload_ids_all"].add(workload_id)

                # And in the environment
                view["pkgs"][pkg_id]["in_workload_ids_env"].add(workload_id)

                # Is it also required?
                if view["pkgs"][pkg_id]["name"] in workload_conf["packages"]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
                elif view["pkgs"][pkg_id]["name"] in workload_conf["arch_packages"][arch]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)

                # pkg_relations
                view["pkgs"][pkg_id]["required_by"].update(workload["pkg_relations"][pkg_id]["required_by"])
                view["pkgs"][pkg_id]["recommended_by"].update(workload["pkg_relations"][pkg_id]["recommended_by"])
                view["pkgs"][pkg_id]["suggested_by"].update(workload["pkg_relations"][pkg_id]["suggested_by"])
                view["pkgs"][pkg_id]["supplements"].update(workload["pkg_relations"][pkg_id]["supplements"])

            # Packages added by this workload (required or dependency)
            for pkg_id in workload["pkg_added_ids"]:
                # Initialise
                if pkg_id not in view["pkgs"]:
                    pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                    view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch)

                # It's in this wokrload
                view["pkgs"][pkg_id]["in_workload_ids_all"].add(workload_id)

                # Is it required?
                if view["pkgs"][pkg_id]["name"] in workload_conf["packages"]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
                elif view["pkgs"][pkg_id]["name"] in workload_conf["arch_packages"][arch]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)

                # Or a dependency?
                else:
                    view["pkgs"][pkg_id]["in_workload_ids_dep"].add(workload_id)

                # pkg_relations
                view["pkgs"][pkg_id]["required_by"].update(workload["pkg_relations"][pkg_id]["required_by"])
                view["pkgs"][pkg_id]["recommended_by"].update(workload["pkg_relations"][pkg_id]["recommended_by"])
                view["pkgs"][pkg_id]["suggested_by"].update(workload["pkg_relations"][pkg_id]["suggested_by"])
                view["pkgs"][pkg_id]["supplements"].update(workload["pkg_relations"][pkg_id]["supplements"])

            # And finally the non-existing, imaginary, package placeholders!
            for pkg_id in workload["pkg_placeholder_ids"]:
                # Initialise
                if pkg_id not in view["pkgs"]:
                    placeholder = workload_conf["package_placeholders"]["pkgs"][pkg_id_to_name(pkg_id)]
                    view["pkgs"][pkg_id] = self._init_view_pkg(placeholder, arch, placeholder=True)

                # It's in this wokrload
                view["pkgs"][pkg_id]["in_workload_ids_all"].add(workload_id)

                # Placeholders are by definition required
                view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)

            # ... including the SRPM placeholders
            for srpm_name in workload["srpm_placeholder_names"]:
                srpm_id = pkg_placeholder_name_to_nevr(srpm_name)

                # Initialise
                if srpm_id not in view["source_pkgs"]:
                    sourcerpm = f"{srpm_id}.src.rpm"
                    view["source_pkgs"][srpm_id] = self._init_view_srpm({"sourcerpm": sourcerpm, "source_name": srpm_name, "reponame": None})

                # It's a placeholder
                view["source_pkgs"][srpm_id]["placeholder"] = True

                # Build requires
                view["source_pkgs"][srpm_id]["placeholder_directly_required_pkg_names"] = workload_conf["package_placeholders"]["srpms"][srpm_name]["buildrequires"]

        # If this is an addon view, remove all packages that are already in the parent view
        if view_conf["type"] == "addon":
            base_view_conf_id = view_conf["base_view_id"]

            base_view_id = f"{base_view_conf_id}:{arch}"

            for base_view_pkg_id in views[base_view_id]["pkgs"]:
                if base_view_pkg_id in view["pkgs"]:
                    del view["pkgs"][base_view_pkg_id]

        # Done with packages!
        log(f"  Done processing workload packages. Total packages in view: {len(view['pkgs'])}")

        # But not with source packages, that's an entirely different story!
        log(f"  Building source package data from {len(view['pkgs'])} binary packages...")
        pkg_counter = 0
        total_pkgs = len(view["pkgs"])

        for pkg_id, pkg in view["pkgs"].items():
            pkg_counter += 1
            if pkg_counter % 1000 == 0:
                log(f"    Progress: {pkg_counter}/{total_pkgs} packages processed for source mapping")

            srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

            if srpm_id not in view["source_pkgs"]:
                view["source_pkgs"][srpm_id] = self._init_view_srpm(pkg)

            # Include some information from the RPM
            view["source_pkgs"][srpm_id]["pkg_ids"].add(pkg_id)

            view["source_pkgs"][srpm_id]["in_workload_ids_all"].update(pkg["in_workload_ids_all"])
            view["source_pkgs"][srpm_id]["in_workload_ids_req"].update(pkg["in_workload_ids_req"])
            view["source_pkgs"][srpm_id]["in_workload_ids_dep"].update(pkg["in_workload_ids_dep"])
            view["source_pkgs"][srpm_id]["in_workload_ids_env"].update(pkg["in_workload_ids_env"])

        log(f"  Includes {len(view['source_pkgs'])} source packages.")

        log("  DONE!")
        log("")

        return view

    def _analyze_views(self):
        log("")
        log("=====  Starting View Analysis =====")
        log("")

        views = {}

        # First, analyse the standard views
        log("Phase 1: Analyzing standard (compose) views...")
        compose_view_count = sum(1 for v in self.configs["views"].values() if v["type"] == "compose")
        log(f"  Found {compose_view_count} compose views to process")

        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                log(f"  Processing compose view: {view_conf_id}")
                for arch in view_conf["architectures"]:
                    log(f"    Architecture: {arch}")
                    view = self._analyze_view(view_conf, arch, views)
                    view_id = view["id"]
                    views[view_id] = view
                    log(f"    ✅ Completed {view_id}")

        log("")
        log("Phase 2: Analyzing addon views...")
        addon_view_count = sum(1 for v in self.configs["views"].values() if v["type"] == "addon")
        log(f"  Found {addon_view_count} addon views to process")

        # Second, analyse the addon views
        # This is important as they need the standard views already available
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "addon":
                log(f"  Processing addon view: {view_conf_id}")
                base_view_conf_id = view_conf["base_view_id"]
                base_view_conf = self.configs["views"][base_view_conf_id]

                for arch in set(view_conf["architectures"]) & set(base_view_conf["architectures"]):
                    log(f"    Architecture: {arch}")
                    view = self._analyze_view(view_conf, arch, views)
                    view_id = view["id"]
                    views[view_id] = view
                    log(f"    ✅ Completed {view_id}")

        log("")
        log(f"View analysis complete! Total views created: {len(views)}")
        log("")

        self.data["views"] = views

    def _populate_buildroot_with_view_srpms(self, view_conf, arch):
        view_conf_id = view_conf["id"]

        log(f"Initialising buildroot packages of: {view_conf['name']} ({view_conf_id}) for {arch}")

        view_id = f"{view_conf_id}:{arch}"
        view = self.data["views"][view_id]
        repo_id = view_conf["repository"]

        # Initialise the srpms section
        if repo_id not in self.data["buildroot"]["srpms"]:
            self.data["buildroot"]["srpms"][repo_id] = {}

        if arch not in self.data["buildroot"]["srpms"][repo_id]:
            self.data["buildroot"]["srpms"][repo_id][arch] = {}

        # Initialise each srpm
        for srpm_id, srpm in view["source_pkgs"].items():

            if srpm["placeholder"]:
                directly_required_pkg_names = srpm["placeholder_directly_required_pkg_names"]

            else:
                # This is the same set in both koji_srpms and srpms
                directly_required_pkg_names = set()

                # Do I need to extract the build dependencies from koji root_logs?
                # Then also save the srpms in the koji_srpm section
                if view_conf["buildroot_strategy"] == "root_logs":
                    srpm_reponame = srpm["reponame"]
                    koji_api_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_api_url"]
                    koji_files_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_files_url"]
                    koji_id = url_to_id(koji_api_url)

                    # Initialise the koji_srpms section
                    if koji_id not in self.data["buildroot"]["koji_srpms"]:
                        # SRPMs
                        self.data["buildroot"]["koji_srpms"][koji_id] = {}
                        # URLs
                        self.data["buildroot"]["koji_urls"][koji_id] = {}
                        self.data["buildroot"]["koji_urls"][koji_id]["api"] = koji_api_url
                        self.data["buildroot"]["koji_urls"][koji_id]["files"] = koji_files_url

                    if arch not in self.data["buildroot"]["koji_srpms"][koji_id]:
                        self.data["buildroot"]["koji_srpms"][koji_id][arch] = {}

                    # Initialise srpms in the koji_srpms section
                    if srpm_id not in self.data["buildroot"]["koji_srpms"][koji_id][arch]:
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id] = {}
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["id"] = srpm_id
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                    else:
                        directly_required_pkg_names = self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"]

            # Initialise srpms in the srpms section
            if srpm_id not in self.data["buildroot"]["srpms"][repo_id][arch]:
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id] = {}
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["id"] = srpm_id
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_relations"] = {}
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_env_ids"] = set()
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_added_ids"] = set()
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"] = {}
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["non_existing_pkgs"] = set()
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["message"] = ""
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["succeeded"] = False
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = False
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["processed"] = False

        log("  DONE!")
        log("")

    def _resolve_srpms_using_root_logs_parallel(self, pass_counter):
        """
        This function is idempotent!
        """
        log(f"== Resolving SRPMs using root logs - pass {pass_counter} (PARALLEL) ========")

        # Collect work items (skip cached entries)
        work_items = []
        total_srpms_to_resolve = 0

        for koji_id in self.data["buildroot"]["koji_srpms"]:
            koji_urls = self.data["buildroot"]["koji_urls"][koji_id]

            # If the cache is empty, initialise it
            if koji_id not in self.cache["root_log_deps"]["current"]:
                self.cache["root_log_deps"]["current"][koji_id] = {}
            if koji_id not in self.cache["root_log_deps"]["next"]:
                self.cache["root_log_deps"]["next"][koji_id] = {}

            for arch in self.data["buildroot"]["koji_srpms"][koji_id]:

                # If the cache is empty, initialise it
                if arch not in self.cache["root_log_deps"]["current"][koji_id]:
                    self.cache["root_log_deps"]["current"][koji_id][arch] = {}
                if arch not in self.cache["root_log_deps"]["next"][koji_id]:
                    self.cache["root_log_deps"]["next"][koji_id][arch] = {}

                for srpm_id, srpm in self.data["buildroot"]["koji_srpms"][koji_id][arch].items():
                    total_srpms_to_resolve += 1

                    # Skip if already processed or cached
                    if srpm["directly_required_pkg_names"]:
                        log(f"  Skipping {srpm_id} {arch} (already done before)")
                        continue

                    if srpm_id in self.cache["root_log_deps"]["current"][koji_id][arch]:
                        log(f"  Using Cache for {srpm_id} {arch}!")
                        directly_required_pkg_names = self.cache["root_log_deps"]["current"][koji_id][arch][srpm_id]
                        self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id] = directly_required_pkg_names
                        srpm["directly_required_pkg_names"].update(directly_required_pkg_names)
                        continue

                    if srpm_id in self.cache["root_log_deps"]["next"][koji_id][arch]:
                        log(f"  Using Cache for {srpm_id} {arch}!")
                        directly_required_pkg_names = self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id]
                        srpm["directly_required_pkg_names"].update(directly_required_pkg_names)
                        continue

                    # Add to work queue
                    work_items.append({
                        "koji_id": koji_id,
                        "koji_api_url": koji_urls["api"],
                        "koji_files_url": koji_urls["files"],
                        "srpm_id": srpm_id,
                        "arch": arch,
                        "dev_buildroot": self.settings.get("dev_buildroot", False),
                    })

        if not work_items:
            log("All SRPMs already cached or processed!")
            return

        log(f"Processing {len(work_items)} SRPMs in parallel (out of {total_srpms_to_resolve} total)")

        # Process in parallel using ProcessPoolExecutor
        max_workers = min(self.settings["parallel_max"], len(work_items))

        # Track failures for reporting
        failed_downloads = []  # Root logs that failed to download
        zero_deps = []  # Packages with zero dependencies (suspicious)
        low_deps = []  # Packages with very few deps (possibly truncated logs)

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            # Submit all jobs
            future_to_item = {
                executor.submit(process_single_srpm_root_log, item): item
                for item in work_items
            }

            # Collect results as they complete
            completed_count = 0
            total_count = len(work_items)

            for future in as_completed(future_to_item):
                completed_count += 1
                work_item = future_to_item[future]

                try:
                    result = future.result()
                    self._apply_srpm_result(work_item, result)

                    if result['error']:
                        log(f"[ Buildroot - pass {pass_counter} - {completed_count} of {total_count} ] "
                            f"Failed {result['srpm_id']} {result['arch']}: {result['error']}")
                        failed_downloads.append({
                            'srpm_id': result['srpm_id'],
                            'arch': result['arch'],
                            'error': result['error']
                        })
                    else:
                        # Log completion with warning if present
                        deps_count = len(result['deps'])
                        warning_msg = result.get('warning', '')
                        if warning_msg:
                            log(f"[ Buildroot - pass {pass_counter} - {completed_count} of {total_count} ] "
                                f"⚠️  {result['srpm_id']} {result['arch']} - {deps_count} deps - {warning_msg}")
                        else:
                            log(f"[ Buildroot - pass {pass_counter} - {completed_count} of {total_count} ] "
                                f"Completed {result['srpm_id']} {result['arch']} - found {deps_count} deps")

                        # Detect suspicious results
                        if deps_count == 0:
                            zero_deps.append({
                                'srpm_id': result['srpm_id'],
                                'arch': result['arch']
                            })
                        elif deps_count < 3:
                            low_deps.append({
                                'srpm_id': result['srpm_id'],
                                'arch': result['arch'],
                                'deps': result['deps']
                            })

                except Exception as e:
                    log(f"Failed to process {work_item['srpm_id']}: {e}")
                    # Apply empty result for failed processing
                    error_result = {
                        "srpm_id": work_item["srpm_id"],
                        "arch": work_item["arch"],
                        "deps": [],
                        "error": str(e),
                    }
                    self._apply_srpm_result(work_item, error_result)
                    failed_downloads.append({
                        'srpm_id': work_item["srpm_id"],
                        'arch': work_item["arch"],
                        'error': str(e)
                    })

        # Save updated cache
        dump_data(self.settings["root_log_deps_cache_path"], self.cache["root_log_deps"]["next"])

        # Report summary of issues
        log("")
        log("=" * 80)
        log("ROOT LOG PROCESSING SUMMARY")
        log("=" * 80)

        if failed_downloads:
            log(f"⚠️  FAILED DOWNLOADS: {len(failed_downloads)} root.log files failed to download/parse")
            log("")
            for item in failed_downloads[:20]:  # Show first 20
                log(f"  ❌ {item['srpm_id']} ({item['arch']})")
                log(f"     Error: {item['error'][:100]}")
            if len(failed_downloads) > 20:
                log(f"  ... and {len(failed_downloads) - 20} more")

        if zero_deps:
            log("")
            log(f"⚠️  ZERO DEPENDENCIES: {len(zero_deps)} packages reported 0 build dependencies")
            log("   (This is unusual - most packages have at least bash, gcc, etc.)")
            log("")
            for item in zero_deps[:20]:
                log(f"  ⚠️  {item['srpm_id']} ({item['arch']})")
            if len(zero_deps) > 20:
                log(f"  ... and {len(zero_deps) - 20} more")

        if low_deps:
            log("")
            log(f"⚠️  LOW DEPENDENCIES: {len(low_deps)} packages have very few dependencies (<3)")
            log("   (Possibly truncated or corrupted root.log files)")
            log("")
            for item in low_deps[:10]:
                log(f"  ⚠️  {item['srpm_id']} ({item['arch']}): {item['deps']}")
            if len(low_deps) > 10:
                log(f"  ... and {len(low_deps) - 10} more")

        if not failed_downloads and not zero_deps and not low_deps:
            log("✅ All root.log files processed successfully with reasonable dependency counts")

        log("=" * 80)
        log("")

        log("")
        log("  DONE!")
        log("")

    def _apply_srpm_result(self, work_item, result):
        """Apply worker result back to main data structures"""
        koji_id = work_item["koji_id"]
        arch = work_item["arch"]
        srpm_id = work_item["srpm_id"]
        deps = result["deps"]

        # Update cache
        self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id] = deps

        # Update main data
        # Here it's important to add the packages to the already initiated
        # set, because its reference is shared between the koji_srpms and the srpm sections
        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"].update(deps)

    def _analyze_build_groups(self):

        log("")
        log("Analyzing build groups...")
        log("")

        # Need to analyse build groups for all repo_ids
        # and arches of buildroot["srpms"]
        for repo_id in self.data["buildroot"]["srpms"]:
            self.data["buildroot"]["build_groups"][repo_id] = {}

            for arch in self.data["buildroot"]["srpms"][repo_id]:

                generated_id = f"CR-buildroot-base-env-{repo_id}-{arch}"

                # Using the _analyze_env function!
                # So I need to reconstruct a fake env_conf
                fake_env_conf = {}
                fake_env_conf["id"] = generated_id
                fake_env_conf["options"] = []
                if self.configs["repos"][repo_id]["source"]["base_buildroot_override"]:
                    fake_env_conf["packages"] = self.configs["repos"][repo_id]["source"]["base_buildroot_override"]
                    fake_env_conf["groups"] = []
                else:
                    fake_env_conf["packages"] = []
                    fake_env_conf["groups"] = ["build"]
                fake_env_conf["arch_packages"] = {}
                fake_env_conf["arch_packages"][arch] = []

                log(f"Resolving build group: {repo_id} {arch}")
                repo = self.configs["repos"][repo_id]
                fake_env = self._analyze_env(fake_env_conf, repo, arch)

                # If this fails, the buildroot can't be resolved.
                # Fail the entire content resolver build!
                if not fake_env["succeeded"]:
                    raise BuildGroupAnalysisError

                self.data["buildroot"]["build_groups"][repo_id][arch] = fake_env
                self.data["buildroot"]["build_groups"][repo_id][arch]["generated_id"] = generated_id

        log("")
        log("  DONE!")
        log("")


    def _expand_buildroot_srpms(self):
        # This function is idempotent!
        # That means it can be run many times without affecting the old results.

        log("Expanding the SRPM set...")

        counter = 0

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                top_lvl_srpm_ids = set(self.data["buildroot"]["srpms"][repo_id][arch])
                for top_lvl_srpm_id in top_lvl_srpm_ids:
                    top_lvl_srpm = self.data["buildroot"]["srpms"][repo_id][arch][top_lvl_srpm_id]

                    for pkg_id in top_lvl_srpm["pkg_relations"]:
                        srpm_id = self.data["pkgs"][repo_id][arch][pkg_id]["sourcerpm"].rsplit(".src.rpm")[0]

                        if srpm_id in self.data["buildroot"]["srpms"][repo_id][arch]:
                            continue

                        # Adding a new one!
                        counter += 1

                        srpm_reponame = self.data["pkgs"][repo_id][arch][pkg_id]["reponame"]

                        # This is the same set in both koji_srpms and srpms
                        directly_required_pkg_names = set()

                        koji_api_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_api_url"]
                        koji_files_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_files_url"]
                        koji_id = url_to_id(koji_api_url)

                        # Initialise the srpm in the koji_srpms section
                        if srpm_id not in self.data["buildroot"]["koji_srpms"][koji_id][arch]:
                            self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id] = {}
                            self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["id"] = srpm_id
                            self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                        else:
                            directly_required_pkg_names = self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"]

                        # Initialise the srpm in the srpms section
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id] = {}
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["id"] = srpm_id
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_relations"] = {}
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_env_ids"] = set()
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_added_ids"] = set()
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"] = {}
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["non_existing_pkgs"] = set()
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["message"] = ""
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["succeeded"] = False
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = False
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["processed"] = False

        log(f"  Found {counter} new SRPMs!")
        log("  DONE!")
        log("")

        return counter


    def _analyze_srpm_buildroots(self, pass_counter):
        # This function is idempotent!
        # That means it can be run many times without affecting the old results.

        log("")
        log("Analyzing SRPM buildroots...")
        log("")

        # Initialise things for the workload resolver
        self._reset_workload_processing_queue()
        fake_workload_results = {}

        # Prepare a counter for the log
        total_srpms_to_resolve = 0
        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():
                    if srpm["processed"]:
                        continue
                    total_srpms_to_resolve += 1
        srpms_to_resolve_counter = 0

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():

                    if srpm["queued"] or srpm["processed"]:
                        continue

                    # Using the _analyze_workload function!
                    # So I need to reconstruct a fake workload_conf and a fake env_conf
                    fake_workload_conf = {}
                    fake_workload_conf["labels"] = []
                    fake_workload_conf["id"] = srpm_id
                    fake_workload_conf["options"] = []
                    fake_workload_conf["packages"] = srpm["directly_required_pkg_names"]
                    fake_workload_conf["groups"] = []
                    fake_workload_conf["package_placeholders"] = {}
                    fake_workload_conf["package_placeholders"]["pkgs"] = {}
                    fake_workload_conf["package_placeholders"]["srpms"] = {}
                    fake_workload_conf["arch_packages"] = {}
                    fake_workload_conf["arch_packages"][arch] = []

                    fake_env_conf = {}
                    fake_env_conf["labels"] = []
                    fake_env_conf["id"] = self.data["buildroot"]["build_groups"][repo_id][arch]["generated_id"]
                    fake_env_conf["packages"] = ["bash"] # This just needs to pass the "if len(packages)" test as True
                    fake_env_conf["arch_packages"] = {}
                    fake_env_conf["arch_packages"][arch] = []

                    srpms_to_resolve_counter += 1
                    repo = self.configs["repos"][repo_id]
                    self._queue_workload_processing(fake_workload_conf, fake_env_conf, repo, arch)

                    # Save the buildroot data
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = True
        log(f"fake_workload_results -> {fake_workload_results}")

        asyncio.run(self._analyze_workloads_async(fake_workload_results))

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():

                    if srpm["processed"]:
                        continue

                    fake_workload_id = f"{srpm_id}:{self.data['buildroot']['build_groups'][repo_id][arch]['generated_id']}:{repo_id}:{arch}"

                    fake_workload = fake_workload_results[fake_workload_id]

                    # Save the buildroot data
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["succeeded"] = fake_workload["succeeded"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_relations"] = fake_workload["pkg_relations"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_env_ids"] = fake_workload["pkg_env_ids"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_added_ids"] = fake_workload["pkg_added_ids"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"] = fake_workload["errors"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["warnings"] = fake_workload["warnings"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["processed"] = True

        log("")
        log("  DONE!")
        log("")


    def _analyze_buildroot(self):

        self._global_performance_hack_run_recommends_queries = False

        self._record_metric("started _analyze_buildroot()")

        self.data["buildroot"] = {}
        self.data["buildroot"]["koji_srpms"] = {}
        self.data["buildroot"]["koji_urls"] = {}
        self.data["buildroot"]["srpms"] = {}
        self.data["buildroot"]["build_groups"] = {}

        # Currently, only "compose" view types are supported.
        # The "addon" type is not.

        # Get SRPMs from views
        #
        # This populates:
        #   data["buildroot"]["koji_srpms"]...
        # and also initiates:
        #   data["buildroot"]["srpms"]...
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                if view_conf["buildroot_strategy"] == "root_logs":
                    for arch in view_conf["architectures"]:
                        self._populate_buildroot_with_view_srpms(view_conf, arch)

        # Time to resolve the build groups!
        # This initialises and populates:
        #   buildroot["build_groups"]
        self._analyze_build_groups()

        pass_counter = 0
        while True:
            pass_counter += 1

            self._record_metric(f"  started pass {pass_counter}:")

            log("")
            log(f"== Buildroot resolution - pass {pass_counter} ========")
            log("")
            log("")
            # Get the directly_required_pkg_names from koji root logs
            # Adds stuff to existing:
            #   data["buildroot"]["koji_srpms"]...
            # ... which also updates:
            #   data["buildroot"]["srpms"]...
            # ... because it's interlinked.
            self._resolve_srpms_using_root_logs_parallel(pass_counter)

            self._record_metric("    finished _resolve_srpms_using_root_logs")

            # And now resolving the actual buildroot
            self._analyze_srpm_buildroots(pass_counter)

            self._record_metric("    finished _analyze_srpm_buildroots")

            # Resolving dependencies could have added new SRPMs into the mix that also
            # need their buildroots resolved! So let's find out if there are any
            new_srpms_count = self._expand_buildroot_srpms()

            self._record_metric(f"    finished with new_srpms_count == {new_srpms_count}")

            if not new_srpms_count:
                log("")
                log("All passes completed!")
                log("")
                break

    def _add_missing_levels_to_pkg_or_srpm(self, pkg_or_srpm, level):
        """Add missing level entries to reach the target level."""
        pkg_current_max_level = len(pkg_or_srpm["level"]) - 1
        for _ in range(level - pkg_current_max_level):
            pkg_or_srpm["level"].append({
                "all": set(),
                "req": set(),
                "dep": set(),
                "env": set()
            })


    def _add_buildroot_to_view(self, view_conf, arch):

        view_conf_id = view_conf["id"]

        view_id = f"{view_conf_id}:{arch}"

        repo_id = view_conf["repository"]

        view = self.data["views"][view_id]

        log("")
        log(f"Adding buildroot to view {view_id}...")

        # Starting with all SRPMs in this view
        srpm_ids_to_process = set(view["source_pkgs"])

        # Starting on level 1, the first buildroot level
        # (it's 0 because it gets incremented to 1 immediately after the loop starts)
        level = 0

        while True:
            level += 1
            added_pkg_ids = set()

            log(f"  Pass {level}...")

            # This is similar to adding workloads in _analyze_view()
            for buildroot_srpm_id in srpm_ids_to_process:
                buildroot_srpm = self.data["buildroot"]["srpms"][repo_id][arch][buildroot_srpm_id]

                # Packages in the base buildroot (which would be the environment in workloads)
                for pkg_id in buildroot_srpm["pkg_env_ids"]:
                    added_pkg_ids.add(pkg_id)

                    # Initialise
                    if pkg_id not in view["pkgs"]:
                        pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                        view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch, level=level)

                    # Add missing levels to the pkg
                    self._add_missing_levels_to_pkg_or_srpm(view["pkgs"][pkg_id], level)

                    # It's in this buildroot
                    view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_all"].add(buildroot_srpm_id)
                    view["pkgs"][pkg_id]["level"][level]["all"].add(buildroot_srpm_id)

                    # And in the base buildroot specifically
                    view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_env"].add(buildroot_srpm_id)
                    view["pkgs"][pkg_id]["level"][level]["env"].add(buildroot_srpm_id)

                    # Is it also required?
                    if view["pkgs"][pkg_id]["name"] in buildroot_srpm["directly_required_pkg_names"]:
                        view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_req"].add(buildroot_srpm_id)
                        view["pkgs"][pkg_id]["level"][level]["req"].add(buildroot_srpm_id)

                    # pkg_relations
                    view["pkgs"][pkg_id]["required_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["required_by"])
                    view["pkgs"][pkg_id]["recommended_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["recommended_by"])
                    view["pkgs"][pkg_id]["suggested_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["suggested_by"])
                    view["pkgs"][pkg_id]["supplements"].update(buildroot_srpm["pkg_relations"][pkg_id]["supplements"])

                # Packages needed on top of the base buildroot (required or dependency)
                for pkg_id in buildroot_srpm["pkg_added_ids"]:
                    added_pkg_ids.add(pkg_id)

                    # Initialise
                    if pkg_id not in view["pkgs"]:
                        pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                        view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch, level=level)

                    # Add missing levels to the pkg
                    self._add_missing_levels_to_pkg_or_srpm(view["pkgs"][pkg_id], level)

                    # It's in this buildroot
                    view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_all"].add(buildroot_srpm_id)
                    view["pkgs"][pkg_id]["level"][level]["all"].add(buildroot_srpm_id)

                    # Is it also required?
                    if view["pkgs"][pkg_id]["name"] in buildroot_srpm["directly_required_pkg_names"]:
                        view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_req"].add(buildroot_srpm_id)
                        view["pkgs"][pkg_id]["level"][level]["req"].add(buildroot_srpm_id)

                    # Or a dependency?
                    else:
                        view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_dep"].add(buildroot_srpm_id)
                        view["pkgs"][pkg_id]["level"][level]["dep"].add(buildroot_srpm_id)

                    # pkg_relations
                    view["pkgs"][pkg_id]["required_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["required_by"])
                    view["pkgs"][pkg_id]["recommended_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["recommended_by"])
                    view["pkgs"][pkg_id]["suggested_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["suggested_by"])
                    view["pkgs"][pkg_id]["supplements"].update(buildroot_srpm["pkg_relations"][pkg_id]["supplements"])

            # Resetting the SRPMs, so only the new ones can be added
            srpm_ids_to_process = set()

            # SRPMs
            for pkg_id in added_pkg_ids:
                pkg = view["pkgs"][pkg_id]
                srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

                # Initialise
                if srpm_id not in view["source_pkgs"]:
                    view["source_pkgs"][srpm_id] = self._init_view_srpm(pkg, level=level)
                    srpm_ids_to_process.add(srpm_id)

                # Add missing levels to the pkg
                self._add_missing_levels_to_pkg_or_srpm(view["source_pkgs"][srpm_id], level)

                view["source_pkgs"][srpm_id]["pkg_ids"].add(pkg_id)

                # Include some information from the RPM
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_all"].update(pkg["in_buildroot_of_srpm_id_all"])
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_req"].update(pkg["in_buildroot_of_srpm_id_req"])
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_dep"].update(pkg["in_buildroot_of_srpm_id_dep"])
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_env"].update(pkg["in_buildroot_of_srpm_id_env"])
                view["source_pkgs"][srpm_id]["level"][level]["all"].update(pkg["level"][level]["all"])
                view["source_pkgs"][srpm_id]["level"][level]["req"].update(pkg["level"][level]["req"])
                view["source_pkgs"][srpm_id]["level"][level]["dep"].update(pkg["level"][level]["dep"])
                view["source_pkgs"][srpm_id]["level"][level]["env"].update(pkg["level"][level]["env"])

            log(f"    added {len(added_pkg_ids)} RPMs")
            log(f"    added {len(srpm_ids_to_process)} SRPMs")

            # More iterations needed?
            if not srpm_ids_to_process:
                log("  All passes completed!")
                log("")
                break

    def _add_buildroot_to_views(self):

        log("")
        log("Adding Buildroot to views...")
        log("")

        # First, the standard views
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                if view_conf["buildroot_strategy"] == "root_logs":
                    for arch in view_conf["architectures"]:
                        self._add_buildroot_to_view(view_conf, arch)

        # And the addon is not supported now

        log("")
        log("  DONE!")
        log("")

    def _init_pkg_or_srpm_relations_fields(self, target_pkg, type=None):
        # I kept them all listed so they're easy to copy

        # Workload IDs
        target_pkg["in_workload_ids_all"] = set()
        target_pkg["in_workload_ids_req"] = set()
        target_pkg["in_workload_ids_dep"] = set()
        target_pkg["in_workload_ids_env"] = set()

        # Workload Conf IDs
        target_pkg["in_workload_conf_ids_all"] = set()
        target_pkg["in_workload_conf_ids_req"] = set()
        target_pkg["in_workload_conf_ids_dep"] = set()
        target_pkg["in_workload_conf_ids_env"] = set()

        # Buildroot SRPM IDs
        target_pkg["in_buildroot_of_srpm_id_all"] = set()
        target_pkg["in_buildroot_of_srpm_id_req"] = set()
        target_pkg["in_buildroot_of_srpm_id_dep"] = set()
        target_pkg["in_buildroot_of_srpm_id_env"] = set()

        # Buildroot SRPM Names
        target_pkg["in_buildroot_of_srpm_name_all"] = {}  # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_req"] = {}  # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_dep"] = {}  # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_env"] = {}  # of set() of srpm_ids

        # Unwanted
        target_pkg["unwanted_completely_in_list_ids"] = set()
        target_pkg["unwanted_buildroot_in_list_ids"] = set()

        # Level number
        target_pkg["level_number"] = 999

        # Levels
        target_pkg["level"] = []

        # Maintainer recommendation
        target_pkg["maintainer_recommendation"] = {}
        target_pkg["maintainer_recommendation_details"] = {}
        target_pkg["best_maintainers"] = set()

        if type == "rpm":

            # Dependency of RPM NEVRs
            target_pkg["dependency_of_pkg_nevrs"] = set()
            target_pkg["hard_dependency_of_pkg_nevrs"] = set()
            target_pkg["weak_dependency_of_pkg_nevrs"] = set()
            target_pkg["reverse_weak_dependency_of_pkg_nevrs"] = set()

            # Dependency of RPM Names
            target_pkg["dependency_of_pkg_names"] = {} # of set() of nevrs
            target_pkg["hard_dependency_of_pkg_names"] = {} # of set() of nevrs
            target_pkg["weak_dependency_of_pkg_names"] = {} # if set() of nevrs
            target_pkg["reverse_weak_dependency_of_pkg_names"] = {} # if set() of nevrs

    def _populate_pkg_or_srpm_relations_fields(self, target_pkg, source_pkg, type=None, view=None):

        # source_pkg is the arch-specific binary package
        # target_pkg is a representation of that pages for all arches
        #
        # This function adds information from the arch-specific package to the general one.
        # It gets called for all the arches.
        #

        if type == "rpm" and not view:
            raise ValueError("This function requires a view when using type = 'rpm'!")

        # Unwanted
        target_pkg["unwanted_completely_in_list_ids"].update(source_pkg["unwanted_completely_in_list_ids"])
        target_pkg["unwanted_buildroot_in_list_ids"].update(source_pkg["unwanted_buildroot_in_list_ids"])

        # Dependency relationships
        for list_type in ["all", "req", "dep", "env"]:
            target_pkg[f"in_workload_ids_{list_type}"].update(source_pkg[f"in_workload_ids_{list_type}"])

            target_pkg[f"in_buildroot_of_srpm_id_{list_type}"].update(source_pkg[f"in_buildroot_of_srpm_id_{list_type}"])

            for workload_id in source_pkg[f"in_workload_ids_{list_type}"]:
                workload_conf_id = workload_id_to_conf_id(workload_id)
                target_pkg[f"in_workload_conf_ids_{list_type}"].add(workload_conf_id)

            for srpm_id in source_pkg[f"in_buildroot_of_srpm_id_{list_type}"]:
                srpm_name = pkg_id_to_name(srpm_id)

                if srpm_name not in target_pkg[f"in_buildroot_of_srpm_name_{list_type}"]:
                    target_pkg[f"in_buildroot_of_srpm_name_{list_type}"][srpm_name] = set()

                target_pkg[f"in_buildroot_of_srpm_name_{list_type}"][srpm_name].add(srpm_id)

        # Level number
        level_number = 0
        for level in source_pkg["level"]:
            if level["all"]:
                if level_number < target_pkg["level_number"]:
                    target_pkg["level_number"] = level_number
            level_number += 1

        # All the levels!
        level = 0
        for level_data in source_pkg["level"]:
            # 'level' is the number
            # 'level_data' is the ["all"][workload_id] or ["all"][srpm_id] or
            #                     ["req"][workload_id] or ["req"][srpm_id] or
            #                     ["dep"][workload_id] or ["dep"][srpm_id] or
            #                     ["env"][workload_id] or ["env"][srpm_id]

            # If I could do 'if level in target_pkg["level"]' I'd do that instead...
            # But it's a list, so have to do this instead
            if len(target_pkg["level"]) == level:
                target_pkg["level"].append(dict())

            for level_scope, those_ids in level_data.items():
                # 'level_scope' is "all" or "req" etc.
                # 'those_ids' is a list of srpm_ids or workload_ids

                if level_scope not in target_pkg["level"][level]:
                    target_pkg["level"][level][level_scope] = set()

                target_pkg["level"][level][level_scope].update(those_ids)

            level += 1

        if type == "rpm":
            # Hard dependency of
            for pkg_id in source_pkg["required_by"]:
                pkg_name = pkg_id_to_name(pkg_id)

                # This only happens in addon views, and only rarely.
                # Basically means that a package in the addon view is required
                # by a package in the base view.
                # Doesn't make sense?
                # Think of 'glibc-all-langpacks' being in the addon,
                # while the proper langpacks along with 'glibc' are in the base view.
                #
                # In that case, 'glibc' is not in the addon, but 'glibc-all-langpacks'
                # requires it.
                #
                # I'm not implementing it now, as it's such a corner case.
                # So just skip it. All the data will remain correct,
                # it's just the 'glibc-all-langpacks' page won't show
                # "required by 'glibc'" that's all.
                if pkg_id not in view["pkgs"]:
                    view_conf_id = view["view_conf_id"]
                    view_conf = self.configs["views"][view_conf_id]
                    if view_conf["type"] == "addon":
                        continue

                pkg = view["pkgs"][pkg_id]
                pkg_nevr = f"{pkg['name']}-{pkg['evr']}"
                target_pkg["hard_dependency_of_pkg_nevrs"].add(pkg_nevr)

                if pkg_name not in target_pkg["hard_dependency_of_pkg_names"]:
                    target_pkg["hard_dependency_of_pkg_names"][pkg_name] = set()
                target_pkg["hard_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)

            # Weak dependency of
            for list_type in ["recommended", "suggested"]:
                for pkg_id in source_pkg[f"{list_type}_by"]:
                    pkg_name = pkg_id_to_name(pkg_id)

                    # This only happens in addon views, and only rarely.
                    # (see the long comment above)
                    if pkg_id not in view["pkgs"]:
                        view_conf_id = view["view_conf_id"]
                        view_conf = self.configs["views"][view_conf_id]
                        if view_conf["type"] == "addon":
                            continue

                    pkg = view["pkgs"][pkg_id]
                    pkg_nevr = f"{pkg['name']}-{pkg['evr']}"
                    target_pkg["weak_dependency_of_pkg_nevrs"].add(pkg_nevr)

                    if pkg_name not in target_pkg["weak_dependency_of_pkg_names"]:
                        target_pkg["weak_dependency_of_pkg_names"][pkg_name] = set()
                    target_pkg["weak_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)

            # Reverse weak dependency of (supplements)
            for pkg_id in source_pkg["supplements"]:
                pkg_name = pkg_id_to_name(pkg_id)

                # This only happens in addon views, and only rarely.
                # (see the long comment above)
                if pkg_id not in view["pkgs"]:
                    view_conf_id = view["view_conf_id"]
                    view_conf = self.configs["views"][view_conf_id]
                    if view_conf["type"] == "addon":
                        continue

                pkg = view["pkgs"][pkg_id]
                pkg_nevr = f"{pkg['name']}-{pkg['evr']}"
                target_pkg["reverse_weak_dependency_of_pkg_nevrs"].add(pkg_nevr)

                if pkg_name not in target_pkg["reverse_weak_dependency_of_pkg_names"]:
                    target_pkg["reverse_weak_dependency_of_pkg_names"][pkg_name] = set()
                target_pkg["reverse_weak_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)

            # All types of dependency
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["hard_dependency_of_pkg_nevrs"])
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["weak_dependency_of_pkg_nevrs"])
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["reverse_weak_dependency_of_pkg_nevrs"])

            for pkg_name, pkg_nevrs in target_pkg["hard_dependency_of_pkg_names"].items():
                if pkg_name not in target_pkg["dependency_of_pkg_names"]:
                    target_pkg["dependency_of_pkg_names"][pkg_name] = set()

                target_pkg["dependency_of_pkg_names"][pkg_name].update(pkg_nevrs)

            for pkg_name, pkg_nevrs in target_pkg["weak_dependency_of_pkg_names"].items():
                if pkg_name not in target_pkg["dependency_of_pkg_names"]:
                    target_pkg["dependency_of_pkg_names"][pkg_name] = set()

                target_pkg["dependency_of_pkg_names"][pkg_name].update(pkg_nevrs)

            for pkg_name, pkg_nevrs in target_pkg["reverse_weak_dependency_of_pkg_names"].items():
                if pkg_name not in target_pkg["dependency_of_pkg_names"]:
                    target_pkg["dependency_of_pkg_names"][pkg_name] = set()

                target_pkg["dependency_of_pkg_names"][pkg_name].update(pkg_nevrs)

        # TODO: add the levels

    def _generate_views_all_arches(self):

        views_all_arches = {}

        for view_conf_id, view_conf in self.configs["views"].items():

            #if view_conf["type"] == "compose":
            if True:
                repo_id = view_conf["repository"]

                view_all_arches = {}

                view_all_arches["id"] = view_conf_id
                view_all_arches["has_buildroot"] = False

                if view_conf["type"] == "compose":
                    if view_conf["buildroot_strategy"] == "root_logs":
                        view_all_arches["has_buildroot"] = True
                else:
                    view_all_arches["has_buildroot"] = False

                view_all_arches["everything_succeeded"] = True
                view_all_arches["no_warnings"] = True

                view_all_arches["workloads"] = {}

                view_all_arches["pkgs_by_name"] = {}
                view_all_arches["pkgs_by_nevr"] = {}

                view_all_arches["source_pkgs_by_name"] = {}

                view_all_arches["numbers"] = {}
                view_all_arches["numbers"]["pkgs"] = {}
                view_all_arches["numbers"]["pkgs"]["runtime"] = 0
                view_all_arches["numbers"]["pkgs"]["env"] = 0
                view_all_arches["numbers"]["pkgs"]["req"] = 0
                view_all_arches["numbers"]["pkgs"]["dep"] = 0
                view_all_arches["numbers"]["pkgs"]["build"] = 0
                view_all_arches["numbers"]["pkgs"]["build_base"] = 0
                view_all_arches["numbers"]["pkgs"]["build_level_1"] = 0
                view_all_arches["numbers"]["pkgs"]["build_level_2_plus"] = 0
                view_all_arches["numbers"]["srpms"] = {}
                view_all_arches["numbers"]["srpms"]["runtime"] = 0
                view_all_arches["numbers"]["srpms"]["env"] = 0
                view_all_arches["numbers"]["srpms"]["req"] = 0
                view_all_arches["numbers"]["srpms"]["dep"] = 0
                view_all_arches["numbers"]["srpms"]["build"] = 0
                view_all_arches["numbers"]["srpms"]["build_base"] = 0
                view_all_arches["numbers"]["srpms"]["build_level_1"] = 0
                view_all_arches["numbers"]["srpms"]["build_level_2_plus"] = 0

                for arch in view_conf["architectures"]:
                    view_id = f"{view_conf_id}:{arch}"
                    view = self.data["views"][view_id]

                    # Workloads
                    for workload_id in view["workload_ids"]:
                        workload = self.data["workloads"][workload_id]
                        workload_conf_id = workload["workload_conf_id"]
                        workload_conf = self.configs["workloads"][workload_conf_id]

                        if workload_conf_id not in view_all_arches["workloads"]:
                            view_all_arches["workloads"][workload_conf_id] = {}
                            view_all_arches["workloads"][workload_conf_id]["workload_conf_id"] = workload_conf_id
                            view_all_arches["workloads"][workload_conf_id]["name"] = workload_conf["name"]
                            view_all_arches["workloads"][workload_conf_id]["maintainer"] = workload_conf["maintainer"]
                            view_all_arches["workloads"][workload_conf_id]["succeeded"] = True
                            view_all_arches["workloads"][workload_conf_id]["no_warnings"] = True
                            # ...

                        if not workload["succeeded"]:
                            view_all_arches["workloads"][workload_conf_id]["succeeded"] = False
                            view_all_arches["everything_succeeded"] = False

                        if workload["warnings"]["message"]:
                            view_all_arches["workloads"][workload_conf_id]["no_warnings"] = False
                            view_all_arches["no_warnings"] = False

                    # Binary Packages
                    for package in view["pkgs"].values():
                        # Binary Packages by name
                        key = "pkgs_by_name"
                        identifier = package["name"]

                        # Init
                        if identifier not in view_all_arches[key]:
                            view_all_arches[key][identifier] = {}
                            view_all_arches[key][identifier]["name"] = package["name"]
                            view_all_arches[key][identifier]["placeholder"] = package["placeholder"]
                            view_all_arches[key][identifier]["source_name"] = package["source_name"]
                            view_all_arches[key][identifier]["nevrs"] = {}
                            view_all_arches[key][identifier]["arches"] = set()
                            view_all_arches[key][identifier]["highest_priority_reponames_per_arch"] = {}

                            self._init_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], type="rpm")

                        if package["nevr"] not in view_all_arches[key][identifier]["nevrs"]:
                            view_all_arches[key][identifier]["nevrs"][package["nevr"]] = set()
                        view_all_arches[key][identifier]["nevrs"][package["nevr"]].add(arch)

                        view_all_arches[key][identifier]["arches"].add(arch)

                        if arch not in view_all_arches[key][identifier]["highest_priority_reponames_per_arch"]:
                            view_all_arches[key][identifier]["highest_priority_reponames_per_arch"][arch] = set()
                        view_all_arches[key][identifier]["highest_priority_reponames_per_arch"][arch].update(package["highest_priority_reponames"])

                        self._populate_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], package, type="rpm", view=view)

                        # Binary Packages by nevr
                        key = "pkgs_by_nevr"
                        identifier = package["nevr"]

                        if identifier not in view_all_arches[key]:
                            view_all_arches[key][identifier] = {}
                            view_all_arches[key][identifier]["name"] = package["name"]
                            view_all_arches[key][identifier]["placeholder"] = package["placeholder"]
                            view_all_arches[key][identifier]["evr"] = package["evr"]
                            view_all_arches[key][identifier]["source_name"] = package["source_name"]
                            view_all_arches[key][identifier]["arches"] = set()
                            view_all_arches[key][identifier]["arches_arches"] = {}
                            view_all_arches[key][identifier]["reponame_per_arch"] = {}
                            view_all_arches[key][identifier]["highest_priority_reponames_per_arch"] = {}
                            view_all_arches[key][identifier]["category"] = None

                            self._init_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], type="rpm")

                        view_all_arches[key][identifier]["arches"].add(arch)
                        view_all_arches[key][identifier]["reponame_per_arch"][arch] = package["reponame"]
                        view_all_arches[key][identifier]["highest_priority_reponames_per_arch"][arch] = package["highest_priority_reponames"]

                        if arch not in view_all_arches[key][identifier]["arches_arches"]:
                            view_all_arches[key][identifier]["arches_arches"][arch] = set()
                        view_all_arches[key][identifier]["arches_arches"][arch].add(package["arch"])

                        self._populate_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], package, type="rpm", view=view)


                    # Source Packages
                    for package in view["source_pkgs"].values():
                        # Source Packages by name
                        key = "source_pkgs_by_name"
                        identifier = package["name"]

                        if identifier not in view_all_arches[key]:
                            view_all_arches[key][identifier] = {}
                            view_all_arches[key][identifier]["name"] = package["name"]
                            view_all_arches[key][identifier]["placeholder"] = package["placeholder"]
                            if view_all_arches["has_buildroot"]:
                                view_all_arches[key][identifier]["buildroot_succeeded"] = True
                                view_all_arches[key][identifier]["buildroot_no_warnings"] = True
                            view_all_arches[key][identifier]["errors"] = {}
                            view_all_arches[key][identifier]["warnings"] = {}
                            view_all_arches[key][identifier]["pkg_names"] = set()
                            view_all_arches[key][identifier]["pkg_nevrs"] = set()
                            view_all_arches[key][identifier]["arches"] = set()
                            view_all_arches[key][identifier]["category"] = None

                            self._init_pkg_or_srpm_relations_fields(view_all_arches[key][identifier])

                        if view_all_arches["has_buildroot"]:
                            if not self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["succeeded"]:
                                view_all_arches["everything_succeeded"] = False
                                view_all_arches[key][identifier]["buildroot_succeeded"] = False
                                view_all_arches[key][identifier]["errors"][arch] = self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["errors"]
                            if self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["warnings"]["message"]:
                                view_all_arches["no_warnings"] = False
                                view_all_arches[key][identifier]["buildroot_no_warnings"] = False
                                view_all_arches[key][identifier]["warnings"][arch] = self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["warnings"]


                        view_all_arches[key][identifier]["arches"].add(arch)

                        self._populate_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], package, type="srpm")


                    # Add binary packages to source packages
                    for pkg_id, pkg in view["pkgs"].items():

                        source_name = pkg["source_name"]

                        # Add package names
                        view_all_arches["source_pkgs_by_name"][source_name]["pkg_names"].add(pkg["name"])

                        # Add package nevrs
                        pkg_nevr = f"{pkg['name']}-{pkg['evr']}"
                        view_all_arches["source_pkgs_by_name"][source_name]["pkg_nevrs"].add(pkg_nevr)

                # RPMs
                for pkg in view_all_arches["pkgs_by_nevr"].values():
                    category = None
                    if pkg["in_workload_ids_env"]:
                        category = "env"
                    elif pkg["in_workload_ids_req"]:
                        category = "req"
                    elif pkg["in_workload_ids_dep"]:
                        category = "dep"
                    elif pkg["in_buildroot_of_srpm_id_env"]:
                        category = "build_base"
                    elif pkg["in_buildroot_of_srpm_id_req"] or pkg["in_buildroot_of_srpm_id_dep"]:
                        if pkg["level_number"] == 1:
                            category = "build_level_1"
                        elif pkg["level_number"] > 1:
                            category = "build_level_2_plus"

                    if category is not None:
                        view_all_arches["numbers"]["pkgs"][category] += 1

                view_all_arches["numbers"]["pkgs"]["runtime"] = \
                    view_all_arches["numbers"]["pkgs"]["env"] + \
                    view_all_arches["numbers"]["pkgs"]["req"] + \
                    view_all_arches["numbers"]["pkgs"]["dep"]
                view_all_arches["numbers"]["pkgs"]["build"] = \
                    view_all_arches["numbers"]["pkgs"]["build_base"] + \
                    view_all_arches["numbers"]["pkgs"]["build_level_1"] + \
                    view_all_arches["numbers"]["pkgs"]["build_level_2_plus"]

                # SRPMs
                for pkg in view_all_arches["source_pkgs_by_name"].values():
                    category = None
                    if pkg["in_workload_ids_env"]:
                        category = "env"
                    elif pkg["in_workload_ids_req"]:
                        category = "req"
                    elif pkg["in_workload_ids_dep"]:
                        category = "dep"
                    elif pkg["in_buildroot_of_srpm_id_env"]:
                        category = "build_base"
                    elif pkg["in_buildroot_of_srpm_id_req"] or pkg["in_buildroot_of_srpm_id_dep"]:
                        if pkg["level_number"] == 1:
                            category = "build_level_1"
                        elif pkg["level_number"] > 1:
                            category = "build_level_2_plus"

                    if category is not None:
                        view_all_arches["numbers"]["srpms"][category] += 1

                view_all_arches["numbers"]["srpms"]["runtime"] = \
                    view_all_arches["numbers"]["srpms"]["env"] + \
                    view_all_arches["numbers"]["srpms"]["req"] + \
                    view_all_arches["numbers"]["srpms"]["dep"]

                view_all_arches["numbers"]["srpms"]["build"] = \
                    view_all_arches["numbers"]["srpms"]["build_base"] + \
                    view_all_arches["numbers"]["srpms"]["build_level_1"] + \
                    view_all_arches["numbers"]["srpms"]["build_level_2_plus"]

                # Done
                views_all_arches[view_conf_id] = view_all_arches

        self.data["views_all_arches"] = views_all_arches

    def _add_unwanted_packages_to_view(self, view, view_conf):

        arch = view["arch"]

        # Find exclusion lists mathing this view's label(s)
        unwanted_conf_ids = set()
        for view_label in view_conf["labels"]:
            for unwanted_conf_id, unwanted in self.configs["unwanteds"].items():
                for unwanted_label in unwanted["labels"]:
                    if view_label == unwanted_label:
                        unwanted_conf_ids.add(unwanted_conf_id)

        # Dicts
        pkgs_unwanted_buildroot = {}
        pkgs_unwanted_completely = {}
        srpms_unwanted_buildroot = {}
        srpms_unwanted_completely = {}

        # Populate the dicts
        for unwanted_conf_id in unwanted_conf_ids:
            unwanted_conf = self.configs["unwanteds"][unwanted_conf_id]

            # Pkgs
            for pkg_name in unwanted_conf["unwanted_packages"]:
                if pkg_name not in pkgs_unwanted_completely:
                    pkgs_unwanted_completely[pkg_name] = set()
                pkgs_unwanted_completely[pkg_name].add(unwanted_conf_id)

            # Arch Pkgs
            for pkg_name in unwanted_conf["unwanted_arch_packages"][arch]:
                if pkg_name not in pkgs_unwanted_completely:
                    pkgs_unwanted_completely[pkg_name] = set()
                pkgs_unwanted_completely[pkg_name].add(unwanted_conf_id)

            # SRPMs
            for pkg_source_name in unwanted_conf["unwanted_source_packages"]:
                if pkg_source_name not in srpms_unwanted_completely:
                    srpms_unwanted_completely[pkg_source_name] = set()
                srpms_unwanted_completely[pkg_source_name].add(unwanted_conf_id)

        # Add it to the packages
        for pkg_id, pkg in view["pkgs"].items():
            pkg_name = pkg["name"]
            srpm_name = pkg["source_name"]

            if pkg_name in pkgs_unwanted_completely:
                list_ids = pkgs_unwanted_completely[pkg_name]
                view["pkgs"][pkg_id]["unwanted_completely_in_list_ids"].update(list_ids)

            if srpm_name in srpms_unwanted_completely:
                list_ids = srpms_unwanted_completely[srpm_name]
                view["pkgs"][pkg_id]["unwanted_completely_in_list_ids"].update(list_ids)

        # Add it to the srpms
        for srpm_id, srpm in view["source_pkgs"].items():
            srpm_name = srpm["name"]

            if srpm_name in srpms_unwanted_completely:
                list_ids = srpms_unwanted_completely[srpm_name]
                view["source_pkgs"][srpm_id]["unwanted_completely_in_list_ids"].update(list_ids)

    def _add_unwanted_packages_to_views(self):

        log("")
        log("Adding Unwanted Packages to views...")
        log("")

        # First, the standard views
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                if view_conf["buildroot_strategy"] == "root_logs":
                    for arch in view_conf["architectures"]:

                        view_id = f"{view_conf_id}:{arch}"
                        view = self.data["views"][view_id]

                        self._add_unwanted_packages_to_view(view, view_conf)


    def _recommend_maintainers(self):

        # Packages can be on one or more _levels_:
        #   level 0 is runtime
        #   level 1 is build deps of the previous level
        #   level 2 is build deps of the previous level
        #   ... etc.
        #
        # Within a level, they can be on one or more _sublevels_:
        #   level 0 sublevel 0 is explicitly required
        #   level 0 sublevel 1 is runtiem deps of the previous sublevel
        #   level 0 sublevel 2 is runtiem deps of the previous sublevel
        #   ... etc
        #   level 1 sublevel 0 is direct build deps of the previous level
        #   level 1 sublevel 1 is runtime deps of the previous sublevel
        #   level 1 sublevel 2 is runtiem deps of the previous sublevel
        #   ... etc
        #
        # I'll call a combination of these a _score_ because I can't think of
        # anything better at this point. It's a tuple!
        #
        # (0, 0)
        #  |  '-- sub-level 0 == explicitly required
        #  '---- level 0 == runtime
        #

        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]
            view_all_arches = self.data["views_all_arches"][view_conf_id]

            # Skip addons for now
            # TODO: Implement support for addons
            if view_conf["type"] == "addon":
                continue

            log(f"  {view_conf_id}")

            # Level 0
            level = str(0)
            sublevel = str(0)
            score = (level, sublevel)

            log(f"    {score}")

            # There's not much point in analyzing packages on multple levels.
            # For example, if someone explicitly requires glibc, I don't need to track
            # details up until the very end of the dependency chain...
            this_level_srpms = set()
            previous_level_srpms = set()

            # Take all explicitly required packages and assign them
            # to the maintainer of their workloads.
            #
            # Or of this is the buildroot levels,
            for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                source_name = pkg["source_name"]

                # Only want explicitly required ones
                for workload_id in pkg["in_workload_ids_req"]:
                    workload = self.data["workloads"][workload_id]
                    workload_conf_id = workload["workload_conf_id"]
                    workload_conf = self.configs["workloads"][workload_conf_id]

                    workload_maintainer = workload_conf["maintainer"]

                    # 1/  maintainer_recommendation

                    if workload_maintainer not in pkg["maintainer_recommendation"]:
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][workload_maintainer] = set()

                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][workload_maintainer].add(score)

                    # 2/  maintainer_recommendation_details

                    if level not in pkg["maintainer_recommendation_details"]:
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}

                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}

                    if workload_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["reasons"] = set()
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"] = set()

                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"].add(workload_conf_id)

            # Lie to the while loop so it runs at least once
            level_changes_made = True
            level_change_detection = set()

            while level_changes_made:
                # Level 1 and higher
                if int(level) > 0:
                    level_changes_made = False

                    log(f"    {score}")

                    # Take all the direct build dependencies
                    # of the previous group, and assign them to the maintainers of packages
                    # that pulled them in
                    for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                        source_name = pkg["source_name"]

                        # Don't process packages on multiple levels. (more details above)
                        if source_name in previous_level_srpms:
                            continue

                        # Look at all SRPMs that directly pull this RPM into the buildroot...
                        for buildroot_srpm_name in pkg["in_buildroot_of_srpm_name_req"]:
                            buildroot_srpm = view_all_arches["source_pkgs_by_name"][buildroot_srpm_name]

                            # ... and if they're in the previous group, assign their maintainer(s)

                            # But limit this to only the ones with the highest score.
                            all_the_previous_sublevels_of_this_buildroot_srpm = set()
                            for buildroot_srpm_maintainer, buildroot_srpm_maintainer_scores in buildroot_srpm["maintainer_recommendation"].items():
                                for buildroot_srpm_maintainer_score in buildroot_srpm_maintainer_scores:
                                    buildroot_srpm_maintainer_score_level, buildroot_srpm_maintainer_score_sublevel = buildroot_srpm_maintainer_score
                                    if not buildroot_srpm_maintainer_score_level == prev_level:
                                        continue
                                    all_the_previous_sublevels_of_this_buildroot_srpm.add(buildroot_srpm_maintainer_score_sublevel)
                            if not all_the_previous_sublevels_of_this_buildroot_srpm:
                                continue
                            the_highest_sublevel_of_this_buildroot_srpm = min(all_the_previous_sublevels_of_this_buildroot_srpm)
                            the_score_I_care_about = (prev_level, the_highest_sublevel_of_this_buildroot_srpm)

                            for buildroot_srpm_maintainer, buildroot_srpm_maintainer_scores in buildroot_srpm["maintainer_recommendation"].items():

                                if the_score_I_care_about in buildroot_srpm_maintainer_scores:

                                    level_change_detection_tuple = (buildroot_srpm_name, pkg_name)
                                    if level_change_detection_tuple not in level_change_detection:
                                        level_changes_made = True
                                        level_change_detection.add(level_change_detection_tuple)

                                    # 1/  maintainer_recommendation

                                    if buildroot_srpm_maintainer not in pkg["maintainer_recommendation"]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][buildroot_srpm_maintainer] = set()

                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][buildroot_srpm_maintainer].add(score)

                                    # 2/  maintainer_recommendation_details

                                    if level not in pkg["maintainer_recommendation_details"]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}

                                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}

                                    if buildroot_srpm_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["reasons"] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"] = set()

                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"].add(buildroot_srpm_name)


                # Time to look at runtime dependencies!
                #
                # Take all packages that depend on the previous group and assign them
                # to the maintainer of their superior package. Do this in a loop until
                # there's nothing to assign.
                #
                # So this will deal with scores 0.1, 0.2, 0.3, ...

                # Lie to the while loop so it runs at least once
                sublevel_changes_made = True
                sublevel_change_detection = set()

                while sublevel_changes_made:

                    # Reset its memories. Let it make some new real memories!!
                    sublevel_changes_made = False

                    # Jump another sub-level down
                    prev_score = score
                    prev_sublevel = sublevel
                    #sublevel += 1
                    sublevel = str(int(sublevel) + 1)
                    score = (level, sublevel)

                    log(f"    {score}")

                    for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                        source_name = pkg["source_name"]

                        # Don't process packages on multiple levels. (more details above)
                        if source_name in previous_level_srpms:
                            continue

                        # Look at all of its superior packages (packages that require it)...
                        for superior_pkg_name in pkg["hard_dependency_of_pkg_names"]:
                            superior_pkg = view_all_arches["pkgs_by_name"][superior_pkg_name]
                            superior_srpm_name = superior_pkg["source_name"]

                            # ... and if they're in the previous group, assign their maintainer(s)
                            for superior_pkg_maintainer, superior_pkg_maintainer_scores in superior_pkg["maintainer_recommendation"].items():
                                if prev_score in superior_pkg_maintainer_scores:

                                    sublevel_change_detection_tuple = (superior_pkg_name, pkg_name, superior_pkg_maintainer)
                                    if sublevel_change_detection_tuple in sublevel_change_detection:
                                        continue
                                    else:
                                        sublevel_changes_made = True
                                        sublevel_change_detection.add(sublevel_change_detection_tuple)

                                    # 1/  maintainer_recommendation

                                    if superior_pkg_maintainer not in pkg["maintainer_recommendation"]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][superior_pkg_maintainer] = set()

                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][superior_pkg_maintainer].add(score)

                                    # 2/  maintainer_recommendation_details

                                    if level not in pkg["maintainer_recommendation_details"]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}

                                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}

                                    if superior_pkg_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"] = set()

                                    # Copy the locations from the superior package one sublevel up
                                    locations = superior_pkg["maintainer_recommendation_details"][level][prev_sublevel][superior_pkg_maintainer]["locations"]
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"].update(locations)

                                    reason = (superior_pkg_name, superior_srpm_name, pkg_name)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"].add(reason)


                # Now add this info to the source packages
                for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                    source_name = pkg["source_name"]

                    # 1/  maintainer_recommendation

                    for maintainer, maintainer_scores in pkg["maintainer_recommendation"].items():

                        if maintainer not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation"]:
                            self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation"][maintainer] = set()

                        self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation"][maintainer].update(maintainer_scores)


                        # Add it here so it's not processed again in the another level
                        this_level_srpms.add(source_name)

                    # 2/  maintainer_recommendation_details

                    for loop_level, loop_sublevels in pkg["maintainer_recommendation_details"].items():

                        if loop_level not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"]:
                            self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level] = {}

                        for loop_sublevel, maintainers in loop_sublevels.items():

                            if loop_sublevel not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level]:
                                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel] = {}

                            for maintainer, maintainer_details in maintainers.items():

                                if maintainer not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel]:
                                    self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer] = {}
                                    self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["reasons"] = set()
                                    self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["locations"] = set()

                                reasons = maintainer_details["reasons"]
                                locations = maintainer_details["locations"]

                                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["reasons"].update(reasons)
                                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["locations"].update(locations)




                # And set stuff for the next level
                prev_level = level
                level = str(int(level) + 1)
                # level += 1
                sublevel = str(0)
                score = (level, sublevel)
                previous_level_srpms.update(this_level_srpms)
                this_level_srpms = set()

            # And elect the best owners for each srpm
            for source_name, srpm in view_all_arches["source_pkgs_by_name"].items():

                if not srpm["maintainer_recommendation_details"]:
                    continue

                level_numbers = set()
                for level_string in srpm["maintainer_recommendation_details"].keys():
                    level_numbers.add(int(level_string))

                lowest_level_int = min(level_numbers)
                lowest_level = str(min(level_numbers))

                if not srpm["maintainer_recommendation_details"][lowest_level]:
                    continue

                sublevel_numbers = set()
                for sublevel_string in srpm["maintainer_recommendation_details"][lowest_level].keys():
                    sublevel_numbers.add(int(sublevel_string))
                lowest_sublevel = str(min(sublevel_numbers))

                maintainers_with_the_best_score = set(srpm["maintainer_recommendation_details"][lowest_level][lowest_sublevel].keys())

                highest_number_of_dependencies = 0
                best_maintainers = set()
                for maint in maintainers_with_the_best_score:

                    # If we're looking at a direct build dependency, count the number of locations == SRPMs that directly need this
                    # And in all other cases count the reasons == the number of packages that runtime require
                    # (in case of 0,0 len(reasons) is always 1 as it just says "directly required" so that works fine)
                    if lowest_level_int > 0 and lowest_sublevel == "0":
                        number_of_dependencies = len(srpm["maintainer_recommendation_details"][lowest_level][lowest_sublevel][maint]["locations"])
                    else:
                        number_of_dependencies = len(srpm["maintainer_recommendation_details"][lowest_level][lowest_sublevel][maint]["reasons"])

                    if number_of_dependencies > highest_number_of_dependencies:
                        highest_number_of_dependencies = number_of_dependencies
                        best_maintainers = set()

                    if number_of_dependencies == highest_number_of_dependencies:
                        best_maintainers.add(maint)

                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["best_maintainers"].update(best_maintainers)

        log("")
        log("  DONE!")
        log("")

    def analyze_things(self):
        log("")
        log("###############################################################################")
        log("### Analyzing stuff! ##########################################################")
        log("###############################################################################")
        log("")

        self._record_metric("started analyze_things()")

        self.data["pkgs"] = {}
        self.data["envs"] = {}
        self.data["workloads"] = {}
        self.data["views"] = {}

        with tempfile.TemporaryDirectory() as tmp:

            if self.settings["dnf_cache_dir_override"]:
                self.tmp_dnf_cachedir = self.settings["dnf_cache_dir_override"]
            else:
                self.tmp_dnf_cachedir = os.path.join(tmp, "dnf_cachedir")
            self.tmp_installroots = os.path.join(tmp, "installroots")

            # Repos
            log("")
            log("=====  Analyzing Repos =====")
            log("")
            self._analyze_repos()

            self._record_metric("finished _analyze_repos()")

            # Environments
            log("")
            log("=====  Analyzing Environments =====")
            log("")
            self._analyze_envs()

            self._record_metric("finished _analyze_envs()")

            # Workloads
            log("")
            log("=====  Analyzing Workloads =====")
            log("")
            self._analyze_workloads()

            self._record_metric("finished _analyze_workloads()")

            # Views
            #
            # This creates:
            #    data["views"][view_id]["id"]
            #    data["views"][view_id]["view_conf_id"]
            #    data["views"][view_id]["arch"]
            #    data["views"][view_id]["workload_ids"]
            #    data["views"][view_id]["pkgs"]
            #    data["views"][view_id]["source_pkgs"]
            #
            log("")
            log("=====  Analyzing Views =====")
            log("")
            self._analyze_views()

            self._record_metric("finished _analyze_views()")

            # Buildroot
            # This is partially similar to workloads, because it's resolving
            # the full dependency tree of the direct build dependencies of SRPMs
            #
            # So compared to workloads:
            #   direct build dependencies are like required packages in workloads
            #   the dependencies are like dependencies in workloads
            #   the "build" group is like environments in workloads
            #
            # This completely creates:
            #   data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]...
            #   data["buildroot"]["srpms"][repo_id][arch][srpm_id]...

            log("")
            log("=====  Analyzing Buildroot =====")
            log("")
            self._analyze_buildroot()

            self._record_metric("finished _analyze_buildroot()")

            # Add buildroot packages to views
            # Further extends the following with buildroot packages:
            #   data["views"][view_id]["pkgs"]
            #   data["views"][view_id]["source_pkgs"]
            #
            log("")
            log("=====  Adding Buildroot to Views =====")
            log("")
            self._add_buildroot_to_views()

            self._record_metric("finished _add_buildroot_to_views()")

            # Unwanted packages
            log("")
            log("=====  Adding Unwanted Packages to Views =====")
            log("")
            self._add_unwanted_packages_to_views()

            self._record_metric("finished _add_unwanted_packages_to_views()")

            # Generate combined views for all arches
            log("")
            log("=====  Generating views_all_arches =====")
            log("")
            self._generate_views_all_arches()

            self._record_metric("finished _generate_views_all_arches()")

            # Recommend package maintainers in views
            log("")
            log("=====  Recommending maintainers =====")
            log("")
            self._recommend_maintainers()

            self._record_metric("finished _recommend_maintainers()")

            # Finally, save the cache for next time
            dump_data(self.settings["root_log_deps_cache_path"], self.cache["root_log_deps"]["next"])

            self._record_metric("finished dumping the root log data cache")

        self._record_metric("finished analyze_things()")

        return self.data

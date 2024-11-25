import tempfile, os, json, datetime, dnf, urllib.request, sys, koji

import multiprocessing, asyncio
from content_resolver.utils import dump_data, load_data, log, err_log, pkg_id_to_name, size, workload_id_to_conf_id, url_to_id
from content_resolver.exceptions import RepoDownloadError, BuildGroupAnalysisError, KojiRootLogError, AnalysisError


def pkg_placeholder_name_to_id(placeholder_name):
    placeholder_id = "{name}-000-placeholder.placeholder".format(name=placeholder_name)
    return placeholder_id


def pkg_placeholder_name_to_nevr(placeholder_name):
    placeholder_id = "{name}-000-placeholder".format(name=placeholder_name)
    return placeholder_id


class Analyzer():

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

        self.global_dnf_repo_cache = {}
        self.data = {}
        self.cache = {}

        self.cache["root_log_deps"] = {}
        self.cache["root_log_deps"]["current"] = {}
        self.cache["root_log_deps"]["next"] = {}

        self.metrics_data = []

        # When analysing buildroot, we don't need metadata about
        # recommends. So this gets flipped and we don't collect them anymore.
        # Saves more than an hour.
        self._global_performance_hack_run_recommends_queries = True

        try:
            self.cache["root_log_deps"]["current"] = load_data(self.settings["root_log_deps_cache_path"])
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

        counter = 0

        for this_record in self.metrics_data:

            if counter == 0:
                prev_timestamp = this_record["timestamp"]
            else:
                self.metrics_data[counter-1]["timestamp"]

            time_diff = this_record["timestamp"] - prev_timestamp

            print("  {} (+{} mins): {}".format(
                this_record["timestamp"].strftime("%H:%M:%S"),
                str(int(time_diff.seconds/60)).zfill(3),
                this_record["name"]
            ))

            counter += 1

    
    def _load_repo_cached(self, base, repo, arch):
        repo_id = repo["id"]

        exists = True
        
        if repo_id not in self.global_dnf_repo_cache:
            exists = False
            self.global_dnf_repo_cache[repo_id] = {}

        elif arch not in self.global_dnf_repo_cache[repo_id]:
            exists = False
        
        if exists:
            #log("  Loading repos from cache...")

            for repo in self.global_dnf_repo_cache[repo_id][arch]:
                base.repos.add(repo)

        else:
            #log("  Loading repos using DNF...")

            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"]:
                    if arch not in repo_data["limit_arches"]:
                        #log("  Skipping {} on {}".format(repo_name, arch))
                        continue
                #log("  Including {}".format(repo_name))

                additional_repo = dnf.repo.Repo(
                    name=repo_name,
                    parent_conf=base.conf
                )
                additional_repo.baseurl = repo_data["baseurl"]
                additional_repo.priority = repo_data["priority"]
                additional_repo.exclude = repo_data["exclude"]
                base.repos.add(additional_repo)

            # Additional repository (if configured)
            #if repo["source"]["additional_repository"]:
            #    additional_repo = dnf.repo.Repo(name="additional-repository",parent_conf=base.conf)
            #    additional_repo.baseurl = [repo["source"]["additional_repository"]]
            #    additional_repo.priority = 1
            #    base.repos.add(additional_repo)

            # All other system repos
            #base.read_all_repos()

            self.global_dnf_repo_cache[repo_id][arch] = []
            for repo in base.repos.iter_enabled():
                self.global_dnf_repo_cache[repo_id][arch].append(repo)
    

    def _analyze_pkgs(self, repo, arch):
        log("Analyzing pkgs for {repo_name} ({repo_id}) {arch}".format(
                repo_name=repo["name"],
                repo_id=repo["id"],
                arch=arch
            ))
        
        with dnf.Base() as base:

            base.conf.debuglevel = 0
            base.conf.errorlevel = 0
            base.conf.logfilelevel = 0

            # Local DNF cache
            cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.cachedir = os.path.join(self.tmp_dnf_cachedir, cachedir_name)

            # Generic installroot
            root_name = "dnf_generic_installroot-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.installroot = os.path.join(self.tmp_installroots, root_name)

            # Architecture
            base.conf.arch = arch
            base.conf.ignorearch = True

            # Releasever
            base.conf.substitutions['releasever'] = repo["source"]["releasever"]

            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"]:
                    if arch not in repo_data["limit_arches"]:
                        log("  Skipping {} on {}".format(repo_name, arch))
                        continue
                log("  Including {}".format(repo_name))

                additional_repo = dnf.repo.Repo(
                    name=repo_name,
                    parent_conf=base.conf
                )
                additional_repo.baseurl = repo_data["baseurl"]
                additional_repo.priority = repo_data["priority"]
                base.repos.add(additional_repo)

            # Additional repository (if configured)
            #if repo["source"]["additional_repository"]:
            #    additional_repo = dnf.repo.Repo(name="additional-repository",parent_conf=base.conf)
            #    additional_repo.baseurl = [repo["source"]["additional_repository"]]
            #    additional_repo.priority = 1
            #    base.repos.add(additional_repo)

            # Load repos
            log("  Loading repos...")
            #base.read_all_repos()


            # At this stage, I need to get all packages from the repo listed.
            # That also includes modular packages. Modular packages in non-enabled
            # streams would be normally hidden. So I mark all the available repos as
            # hotfix repos to make all packages visible, including non-enabled streams.
            for dnf_repo in base.repos.all():
                dnf_repo.module_hotfixes = True

            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            MAX_TRIES = 10
            attempts = 0
            success = False
            while attempts < MAX_TRIES:
                try:
                    base.fill_sack(load_system_repo=False)
                    success = True
                    break
                except dnf.exceptions.RepoError as err:
                    attempts +=1
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing repo '{repo_name} ({repo_id}) {arch}".format(
                repo_name=repo["name"],
                repo_id=repo["id"],
                arch=arch
                )
                err_log(err)
                raise RepoDownloadError(err)

            # DNF query
            query = base.sack.query

            # Get all packages
            all_pkgs_set = set(query())
            pkgs = {}
            for pkg_object in all_pkgs_set:
                pkg_nevra = "{name}-{evr}.{arch}".format(
                    name=pkg_object.name,
                    evr=pkg_object.evr,
                    arch=pkg_object.arch
                )
                pkg_nevr = "{name}-{evr}".format(
                    name=pkg_object.name,
                    evr=pkg_object.evr
                )
                pkg = {}
                pkg["id"] = pkg_nevra
                pkg["name"] = pkg_object.name
                pkg["evr"] = pkg_object.evr
                pkg["nevr"] = pkg_nevr
                pkg["arch"] = pkg_object.arch
                pkg["installsize"] = pkg_object.installsize
                pkg["description"] = pkg_object.description
                #pkg["provides"] = pkg_object.provides
                #pkg["requires"] = pkg_object.requires
                #pkg["recommends"] = pkg_object.recommends
                #pkg["suggests"] = pkg_object.suggests
                pkg["summary"] = pkg_object.summary
                pkg["source_name"] = pkg_object.source_name
                pkg["sourcerpm"] = pkg_object.sourcerpm
                pkg["reponame"] = pkg_object.reponame

                pkgs[pkg_nevra] = pkg
            
            # There shouldn't be multiple packages of the same NVR
            # But the world isn't as simple! So add all reponames
            # to every package, in case it's in multiple repos

            repo_priorities = {}
            for repo_name, repo_data in repo["source"]["repos"].items():
                repo_priorities[repo_name] = repo_data["priority"]

            for pkg_object in all_pkgs_set:
                pkg_nevra = "{name}-{evr}.{arch}".format(
                    name=pkg_object.name,
                    evr=pkg_object.evr,
                    arch=pkg_object.arch
                )
                reponame = pkg_object.reponame

                if "all_reponames" not in pkgs[pkg_nevra]:
                    pkgs[pkg_nevra]["all_reponames"] = set()
                
                pkgs[pkg_nevra]["all_reponames"].add(reponame)
            
            for pkg_nevra, pkg in pkgs.items():
                pkgs[pkg_nevra]["highest_priority_reponames"] = set()

                all_repo_priorities = set()
                for reponame in pkg["all_reponames"]:
                    all_repo_priorities.add(repo_priorities[reponame])
                
                highest_repo_priority = sorted(list(all_repo_priorities))[0]

                for reponame in pkg["all_reponames"]:
                    if repo_priorities[reponame] == highest_repo_priority:
                        pkgs[pkg_nevra]["highest_priority_reponames"].add(reponame)

            log("  Done!  ({pkg_count} packages in total)".format(
                pkg_count=len(pkgs)
            ))
            log("")

        return pkgs
    
    def _analyze_repos(self):
        self.data["repos"] = {}
        for _,repo in self.configs["repos"].items():
            repo_id = repo["id"]
            self.data["pkgs"][repo_id] = {}
            self.data["repos"][repo_id] = {}
            for arch in repo["source"]["architectures"]:
                self.data["pkgs"][repo_id][arch] = self._analyze_pkgs(repo, arch)
            
            # Reading the optional composeinfo
            self.data["repos"][repo_id]["compose_date"] = None
            self.data["repos"][repo_id]["compose_days_ago"] = 0
            if repo["source"]["composeinfo"]:
                # At this point, this is all I can do. Hate me or not, it gets us
                # what we need and won't brake anything in case things go badly. 
                try:
                    with urllib.request.urlopen(repo["source"]["composeinfo"]) as response:
                        composeinfo_raw_response = response.read()

                    composeinfo_data = json.loads(composeinfo_raw_response)
                    self.data["repos"][repo_id]["composeinfo"] = composeinfo_data

                    compose_date = datetime.datetime.strptime(composeinfo_data["payload"]["compose"]["date"], "%Y%m%d").date()
                    self.data["repos"][repo_id]["compose_date"] = compose_date.strftime("%Y-%m-%d")

                    date_now = datetime.datetime.now().date()
                    self.data["repos"][repo_id]["compose_days_ago"] = (date_now - compose_date).days

                except:
                    pass

    def _analyze_package_relations(self, dnf_query, package_placeholders = None):
        relations = {}

        for pkg in dnf_query:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            
            required_by = set()
            recommended_by = set()
            suggested_by = set()

            for dep_pkg in dnf_query.filter(requires=[pkg]):
                dep_pkg_id = "{name}-{evr}.{arch}".format(
                    name=dep_pkg.name,
                    evr=dep_pkg.evr,
                    arch=dep_pkg.arch
                )
                required_by.add(dep_pkg_id)

            if self._global_performance_hack_run_recommends_queries:
                for dep_pkg in dnf_query.filter(recommends=[pkg]):
                    dep_pkg_id = "{name}-{evr}.{arch}".format(
                        name=dep_pkg.name,
                        evr=dep_pkg.evr,
                        arch=dep_pkg.arch
                    )
                    recommended_by.add(dep_pkg_id)
            
            #for dep_pkg in dnf_query.filter(suggests=[pkg]):
            #    dep_pkg_id = "{name}-{evr}.{arch}".format(
            #        name=dep_pkg.name,
            #        evr=dep_pkg.evr,
            #        arch=dep_pkg.arch
            #    )
            #    suggested_by.add(dep_pkg_id)
            
            relations[pkg_id] = {}
            relations[pkg_id]["required_by"] = sorted(list(required_by))
            relations[pkg_id]["recommended_by"] = sorted(list(recommended_by))
            #relations[pkg_id]["suggested_by"] = sorted(list(suggested_by))
            relations[pkg_id]["suggested_by"] = []
            relations[pkg_id]["source_name"] = pkg.source_name
            relations[pkg_id]["reponame"] = pkg.reponame
        
        if package_placeholders:
            for placeholder_name,placeholder_data in package_placeholders.items():
                placeholder_id = pkg_placeholder_name_to_id(placeholder_name)

                relations[placeholder_id] = {}
                relations[placeholder_id]["required_by"] = []
                relations[placeholder_id]["recommended_by"] = []
                relations[placeholder_id]["suggested_by"] = []
                relations[placeholder_id]["reponame"] = None
            
            # TODO: triple for loop!!!!
            for placeholder_name,placeholder_data in package_placeholders.items():
                placeholder_id = pkg_placeholder_name_to_id(placeholder_name)
                for placeholder_dependency_name in placeholder_data["requires"]:
                    for pkg_id in relations:
                        pkg_name = pkg_id_to_name(pkg_id)
                        if pkg_name == placeholder_dependency_name:
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
        
        env = queue_result.get()

        return env


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

        with dnf.Base() as base:

            base.conf.debuglevel = 0
            base.conf.errorlevel = 0
            base.conf.logfilelevel = 0

            # Local DNF cache
            cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.cachedir = os.path.join(self.tmp_dnf_cachedir, cachedir_name)

            # Environment installroot
            root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
                env_conf=env_conf["id"],
                repo=repo["id"],
                arch=arch
            )
            base.conf.installroot = os.path.join(self.tmp_installroots, root_name)

            # Architecture
            base.conf.arch = arch
            base.conf.ignorearch = True

            # Releasever
            base.conf.substitutions['releasever'] = repo["source"]["releasever"]

            # Additional DNF Settings
            base.conf.tsflags.append('justdb')
            base.conf.tsflags.append('noscripts')

            # Environment config
            if "include-weak-deps" not in env_conf["options"]:
                base.conf.install_weak_deps = False
            if "include-docs" not in env_conf["options"]:
                base.conf.tsflags.append('nodocs')

            # Load repos
            #log("  Loading repos...")
            #base.read_all_repos()
            self._load_repo_cached(base, repo, arch)

            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            MAX_TRIES = 10
            attempts = 0
            success = False
            while attempts < MAX_TRIES:
                try:
                    base.fill_sack(load_system_repo=False)
                    success = True
                    break
                except dnf.exceptions.RepoError as err:
                    attempts +=1
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
                    repo=repo["id"],
                    arch=arch
                )
                err_log(err)
                raise RepoDownloadError(err)


            # Packages
            log("  Adding packages...")
            for pkg in env_conf["packages"]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    env["errors"]["non_existing_pkgs"].append(pkg)
                    continue
            
            # Groups
            log("  Adding groups...")
            if env_conf["groups"]:
                base.read_comps(arch_filter=True)
            for grp_spec in env_conf["groups"]:
                group = base.comps.group_by_pattern(grp_spec)
                if not group:
                    env["errors"]["non_existing_pkgs"].append(grp_spec)
                    continue
                base.group_install(group.id, ['mandatory', 'default'])

            # Architecture-specific packages
            for pkg in env_conf["arch_packages"][arch]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    env["errors"]["non_existing_pkgs"].append(pkg)
                    continue
            
            # Resolve dependencies
            log("  Resolving dependencies...")
            try:
                base.resolve()
            except dnf.exceptions.DepsolveError as err:
                err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                        env_conf=env_conf["id"],
                        repo=repo["id"],
                        arch=arch
                    ))
                err_log("  - {err}".format(err=err))
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            # Write the result into RPMDB.
            # The transaction needs us to download all the packages. :(
            # So let's do that to make it happy.
            log("  Downloading packages...")
            try:
                base.download_packages(base.transaction.install_set)
            except dnf.exceptions.DownloadError as err:
                err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                        env_conf=env_conf["id"],
                        repo=repo["id"],
                        arch=arch
                    ))
                err_log("  - {err}".format(err=err))
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            log("  Running DNF transaction, writing RPMDB...")
            try:
                base.do_transaction()
            except (dnf.exceptions.TransactionCheckError, dnf.exceptions.Error) as err:
                err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                        env_conf=env_conf["id"],
                        repo=repo["id"],
                        arch=arch
                    ))
                err_log("  - {err}".format(err=err))
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            # DNF Query
            log("  Creating a DNF Query object...")
            query = base.sack.query().filterm(pkg=base.transaction.install_set)

            for pkg in query:
                pkg_id = "{name}-{evr}.{arch}".format(
                    name=pkg.name,
                    evr=pkg.evr,
                    arch=pkg.arch
                )
                env["pkg_ids"].append(pkg_id)
            
            env["pkg_relations"] = self._analyze_package_relations(query)

            log("  Done!  ({pkg_count} packages in total)".format(
                pkg_count=len(env["pkg_ids"])
            ))
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
                    log("Analyzing {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                        env_name=env_conf["name"],
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch
                    ))

                    env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )
                    envs[env_id] = self._analyze_env(env_conf, repo, arch)
                    
        self.data["envs"] = envs


    def _return_failed_workload_env_err(self, workload_conf, env_conf, repo, arch):
        workload = {}

        workload["workload_conf_id"] = workload_conf["id"]
        workload["env_conf_id"] = env_conf["id"]
        workload["repo_id"] = repo["id"]
        workload["arch"] = arch

        workload["pkg_env_ids"] = []
        workload["pkg_added_ids"] = []
        workload["pkg_placeholder_ids"] = []

        workload["pkg_relations"] = []

        workload["errors"] = {}
        workload["errors"]["non_existing_pkgs"] = []
        workload["succeeded"] = False
        workload["env_succeeded"] = False

        workload["errors"]["message"] = """
        Failed to analyze this workload because of an error while analyzing the environment.

        Please see the associated environment results for a detailed error message.
        """

        return workload


    def _analyze_workload(self, workload_conf, env_conf, repo, arch):

        workload = {}

        workload["workload_conf_id"] = workload_conf["id"]
        workload["env_conf_id"] = env_conf["id"]
        workload["repo_id"] = repo["id"]
        workload["arch"] = arch

        workload["pkg_env_ids"] = []
        workload["pkg_added_ids"] = []
        workload["pkg_placeholder_ids"] = []
        workload["srpm_placeholder_names"] = []

        workload["pkg_relations"] = []

        workload["errors"] = {}
        workload["errors"]["non_existing_pkgs"] = []
        workload["errors"]["non_existing_placeholder_deps"] = []

        workload["warnings"] = {}
        workload["warnings"]["non_existing_pkgs"] = []
        workload["warnings"]["non_existing_placeholder_deps"] = []
        workload["warnings"]["message"] = None

        workload["succeeded"] = True
        workload["env_succeeded"] = True


        # Figure out the workload labels
        # It can only have labels that are in both the workload_conf and the env_conf
        workload["labels"] = list(set(workload_conf["labels"]) & set(env_conf["labels"]))

        with dnf.Base() as base:

            base.conf.debuglevel = 0
            base.conf.errorlevel = 0
            base.conf.logfilelevel = 0

            # Local DNF cache
            cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.cachedir = os.path.join(self.tmp_dnf_cachedir, cachedir_name)

            # Environment installroot
            # Since we're not writing anything into the installroot,
            # let's just use the base image's installroot!
            root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
                env_conf=env_conf["id"],
                repo=repo["id"],
                arch=arch
            )
            base.conf.installroot = os.path.join(self.tmp_installroots, root_name)

            # Architecture
            base.conf.arch = arch
            base.conf.ignorearch = True

            # Releasever
            base.conf.substitutions['releasever'] = repo["source"]["releasever"]

            # Environment config
            if "include-weak-deps" not in workload_conf["options"]:
                base.conf.install_weak_deps = False
            if "include-docs" not in workload_conf["options"]:
                base.conf.tsflags.append('nodocs')

            # Load repos
            #log("  Loading repos...")
            #base.read_all_repos()
            self._load_repo_cached(base, repo, arch)

            # 0 % 

            # Now I need to load the local RPMDB.
            # However, if the environment is empty, it wasn't created, so I need to treat
            # it differently. So let's check!
            if len(env_conf["packages"]) or len(env_conf["arch_packages"][arch]) or len(env_conf["groups"]):
                # It's not empty! Load local data.
                base.fill_sack(load_system_repo=True)
            else:
                # It's empty. Treat it like we're using an empty installroot.
                # This sometimes fails, so let's try at least N times
                # before totally giving up!
                MAX_TRIES = 10
                attempts = 0
                success = False
                while attempts < MAX_TRIES:
                    try:
                        base.fill_sack(load_system_repo=False)
                        success = True
                        break
                    except dnf.exceptions.RepoError as err:
                        attempts +=1
                        #log("  Failed to download repodata. Trying again!")
                if not success:
                    err = "Failed to download repodata while analyzing workload '{workload_id} on '{env_id}' from '{repo}' {arch}...".format(
                            workload_id=workload_conf_id,
                            env_id=env_conf_id,
                            repo_name=repo["name"],
                            repo=repo_id,
                            arch=arch)
                    err_log(err)
                    raise RepoDownloadError(err)
            
            # 37 %

            # Packages
            #log("  Adding packages...")
            for pkg in workload_conf["packages"]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    if pkg in self.settings["weird_packages_that_can_not_be_installed"]:
                        continue
                    else:
                        if "strict" in workload_conf["options"]:
                            workload["errors"]["non_existing_pkgs"].append(pkg)
                        else:
                            workload["warnings"]["non_existing_pkgs"].append(pkg)
                        continue
            
            # Groups
            #log("  Adding groups...")
            if workload_conf["groups"]:
                base.read_comps(arch_filter=True)
            for grp_spec in workload_conf["groups"]:
                group = base.comps.group_by_pattern(grp_spec)
                if not group:
                    workload["errors"]["non_existing_pkgs"].append(grp_spec)
                    continue
                base.group_install(group.id, ['mandatory', 'default'])
            
            
                # TODO: Mark group packages as required... the following code doesn't work
                #for pkg in group.packages_iter():
                #    print(pkg.name)
                #    workload_conf["packages"].append(pkg.name)
                
                    
            
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
            #log("  Adding package placeholder dependencies...")
            for placeholder_name, placeholder_data in package_placeholders.items():
                for pkg in placeholder_data["requires"]:
                    try:
                        base.install(pkg)
                    except dnf.exceptions.MarkingError:
                        if "strict" in workload_conf["options"]:
                            workload["errors"]["non_existing_placeholder_deps"].append(pkg)
                        else:
                            workload["warnings"]["non_existing_placeholder_deps"].append(pkg)
                        continue

            # Architecture-specific packages
            for pkg in workload_conf["arch_packages"][arch]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    if "strict" in workload_conf["options"]:
                        workload["errors"]["non_existing_pkgs"].append(pkg)
                    else:
                        workload["warnings"]["non_existing_pkgs"].append(pkg)
                    continue

            if workload["errors"]["non_existing_pkgs"] or workload["errors"]["non_existing_placeholder_deps"]:
                error_message_list = []
                if workload["errors"]["non_existing_pkgs"]:
                    error_message_list.append("The following required packages are not available:")
                    for pkg_name in workload["errors"]["non_existing_pkgs"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                if workload["errors"]["non_existing_placeholder_deps"]:
                    error_message_list.append("The following dependencies of package placeholders are not available:")
                    for pkg_name in workload["errors"]["non_existing_placeholder_deps"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                error_message = "\n".join(error_message_list)
                workload["succeeded"] = False
                workload["errors"]["message"] = str(error_message)
                #log("  Failed!  (Error message will be on the workload results page.")
                #log("")
                return workload
            
            if workload["warnings"]["non_existing_pkgs"] or workload["warnings"]["non_existing_placeholder_deps"]:
                error_message_list = []
                if workload["warnings"]["non_existing_pkgs"]:
                    error_message_list.append("The following required packages are not available (and were skipped):")
                    for pkg_name in workload["warnings"]["non_existing_pkgs"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                if workload["warnings"]["non_existing_placeholder_deps"]:
                    error_message_list.append("The following dependencies of package placeholders are not available (and were skipped):")
                    for pkg_name in workload["warnings"]["non_existing_placeholder_deps"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                error_message = "\n".join(error_message_list)
                workload["warnings"]["message"] = str(error_message)

            # 37 %

            # Resolve dependencies
            #log("  Resolving dependencies...")
            try:
                base.resolve()
            except dnf.exceptions.DepsolveError as err:
                workload["succeeded"] = False
                workload["errors"]["message"] = str(err)
                #log("  Failed!  (Error message will be on the workload results page.")
                #log("")
                return workload

            # 43 %

            # DNF Query
            #log("  Creating a DNF Query object...")
            query_env = base.sack.query()
            pkgs_env = set(query_env.installed())
            pkgs_added = set(base.transaction.install_set)
            pkgs_all = set.union(pkgs_env, pkgs_added)
            query_all = base.sack.query().filterm(pkg=pkgs_all)
            
            # OK all good so save stuff now
            for pkg in pkgs_env:
                pkg_id = "{name}-{evr}.{arch}".format(
                    name=pkg.name,
                    evr=pkg.evr,
                    arch=pkg.arch
                )
                workload["pkg_env_ids"].append(pkg_id)
            
            for pkg in pkgs_added:
                pkg_id = "{name}-{evr}.{arch}".format(
                    name=pkg.name,
                    evr=pkg.evr,
                    arch=pkg.arch
                )
                workload["pkg_added_ids"].append(pkg_id)

            # No errors so far? That means the analysis has succeeded,
            # so placeholders can be added to the list as well.
            # (Failed workloads need to have empty results, that's why)
            for placeholder_name in package_placeholders:
                workload["pkg_placeholder_ids"].append(pkg_placeholder_name_to_id(placeholder_name))
            
            for srpm_placeholder_name in srpm_placeholders:
                workload["srpm_placeholder_names"].append(srpm_placeholder_name)

            # 43 %

            workload["pkg_relations"] = self._analyze_package_relations(query_all, package_placeholders)

            # 100 %
            
            pkg_env_count = len(workload["pkg_env_ids"])
            pkg_added_count = len(workload["pkg_added_ids"])
            #log("  Done!  ({pkg_count} packages in total. That's {pkg_env_count} in the environment, and {pkg_added_count} added.)".format(
            #    pkg_count=str(pkg_env_count + pkg_added_count),
            #    pkg_env_count=pkg_env_count,
            #    pkg_added_count=pkg_added_count
            #))
            #log("")

        # How long do various parts take:
        # 37 % - populatind DNF's base.sack
        # 6 %  - resolving deps
        # 57 % - _analyze_package_relations with recommends

        # Removing recommends from _analyze_package_relations 
        # gets the total duration down to
        # 64 %

        return workload

    
    def _analyze_workload_process(self, queue_result, workload_conf, env_conf, repo, arch):

        workload = self._analyze_workload(workload_conf, env_conf, repo, arch)
        queue_result.put(workload)


    async def _analyze_workloads_subset_async(self, task_queue, results):

        for task in task_queue:
            workload_conf = task["workload_conf"]
            env_conf = task["env_conf"]
            repo = task["repo"]
            arch = task["arch"]

            workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                workload_conf_id=workload_conf["id"],
                env_conf_id=env_conf["id"],
                repo_id=repo["id"],
                arch=arch
            )

            # Max processes
            while True:
                if self.current_subprocesses < self.settings["max_subprocesses"]:
                    self.current_subprocesses += 1
                    break
                else:
                    await asyncio.sleep(.1)

            # Log progress
            self.workload_queue_counter_current += 1
            log("[{} of {}]".format(self.workload_queue_counter_current, self.workload_queue_counter_total))
            log("Analyzing workload: {}".format(workload_id))
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
                    await asyncio.sleep(.1)
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
            if queue_result.empty():
                log("")
                log("")
                log("--------------------------------------------------------------------------")
                log("")
                log("ERROR: Workload analysis failed")
                log("")
                log("Details:")
                log("  workload_conf: {}".format(workload_conf["id"]))
                log("  env_conf:      {}".format(env_conf["id"]))
                log("  repo:          {}".format(repo["id"]))
                log("  arch:          {}".format(arch))
                log("")
                log("More details somewhere above.")
                log("")
                log("--------------------------------------------------------------------------")
                log("")
                log("")
                sys.exit(1)
        
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
                        env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                            env_conf_id=env_conf["id"],
                            repo_id=repo["id"],
                            arch=arch
                        )
                        env = self.data["envs"][env_id]

                        if env["succeeded"]:
                            self._queue_workload_processing(workload_conf, env_conf, repo, arch)

                        else:
                            workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                                workload_conf_id=workload_conf_id,
                                env_conf_id=env_conf_id,
                                repo_id=repo_id,
                                arch=arch
                            )
                            self.data["workloads"][workload_id] = _return_failed_workload_env_err(workload_conf, env_conf, repo, arch)

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
                "sourcerpm": "{}-000-placeholder".format(input_pkg["srpm"]),
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

        log("Analyzing view: {view_name} ({view_conf_id}) for {arch}".format(
            view_name=view_conf["name"],
            view_conf_id=view_conf_id,
            arch=arch
        ))

        view_id = "{view_conf_id}:{arch}".format(
            view_conf_id=view_conf_id,
            arch=arch
        )

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

        log("  Includes {} workloads.".format(len(view["workload_ids"])))

        # Packages
        for workload_id in view["workload_ids"]:
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
                    sourcerpm = "{}.src.rpm".format(srpm_id)
                    view["source_pkgs"][srpm_id] = self._init_view_srpm({"sourcerpm": sourcerpm, "source_name": srpm_name, "reponame": None})
                
                # It's a placeholder
                view["source_pkgs"][srpm_id]["placeholder"] = True

                # Build requires
                view["source_pkgs"][srpm_id]["placeholder_directly_required_pkg_names"] = workload_conf["package_placeholders"]["srpms"][srpm_name]["buildrequires"]
        
        # If this is an addon view, remove all packages that are already in the parent view
        if view_conf["type"] == "addon":
            base_view_conf_id = view_conf["base_view_id"]

            base_view_id = "{base_view_conf_id}:{arch}".format(
                base_view_conf_id=base_view_conf_id,
                arch=arch
            )

            for base_view_pkg_id in views[base_view_id]["pkgs"]:
                if base_view_pkg_id in view["pkgs"]:
                    del view["pkgs"][base_view_pkg_id]

        # Done with packages!
        log("  Includes {} packages.".format(len(view["pkgs"])))

        # But not with source packages, that's an entirely different story!
        for pkg_id, pkg in view["pkgs"].items():
            srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

            if srpm_id not in view["source_pkgs"]:
                view["source_pkgs"][srpm_id] = self._init_view_srpm(pkg)

            # Include some information from the RPM
            view["source_pkgs"][srpm_id]["pkg_ids"].add(pkg_id)

            view["source_pkgs"][srpm_id]["in_workload_ids_all"].update(pkg["in_workload_ids_all"])
            view["source_pkgs"][srpm_id]["in_workload_ids_req"].update(pkg["in_workload_ids_req"])
            view["source_pkgs"][srpm_id]["in_workload_ids_dep"].update(pkg["in_workload_ids_dep"])
            view["source_pkgs"][srpm_id]["in_workload_ids_env"].update(pkg["in_workload_ids_env"])
        
        log("  Includes {} source packages.".format(len(view["source_pkgs"])))


        log("  DONE!")
        log("")

        return view


    def _analyze_views(self):

        views = {}

        # First, analyse the standard views
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                for arch in view_conf["architectures"]:
                    view = self._analyze_view(view_conf, arch, views)
                    view_id = view["id"]

                    views[view_id] = view
        
        # Second, analyse the addon views
        # This is important as they need the standard views already available
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "addon":
                base_view_conf_id = view_conf["base_view_id"]
                base_view_conf = self.configs["views"][base_view_conf_id]

                for arch in set(view_conf["architectures"]) & set(base_view_conf["architectures"]):
                    view = self._analyze_view(view_conf, arch, views)
                    view_id = view["id"]

                    views[view_id] = view
        
        self.data["views"] = views


    def _populate_buildroot_with_view_srpms(self, view_conf, arch):
        view_conf_id = view_conf["id"]

        log("Initialising buildroot packages of: {view_name} ({view_conf_id}) for {arch}".format(
            view_name=view_conf["name"],
            view_conf_id=view_conf_id,
            arch=arch
        ))

        view_id = "{view_conf_id}:{arch}".format(
            view_conf_id=view_conf_id,
            arch=arch
        )

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


    def _get_build_deps_from_a_root_log(self, root_log):
        required_pkgs = []

        # The individual states are nicely described inside the for loop.
        # They're processed in order
        state = 0
        
        for file_line in root_log.splitlines():

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
                    pkg_name = file_line.split()[3].strip('"').rsplit("-",2)[0]
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
                    pkg_index = file_line.split().index("already") - 2
                    pkg_name = file_line.split()[pkg_index].strip('"').rsplit("-",2)[0]
                    required_pkgs.append(pkg_name)

                # The next line will be the first package. Next state!
                # DNF5 reports "Installing: ## packages" in the Transaction Summary,
                # which we need to ignore
                if "Installing:" in file_line and len(file_line.split()) == 3:
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
                elif file_line.split()[2] == "Package" and file_line.split()[-1] == "installed.":
                    pkg_name = file_line.split()[3].strip('"').rsplit("-",2)[0]
                    required_pkgs.append(pkg_name)

                else:
                    # I need to deal with the following thing...
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
                    # Which if I take the usual file_line.split()[2] I get the correct name,
                    # but the next line gives me "aarch64" as a package name which is wrong.
                    #
                    # So the usual line has file_line.split() == 8
                    # The one with the long package name has file_line.split() == 3
                    # and the one following it has file_line.split() == 7
                    # 
                    # One more thing... long release!
                    #
                    # DEBUG util.py:446:   qrencode-devel               aarch64 4.0.2-8.fc35                  build  13 k
                    # DEBUG util.py:446:   systemtap-sdt-devel          aarch64 4.6~pre16291338gf2c14776-1.fc36
                    # DEBUG util.py:446:                                                                      build  71 k
                    # DEBUG util.py:446:   tpm2-tss-devel               aarch64 3.1.0-4.fc36                  build 315 k
                    #
                    # So the good one here is file_line.split() == 5.
                    # And the following is also file_line.split() == 5. Fun!
                    # 
                    # So if it ends with B, k, M, G it's the wrong line, so skip, otherwise take the package name.
                    #
                    # I can also anticipate both get long... that would mean I need to skip file_line.split() == 4.

                    if len(file_line.split()) == 10 or len(file_line.split()) == 11:
                        # Sometimes DNF5 prints "Package ... is already installed" in the middle of a line
                        pkg_index = file_line.split().index("already") - 2
                        pkg_name = file_line.split()[pkg_index].strip('"').rsplit("-",2)[0]
                        required_pkgs.append(pkg_name)
                        if pkg_index == 3:
                            pkg_name = file_line.split()[7]
                        else:
                            pkg_name = file_line.split()[2]
                        required_pkgs.append(pkg_name)

                    # TODO: len(file_line.split()) == 9 ??

                    elif len(file_line.split()) == 8 or len(file_line.split()) == 3:
                        pkg_name = file_line.split()[2]
                        required_pkgs.append(pkg_name)

                    elif len(file_line.split()) == 7 or len(file_line.split()) == 4:
                        continue

                    elif len(file_line.split()) == 6 or len(file_line.split()) == 5:
                        # DNF5 uses B/KiB/MiB/GiB, DNF4 uses B/k/M/G
                        if file_line.split()[4] in ["B", "KiB", "k", "MiB", "M", "GiB", "G"]:
                            continue
                        else:
                            pkg_name = file_line.split()[2]
                            required_pkgs.append(pkg_name)

                    else:
                        raise KojiRootLogError
            

            # 4/
            # I'm done. So I can break out of the loop.
            elif state == 4:
                break
                

        return required_pkgs


    def _resolve_srpm_using_root_log(self, srpm_id, arch, koji_session, koji_files_url):
        
        # Buildroot grows pretty quickly. Use a fake one for development.
        if self.settings["dev_buildroot"]:
            # Making sure there are 3 passes at least, but that it won't get overwhelmed
            if srpm_id.rsplit("-",2)[0] in ["bash", "make", "unzip"]:
                return ["gawk", "xz", "findutils"]
            
            elif srpm_id.rsplit("-",2)[0] in ["gawk", "xz", "findutils"]:
                return ['cpio', 'diffutils']
            
            return ["bash", "make", "unzip"]

        # Shim is special.
        if srpm_id.rsplit("-",2)[0] in ["shim"]:
            log(    "It's shim! It gets sometiems tagged from wherever... Let's not even bother!")
            return []
        
        # Starting for real!
        log("    Talking to Koji API...")
        # This sometimes hangs, so I'm giving it a timeout and
        # a few extra tries before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                koji_pkg_data = koji_session.getRPM("{}.src".format(srpm_id))
                koji_logs = koji_session.getBuildLogs(koji_pkg_data["build_id"])
                success = True
                break
            except:
                attempts +=1
                log("    Error talking to Koji API... retrying...")
        if not success:
            raise KojiRootLogError("Could not talk to Koji API")

        koji_log_path = None

        for koji_log in koji_logs:
            if koji_log["name"] == "root.log":
                if koji_log["dir"] == arch or koji_log["dir"] == "noarch":
                    koji_log_path = koji_log["path"]
        
        if not koji_log_path:
            return []

        root_log_url = "{koji_files_url}/{koji_log_path}".format(
            koji_files_url=koji_files_url,
            koji_log_path=koji_log_path
        )

        log("    Downloading the root.log file...")
        # This sometimes hangs, so I'm giving it a timeout and
        # a few extra tries before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                with urllib.request.urlopen(root_log_url, timeout=20) as response:
                    root_log_data = response.read()
                    root_log_contents = root_log_data.decode('utf-8')
                success = True
                break
            except:
                attempts +=1
                log("    Error getting the root log... retrying...")
        if not success:
            raise KojiRootLogError("Could not get a root.log file")

        log("    Parsing the root.log file...")
        directly_required_pkg_names = self._get_build_deps_from_a_root_log(root_log_contents)

        log("    Done!")
        return directly_required_pkg_names


    def _resolve_srpms_using_root_logs(self, pass_counter):
        # This function is idempotent!
        # 
        # That means it can be run many times without affecting the old results.

        log("== Resolving SRPMs using root logs - pass {} ========".format(pass_counter))

        # Prepare a counter for the log
        total_srpms_to_resolve = 0
        for koji_id in self.data["buildroot"]["koji_srpms"]:
            for arch in self.data["buildroot"]["koji_srpms"][koji_id]:
                total_srpms_to_resolve += len(self.data["buildroot"]["koji_srpms"][koji_id][arch])
        srpms_to_resolve_counter = 0

        # I need to keep sessions open to Koji
        # And because in some cases (in mixed repos) packages
        # could have been in different koji instances, I need
        # multiple Koji sesions!
        koji_sessions = {}

        for koji_id in self.data["buildroot"]["koji_srpms"]:
            koji_urls = self.data["buildroot"]["koji_urls"][koji_id]

            # If the cache is empty, initialise it
            if koji_id not in self.cache["root_log_deps"]["current"]:
                self.cache["root_log_deps"]["current"][koji_id] = {}
            if koji_id not in self.cache["root_log_deps"]["next"]:
                self.cache["root_log_deps"]["next"][koji_id] = {}

            # Initiate Koji sessions
            if koji_id not in koji_sessions:
                koji_sessions[koji_id] = koji.ClientSession(koji_urls["api"], opts = {"timeout": 20})

            for arch in self.data["buildroot"]["koji_srpms"][koji_id]:

                # If the cache is empty, initialise it
                if arch not in self.cache["root_log_deps"]["current"][koji_id]:
                    self.cache["root_log_deps"]["current"][koji_id][arch] = {}
                if arch not in self.cache["root_log_deps"]["next"][koji_id]:
                    self.cache["root_log_deps"]["next"][koji_id][arch] = {}
                

                for srpm_id, srpm in self.data["buildroot"]["koji_srpms"][koji_id][arch].items():
                    srpms_to_resolve_counter += 1
                
                    log("")
                    log("[ Buildroot - pass {} - {} of {} ]".format(pass_counter, srpms_to_resolve_counter, total_srpms_to_resolve))
                    log("Koji root_log {srpm_id} {arch}".format(
                        srpm_id=srpm_id,
                        arch=arch
                    ))
                    if not srpm["directly_required_pkg_names"]:
                        if srpm_id in self.cache["root_log_deps"]["current"][koji_id][arch]:
                            log("  Using Cache!")
                            directly_required_pkg_names = self.cache["root_log_deps"]["current"][koji_id][arch][srpm_id]

                        elif srpm_id in self.cache["root_log_deps"]["next"][koji_id][arch]:
                            log("  Using Cache!")
                            directly_required_pkg_names = self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id]
                        
                        else:
                            log("  Resolving...")
                            directly_required_pkg_names = self._resolve_srpm_using_root_log(srpm_id, arch, koji_sessions[koji_id], koji_urls["files"])
                        
                        self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id] = directly_required_pkg_names

                        # Here it's important to add the packages to the already initiated
                        # set, because its reference is shared between the koji_srpms and the srpm sections
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"].update(directly_required_pkg_names)
                    else:
                        log("  Skipping! (already done before)")
        log("")
        log("  DONE!")
        log("")


    def _analyze_build_groups(self):

        log("")
        log("Analyzing build groups...")
        log("")

        # Need to analyse build groups for all repo_ids
        # and arches of buildroot["srpms"]
        for repo_id in self.data["buildroot"]["srpms"]:
            self.data["buildroot"]["build_groups"][repo_id] = {}

            for arch in self.data["buildroot"]["srpms"][repo_id]:

                generated_id = "CR-buildroot-base-env-{repo_id}-{arch}".format(
                    repo_id=repo_id,
                    arch=arch
                )

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

                log("Resolving build group: {repo_id} {arch}".format(
                    repo_id=repo_id,
                    arch=arch
                ))
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
        # 
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

        log("  Found {} new SRPMs!".format(counter))
        log("  DONE!")
        log("")

        return counter


    def _analyze_srpm_buildroots(self, pass_counter):
        # This function is idempotent!
        # 
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
                    
                    #log("[ Buildroot - pass {} - {} of {} ]".format(pass_counter, srpms_to_resolve_counter, total_srpms_to_resolve))
                    #log("Resolving SRPM buildroot: {repo_id} {arch} {srpm_id}".format(
                    #    repo_id=repo_id,
                    #    arch=arch,
                    #    srpm_id=srpm_id
                    #))
                    repo = self.configs["repos"][repo_id]

                    #fake_workload = self._analyze_workload(fake_workload_conf, fake_env_conf, repo, arch)
                    self._queue_workload_processing(fake_workload_conf, fake_env_conf, repo, arch)

                    # Save the buildroot data
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = True

        asyncio.run(self._analyze_workloads_async(fake_workload_results))

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():

                    if srpm["processed"]:
                        continue

                    fake_workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                        workload_conf_id=srpm_id,
                        env_conf_id=self.data["buildroot"]["build_groups"][repo_id][arch]["generated_id"],
                        repo_id=repo_id,
                        arch=arch
                    )

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
        # 
        # This initialises and populates:
        #   buildroot["build_groups"]
        self._analyze_build_groups()

        pass_counter = 0
        while True:
            pass_counter += 1

            self._record_metric("  started pass {}:".format(pass_counter))

            log("")
            log("== Buildroot resolution - pass {} ========".format(pass_counter))
            log("")
            log("")
            # Get the directly_required_pkg_names from koji root logs
            # 
            # Adds stuff to existing:
            #   data["buildroot"]["koji_srpms"]...
            # ... which also updates:
            #   data["buildroot"]["srpms"]...
            # ... because it's interlinked.
            self._resolve_srpms_using_root_logs(pass_counter)

            self._record_metric("    finished _resolve_srpms_using_root_logs")

            # And now resolving the actual buildroot
            self._analyze_srpm_buildroots(pass_counter)

            self._record_metric("    finished _analyze_srpm_buildroots")

            # Resolving dependencies could have added new SRPMs into the mix that also
            # need their buildroots resolved! So let's find out if there are any
            new_srpms_count = self._expand_buildroot_srpms()

            self._record_metric("    finished with new_srpms_count == {}".format(new_srpms_count))

            if not new_srpms_count:
                log("")
                log("All passes completed!")
                log("")
                break


    def _add_missing_levels_to_pkg_or_srpm(self, pkg_or_srpm, level):

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

        view_id = "{view_conf_id}:{arch}".format(
            view_conf_id=view_conf_id,
            arch=arch
        )

        repo_id = view_conf["repository"]

        view = self.data["views"][view_id]


        log("")
        log("Adding buildroot to view {}...".format(view_id))

        # Starting with all SRPMs in this view
        srpm_ids_to_process = set(view["source_pkgs"])

        # Starting on level 1, the first buildroot level
        # (it's 0 because it gets incremented to 1 immediately after the loop starts)
        level = 0

        while True:
            level += 1
            added_pkg_ids = set()

            log("  Pass {}...".format(level))

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

            log ("    added {} RPMs".format(len(added_pkg_ids)))
            log ("    added {} SRPMs".format(len(srpm_ids_to_process)))

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


    def _init_pkg_or_srpm_relations_fields(self, target_pkg, type = None):
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
        target_pkg["in_buildroot_of_srpm_name_all"] = {} # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_req"] = {} # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_dep"] = {} # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_env"] = {} # of set() of srpm_ids

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

            # Dependency of RPM Names
            target_pkg["dependency_of_pkg_names"] = {} # of set() of nevrs
            target_pkg["hard_dependency_of_pkg_names"] = {} # of set() of nevrs
            target_pkg["weak_dependency_of_pkg_names"] = {} # if set() of nevrs

    
    def _populate_pkg_or_srpm_relations_fields(self, target_pkg, source_pkg, type = None, view = None):

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
            target_pkg["in_workload_ids_{}".format(list_type)].update(source_pkg["in_workload_ids_{}".format(list_type)])

            target_pkg["in_buildroot_of_srpm_id_{}".format(list_type)].update(source_pkg["in_buildroot_of_srpm_id_{}".format(list_type)])

            for workload_id in source_pkg["in_workload_ids_{}".format(list_type)]:
                workload_conf_id = workload_id_to_conf_id(workload_id)
                target_pkg["in_workload_conf_ids_{}".format(list_type)].add(workload_conf_id)

            for srpm_id in source_pkg["in_buildroot_of_srpm_id_{}".format(list_type)]:
                srpm_name = pkg_id_to_name(srpm_id)

                if srpm_name not in target_pkg["in_buildroot_of_srpm_name_{}".format(list_type)]:
                    target_pkg["in_buildroot_of_srpm_name_{}".format(list_type)][srpm_name] = set()
                
                target_pkg["in_buildroot_of_srpm_name_{}".format(list_type)][srpm_name].add(srpm_id)
        
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
            
            level +=1
 
        
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
                pkg_nevr = "{name}-{evr}".format(
                    name=pkg["name"],
                    evr=pkg["evr"]
                )
                target_pkg["hard_dependency_of_pkg_nevrs"].add(pkg_nevr)

                if pkg_name not in target_pkg["hard_dependency_of_pkg_names"]:
                    target_pkg["hard_dependency_of_pkg_names"][pkg_name] = set()
                target_pkg["hard_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)

            # Weak dependency of
            for list_type in ["recommended", "suggested"]:
                for pkg_id in source_pkg["{}_by".format(list_type)]:
                    pkg_name = pkg_id_to_name(pkg_id)

                    # This only happens in addon views, and only rarely.
                    # (see the long comment above)
                    if pkg_id not in view["pkgs"]:
                        view_conf_id = view["view_conf_id"]
                        view_conf = self.configs["views"][view_conf_id]
                        if view_conf["type"] == "addon":
                            continue

                    pkg = view["pkgs"][pkg_id]
                    pkg_nevr = "{name}-{evr}".format(
                        name=pkg["name"],
                        evr=pkg["evr"]
                    )
                    target_pkg["weak_dependency_of_pkg_nevrs"].add(pkg_nevr)

                    if pkg_name not in target_pkg["weak_dependency_of_pkg_names"]:
                        target_pkg["weak_dependency_of_pkg_names"][pkg_name] = set()
                    target_pkg["weak_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)
            
            # All types of dependency
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["hard_dependency_of_pkg_nevrs"])
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["weak_dependency_of_pkg_nevrs"])

            for pkg_name, pkg_nevrs in target_pkg["hard_dependency_of_pkg_names"].items():
                if pkg_name not in target_pkg["dependency_of_pkg_names"]:
                    target_pkg["dependency_of_pkg_names"][pkg_name] = set()
                
                target_pkg["dependency_of_pkg_names"][pkg_name].update(pkg_nevrs)

            for pkg_name, pkg_nevrs in target_pkg["weak_dependency_of_pkg_names"].items():
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
                    view_id = "{view_conf_id}:{arch}".format(
                        view_conf_id=view_conf_id,
                        arch=arch
                    )

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
                        pkg_nevr = "{name}-{evr}".format(
                            name=pkg["name"],
                            evr=pkg["evr"]
                        )
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
                    
                    view_all_arches["numbers"]["pkgs"][category] += 1
                
                view_all_arches["numbers"]["pkgs"]["runtime"] = view_all_arches["numbers"]["pkgs"]["env"] + view_all_arches["numbers"]["pkgs"]["req"] + view_all_arches["numbers"]["pkgs"]["dep"]
                view_all_arches["numbers"]["pkgs"]["build"] = view_all_arches["numbers"]["pkgs"]["build_base"] + view_all_arches["numbers"]["pkgs"]["build_level_1"] + view_all_arches["numbers"]["pkgs"]["build_level_2_plus"]
                
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

                        view_id = "{view_conf_id}:{arch}".format(
                            view_conf_id=view_conf_id,
                            arch=arch
                        )

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

            log("  {}".format(view_conf_id))

            # Level 0
            level = str(0)
            sublevel = str(0)
            score = (level, sublevel)

            log("    {}".format(score))

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
                        #pkg["maintainer_recommendation"][workload_maintainer] = set()
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][workload_maintainer] = set()
                    
                    #pkg["maintainer_recommendation"][workload_maintainer].add(score)
                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][workload_maintainer].add(score)

                    # 2/  maintainer_recommendation_details

                    if level not in pkg["maintainer_recommendation_details"]:
                        #pkg["maintainer_recommendation_details"][level] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}
                    
                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                        #pkg["maintainer_recommendation_details"][level][sublevel] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}
                    
                    if workload_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                        #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer] = {}
                        #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["reasons"] = {}
                        #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["reasons"] = set()
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"] = set()

                    #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"].add(workload_conf_id)
                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"].add(workload_conf_id)

            # Lie to the while loop so it runs at least once
            level_changes_made = True
            level_change_detection = set()


            while level_changes_made:

                # Level 1 and higher
                if int(level) > 0:

                    level_changes_made = False

                    log("    {}".format(score))

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
                                        #pkg["maintainer_recommendation"][workload_maintainer] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][buildroot_srpm_maintainer] = set()

                                    #pkg["maintainer_recommendation"][buildroot_srpm_maintainer].add(score)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][buildroot_srpm_maintainer].add(score)

                                    # 2/  maintainer_recommendation_details

                                    if level not in pkg["maintainer_recommendation_details"]:
                                        #pkg["maintainer_recommendation_details"][level] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}
                                    
                                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}
                                    
                                    if buildroot_srpm_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["reasons"] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["reasons"] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"] = set()

                                    #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"].add(buildroot_srpm_name)
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

                    log("    {}".format(score))

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
                                        #pkg["maintainer_recommendation"][workload_maintainer] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][superior_pkg_maintainer] = set()

                                    #pkg["maintainer_recommendation"][superior_pkg_maintainer].add(score)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][superior_pkg_maintainer].add(score)

                                    # 2/  maintainer_recommendation_details

                                    if level not in pkg["maintainer_recommendation_details"]:
                                        #pkg["maintainer_recommendation_details"][level] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}
                                    
                                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}
                                    
                                    if superior_pkg_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"] = set()

                                    # Copy the locations from the superior package one sublevel up
                                    locations = superior_pkg["maintainer_recommendation_details"][level][prev_sublevel][superior_pkg_maintainer]["locations"]
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"].update(locations)

                                    reason = (superior_pkg_name, superior_srpm_name, pkg_name)
                                    #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"].add(reason)
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
                #level += 1
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

            # List of supported arches
            all_arches = self.settings["allowed_arches"]

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
            # 
            log("")
            log("=====  Analyzing Buildroot =====")
            log("")
            self._analyze_buildroot()

            self._record_metric("finished _analyze_buildroot()")

            # Add buildroot packages to views
            # 
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
            self. _generate_views_all_arches()

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
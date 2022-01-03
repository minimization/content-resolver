#!/usr/bin/python3


import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint, urllib.request, sys, koji
import concurrent.futures
import rpm_showme as showme
from functools import lru_cache
import multiprocessing, time

import asyncio





class AnalysisError(Exception):
    pass


# Highlight subprocesses
first_log_pid = os.getpid()

def log(msg):
    if first_log_pid == os.getpid():
        print("  {}  :  {}".format(os.getpid(), msg), file=sys.stderr)
    else:
        print("* {}  :  {}".format(os.getpid(), msg), file=sys.stderr)

log("")


class Analyzer():

    def __init__(self):
        self.workload_queue = {}
        self.workload_queue_counter_total = 0
        self.workload_queue_counter_current = 0
        self.current_subprocesses = 0

        self.data = {}
        self.data["envs"] = {}
        self.data["envs"]["env_id"] = {}
        self.data["envs"]["env_id"]["succeeded"] = True

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


        log(workload)


        time.sleep(2)

        if workload_conf["id"] == 22 and arch == "x86_64":
            fuck

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
                if self.current_subprocesses < 4:  # <<------ subprocesses
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

            #while True:
            #    if process.is_alive():
            #        log("  waiting on {}".format(workload_id))
            #        await asyncio.sleep(1)
            #    else:
            #        break
            
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

        log("_analyze_workloads_async")

        tasks = []

        for repo in self.workload_queue:
            for arch in self.workload_queue[repo]:

                task_queue = self.workload_queue[repo][arch]

                tasks.append(asyncio.create_task(self._analyze_workloads_subset_async(task_queue, results)))
        
        for task in tasks:
            await task
            await asyncio.sleep(.1)

        log("DONE!")

    
    def _queue_workload_processing(self, workload_conf, env_conf, repo, arch):

        #log("_queue_workload_processing")
        
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


    def _analyze_workloads(self):

        #log("_analyze_workloads")

        for repo_id in ["fedora", "rhel"]:
            for arch in ["aarch64", "x86_64"]:

                for n in range(1,10):

                    workload_conf = {
                        "id": n
                    }

                    env_conf = {
                        "id": "env_id"
                    }

                    repo = {
                        "id": repo_id
                    }

                    self._queue_workload_processing(workload_conf, env_conf, repo, arch)

                    self.workload_queue_counter_total += 1

        self.data["workloads"] = {}

        log("Analyzing {} workloads.".format(self.workload_queue_counter_total))
        log("")
        log("")

        asyncio.run(self._analyze_workloads_async(self.data["workloads"]))


log("")
a = Analyzer()
a._analyze_workloads()
print(a.data["workloads"])


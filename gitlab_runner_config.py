#!/usr/bin/env python3

###############################################################################
# Copyright (c) 2019, Lawrence Livermore National Security, LLC
# Produced at the Lawrence Livermore National Laboratory
# Written by Thomas Mendoza mendoza33@llnl.gov
# LLNL-CODE-795365
# All rights reserved
#
# This file is part of gitlab-runner-auth:
# https://github.com/LLNL/gitlab-runner-auth
#
# SPDX-License-Identifier: MIT
###############################################################################

import os
import re
import sys
import socket
import argparse
import json
import urllib.request
import logging
import stat
from string import Formatter
from urllib.request import Request
from urllib.parse import urlencode, urljoin
from urllib.error import HTTPError
from json import JSONDecodeError

LOGGER_NAME = "gitlab-runner-config"
logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(LOGGER_NAME)

class gitlab_client:
    base_url = ""
    admin_token = ""
    access_token = ""

    def __init__(self, url, admin_token, access_token):
        self.base_url = url
        self.admin_token = admin_token
        self.access_token = access_token

    def list_runners(self, filters=None):
        try:
            query = ""
            if filters:
                query = "?" + urlencode(filters)

            url = urljoin(self.base_url, "runners/all" + query)
            request = Request(url, headers={"PRIVATE-TOKEN": self.access_token})
            return json.load(urllib.request.urlopen(request))
        except JSONDecodeError:
            print("Failed parsing request data JSON")
            sys.exit(1)
        except HTTPError as e:
            print("Error listing Gitlab repos: {reason}".format(reason=e.reason))
            sys.exit(1)

    def runner_info(self, repo_id):
        try:
            url = urljoin(self.base_url, "runners/" + str(repo_id))
            request = Request(url, headers={"PRIVATE-TOKEN": self.access_token})
            return json.load(urllib.request.urlopen(request))
        except JSONDecodeError:
            print("Failed parsing request data JSON")
            sys.exit(1)
        except HTTPError as e:
            print(
                "Error while requesting repo info for repo {repo}: {reason}".format(
                    repo=repo_id, reason=e.reason
                )
            )
            sys.exit(1)

    def valid_runner_token(self, token):
        """Test whether or not a runner token is valid"""

        try:
            url = urljoin(self.base_url, "runners/verify")
            data = urlencode({"token": token})

            request = Request(url, data=data.encode(), method="POST")
            urllib.request.urlopen(request)
            return True
        except HTTPError as e:
            if e.code == 403:
                return False
            else:
                print("Error while validating token: {}".format(e.reason))
                sys.exit(1)

    def register_runner(self, runner_type, tags):
        """Registers a runner and returns its info"""

        try:
            # the first tag is always the hostname
            url = urljoin(self.base_url, "runners")
            data = urlencode(
                {
                    "token": self.admin_token,
                    "description": tags[0] + "-" + runner_type,
                    "tag_list": ",".join(tags + [runner_type]),
                }
            )

            request = Request(url, data=data.encode(), method="POST")
            response = urllib.request.urlopen(request)
            if response.getcode() == 201:
                return json.load(response)
            else:
                print("Registration for {runner_type} failed".format(runner_type))
                sys.exit(1)
        except HTTPError as e:
            print(
                "Error registering runner {runner} with tags {tags}: {reason}".format(
                    runner=runner_type, tags=",".join(tags), reason=e.reason
                )
            )
            sys.exit(1)

    def update_runner_token(self, token, runner_type):
        config = None
        if not token or not self.valid_runner_token(token):
            # no refresh endpoint...delete and re-register
            if token:
                self.delete_runner(token)
            config = self.register_runner(runner_type, None)
        return config

    def delete_runner(self, runner_token):
        """Delete an existing runner"""

        try:
            url = urljoin(self.base_url, "runners")
            data = urlencode(
                {
                    "token": runner_token,
                }
            )

            request = Request(url, data=data.encode(), method="DELETE")
            response = urllib.request.urlopen(request)
            if response.getcode() == 204:
                return True
            else:
                print("Deleting runner with id failed")
                sys.exit(1)
        except HTTPError as e:
            print("Error deleting runner: {reason}".format(reason=e.reason))
            sys.exit(1)

    def list_gitlab_tags(self):
        filters = {"scope": "shared", "tag_list": ",".join([socket.gethostname()])}
        runners = [
            self.runner_info(r["id"])
            for r in self.list_runners(filters)
        ]
        return set(tag for r in runners for tag in r["tag_list"])

    def update_runner(self, runner_type, token=""):
        gitlab_tags = self.list_gitlab_tags()
        if runner_type not in gitlab_tags:
            # no config template tags in common with Gitlab, register runners
            # for all the tags pulled from the template.
            return self.register_runner(
                runner_type,
                generate_tags(runner_type=runner_type),
            )
        else:
            return self.update_runner_token(token, runner_type)

def generate_tags(runner_type=""):
    """The set of tags for a host

    Minimally, this is the system hostname, but should include things like OS,
    architecture, GPU availability, etc.

    These tags are specified by runner configs and used by CI specs to run jobs
    on the appropriate host.
    """

    # the hostname is _required_ to make this script work, everything else
    # is extra (as far as this script is concerned)
    hostname = socket.gethostname()

    # also tag with the generic cluster name by removing any trailing numbers
    tags = [hostname, re.sub(r"\d", "", hostname)]
    if runner_type == "batch":

        def which(cmd):
            all_paths = (
                os.path.join(path, cmd) for path in os.environ["PATH"].split(os.pathsep)
            )

            return any(
                os.access(path, os.X_OK) and os.path.isfile(path) for path in all_paths
            )

        if which("bsub"):
            tags.append("lsf")
        elif which("salloc"):
            tags.append("slurm")
        elif which("cqsub"):
            tags.append("cobalt")
    return tags

def create_client(data, clients):
    url = data.get("url", "")
    if url in clients:
        return clients[url]

    # TODO: tokens in the runner configs for multiple gitlab urls, separate?
    admin_token = data.get("admin_token", "")
    access_token = data.get("access_token", "")
    # removing admin/access tokens for future writing out to config.toml
    del data["admin_token"]
    del data["access_token"]

    clients[url] = gitlab_client(url, admin_token, access_token)
    return clients[url]

def read_runner(templates, path, clients):
    """Reads a runner file and generates runner configurations"""
    data_file = os.path.join(templates, path)
    runners = {}
    with open(data_file, "r") as fh:
        runner_config = json.load(fh)
        for runner_type, data in runner_config.items():
            data = data or {}
            name = data.get("name", "")
            if name in runners:
                print(f"Duplicate runner found for {name}")
                sys.exit(1)

            client = create_client(data, clients)
            runner_type = data.get("executor", "")
            token = data.get("token", "")
            runners[name] = client.update_runner(runner_type, token)
    return runners

def update_runner_config(config_template, config_file, internal_config):
    """Using data from config.json, write the config.toml used by the runner

    Nominally, this method will provide a dictionary of keyword arguments to
    format of the form:

    {
        "<runner_type>": "<runner_token>",
        ...,
    }

    The config.toml must specify named template args like:

    [[runners]]
      token = "{runner_type}"
      ...

    and _not_ use positional arguments.
    """

    template_kwargs = {
        "hostname": socket.gethostname(),
    }
    template_kwargs.update(
        {runner: data["token"] for runner, data in internal_config.items()}
    )

    with open(config_template) as th, open(config_file, "w") as ch:
        template = th.read()
        config = template.format(**template_kwargs)
        ch.write(config)

def owner_only_permissions(path):
    st = path.stat()
    return not (bool(st.st_mode & stat.S_IRWXG) and bool(st.st_mode & stat.S_IRWXU))

def configure_runners(prefix, instance):
    """Processes a directory of runners"""

    # cache of gitlab clients
    clients = {}

    runners = {}

    if not all(owner_only_permissions(d) for d in [prefix, instance]):
        logger.error(
            "check permissions on {prefix} or {instance}, too permissive, exiting".format(
            prefix=prefix, instance=instance))
        sys.exit(1)

    for filename in os.listdir(instance):
        if filename.endswith(".toml"):
            runners.update(read_runner(instance, filename, clients))

    config_file = os.path.join(prefix, "config.toml")
    config_template = os.path.join(prefix, "config.template")
    update_runner_config(config_template, config_file, runners)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="On the fly runner config")
    parser.add_argument(
        "-p",
        "--prefix",
        default="/etc/gitlab-runner",
        help="The runner config directory prefix"
    )
    parser.add_argument(
        "-i",
        "--instance",
        default="/etc/gitlab-runner/main",
        help="The config template directory for this instance under the configuration directory "
    )
    args = parser.parse_args()
    configure_runners(args.prefix, args.instance)
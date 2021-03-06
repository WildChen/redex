#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import distutils.version
import errno
import fnmatch
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from os.path import basename, isfile, join

import pyredex.unpacker
from pyredex.logger import log


temp_dirs = []


def abs_glob(directory, pattern="*"):
    """
    Returns all files that match the specified glob inside a directory.
    Returns absolute paths. Does not return files that start with '.'
    """
    for result in glob.glob(join(directory, pattern)):
        yield join(directory, result)


def make_temp_dir(name="", debug=False):
    """ Make a temporary directory which will be automatically deleted """
    global temp_dirs
    directory = tempfile.mkdtemp(name)
    if not debug:
        temp_dirs.append(directory)
    return directory


def remove_temp_dirs():
    global temp_dirs
    for directory in temp_dirs:
        shutil.rmtree(directory)


def with_temp_cleanup(fn, always_clean=False):
    success = always_clean
    try:
        fn()
        success = True
    finally:
        if success:
            remove_temp_dirs()


def find_android_build_tools():
    VERSION_REGEXP = r"\d+\.\d+\.\d+$"
    android_home = os.environ["ANDROID_SDK"]
    build_tools = join(android_home, "build-tools")
    version = max(
        (d for d in os.listdir(build_tools) if re.match(VERSION_REGEXP, d)),
        key=distutils.version.StrictVersion,
    )
    return join(build_tools, version)


def remove_signature_files(extracted_apk_dir):
    for f in abs_glob(extracted_apk_dir, "META-INF/*"):
        cert_path = join(extracted_apk_dir, f)
        if isfile(cert_path):
            os.remove(cert_path)


def sign_apk(keystore, keypass, keyalias, apk):
    subprocess.check_call(
        [
            join(find_android_build_tools(), "apksigner"),
            "sign",
            "--v1-signing-enabled",
            "--v2-signing-enabled",
            "--ks",
            keystore,
            "--ks-pass",
            "pass:" + keypass,
            "--ks-key-alias",
            keyalias,
            apk,
        ],
        stdout=sys.stderr,
    )


def remove_comments_from_line(l):
    (found_backslash, in_quote) = (False, False)
    for idx, c in enumerate(l):
        if c == "\\" and not found_backslash:
            found_backslash = True
        elif c == '"' and not found_backslash:
            found_backslash = False
            in_quote = not in_quote
        elif c == "#" and not in_quote:
            return l[:idx]
        else:
            found_backslash = False
    return l


def remove_comments(lines):
    return "".join([remove_comments_from_line(l) + "\n" for l in lines])


def argparse_yes_no_flag(parser, flag_name, on_prefix="", off_prefix="no-", **kwargs):
    class FlagAction(argparse.Action):
        def __init__(self, option_strings, dest, nargs=None, **kwargs):
            super(FlagAction, self).__init__(option_strings, dest, nargs=0, **kwargs)

        def __call__(self, parser, namespace, values, option_string=None):
            setattr(
                namespace,
                self.dest,
                False if option_string.startswith(f"--{off_prefix}") else True,
            )

    parser.add_argument(
        f"--{on_prefix}{flag_name}",
        f"--{off_prefix}{flag_name}",
        dest=flag_name,
        action=FlagAction,
        default=False,
        **kwargs,
    )


def unzip_apk(apk, destination_directory):
    with zipfile.ZipFile(apk) as z:
        z.extractall(destination_directory)


def extract_dex_number(dexfilename):
    m = re.search(r"(classes|.*-)(\d+)", basename(dexfilename))
    if m is None:
        raise Exception("Bad secondary dex name: " + dexfilename)
    return int(m.group(2))


def dex_glob(directory):
    """
    Return the dexes in a given directory, with the primary dex first.
    """
    primary = join(directory, "classes.dex")
    if not isfile(primary):
        raise Exception("No primary dex found")

    secondaries = [
        d for d in glob.glob(join(directory, "*.dex")) if not d.endswith("classes.dex")
    ]
    secondaries.sort(key=extract_dex_number)

    return [primary] + secondaries


def move_dexen_to_directories(root, dexpaths):
    """
    Move each dex file to its own directory within root and return a list of the
    new paths. Redex will operate on each dex and put the modified dex into the
    same directory.
    """
    res = []
    for idx, dexpath in enumerate(dexpaths):
        dexname = basename(dexpath)
        dirpath = join(root, "dex" + str(idx))
        os.mkdir(dirpath)
        shutil.move(dexpath, dirpath)
        res.append(join(dirpath, dexname))

    return res


def ensure_libs_dir(libs_dir, sub_dir):
    """Ensures the base libs directory and the sub directory exist. Returns top
    most dir that was created.
    """
    if os.path.exists(libs_dir):
        os.mkdir(sub_dir)
        return sub_dir
    else:
        os.mkdir(libs_dir)
        os.mkdir(sub_dir)
        return libs_dir


def get_file_ext(file_name):
    return os.path.splitext(file_name)[1]


class ZipManager:
    """
    __enter__: Unzips input_apk into extracted_apk_dir
    __exit__: Zips extracted_apk_dir into output_apk
    """

    per_file_compression = {}

    def __init__(self, input_apk, extracted_apk_dir, output_apk):
        self.input_apk = input_apk
        self.extracted_apk_dir = extracted_apk_dir
        self.output_apk = output_apk

    def __enter__(self):
        log("Extracting apk...")
        with zipfile.ZipFile(self.input_apk) as z:
            for info in z.infolist():
                self.per_file_compression[info.filename] = info.compress_type
            z.extractall(self.extracted_apk_dir)

    def __exit__(self, *args):
        remove_signature_files(self.extracted_apk_dir)
        if isfile(self.output_apk):
            os.remove(self.output_apk)

        log("Creating output apk")
        with zipfile.ZipFile(self.output_apk, "w") as new_apk:
            # Need sorted output for deterministic zip file. Sorting `dirnames` will
            # ensure the tree walk order. Sorting `filenames` will ensure the files
            # inside the tree.
            # This scheme uses less memory than collecting all files first.
            for dirpath, dirnames, filenames in os.walk(self.extracted_apk_dir):
                dirnames.sort()
                for filename in sorted(filenames):
                    filepath = join(dirpath, filename)
                    archivepath = filepath[len(self.extracted_apk_dir) + 1 :]
                    try:
                        compress = self.per_file_compression[archivepath]
                    except KeyError:
                        compress = zipfile.ZIP_DEFLATED
                    new_apk.write(filepath, archivepath, compress_type=compress)


class UnpackManager:
    """
    __enter__: Unpacks dexes and application modules from extracted_apk_dir into dex_dir
    __exit__: Repacks the dexes and application modules in dex_dir back into extracted_apk_dir
    """

    application_modules = []

    def __init__(
        self,
        input_apk,
        extracted_apk_dir,
        dex_dir,
        have_locators=False,
        debug_mode=False,
        fast_repackage=False,
    ):
        self.input_apk = input_apk
        self.extracted_apk_dir = extracted_apk_dir
        self.dex_dir = dex_dir
        self.have_locators = have_locators
        self.debug_mode = debug_mode
        self.fast_repackage = fast_repackage

    def __enter__(self):
        dex_file_path = self.get_dex_file_path(self.input_apk, self.extracted_apk_dir)

        self.dex_mode = pyredex.unpacker.detect_secondary_dex_mode(dex_file_path)
        log("Detected dex mode " + str(type(self.dex_mode).__name__))
        log("Unpacking dex files")
        self.dex_mode.unpackage(dex_file_path, self.dex_dir)

        log("Detecting Application Modules")
        store_metadata_dir = make_temp_dir(
            ".application_module_metadata", self.debug_mode
        )
        self.application_modules = pyredex.unpacker.ApplicationModule.detect(
            self.extracted_apk_dir
        )
        store_files = []
        for module in self.application_modules:
            canary_prefix = module.get_canary_prefix()
            log(
                "found module: "
                + module.get_name()
                + " "
                + (canary_prefix if canary_prefix is not None else "(no canary prefix)")
            )
            store_path = os.path.join(self.dex_dir, module.get_name())
            os.mkdir(store_path)
            module.unpackage(self.extracted_apk_dir, store_path)
            store_metadata = os.path.join(
                store_metadata_dir, module.get_name() + ".json"
            )
            module.write_redex_metadata(store_path, store_metadata)
            store_files.append(store_metadata)
        return store_files

    def __exit__(self, *args):
        log("Repacking dex files")
        log("Emit Locator Strings: %s" % self.have_locators)

        self.dex_mode.repackage(
            self.get_dex_file_path(self.input_apk, self.extracted_apk_dir),
            self.dex_dir,
            self.have_locators,
            fast_repackage=self.fast_repackage,
        )

        locator_store_id = 1
        for module in self.application_modules:
            log(
                "repacking module: "
                + module.get_name()
                + " with id "
                + str(locator_store_id)
            )
            module.repackage(
                self.extracted_apk_dir,
                self.dex_dir,
                self.have_locators,
                locator_store_id,
                fast_repackage=self.fast_repackage,
            )
            locator_store_id = locator_store_id + 1

    def get_dex_file_path(self, input_apk, extracted_apk_dir):
        # base on file extension check if input is
        # an apk file (".apk") or an Android bundle file (".aab")
        # TODO: support loadable modules (at this point only
        # very basic support is provided - in case of Android bundles
        # "regular" apk file content is moved to the "base"
        # sub-directory of the bundle archive)
        if get_file_ext(input_apk) == ".aab":
            return join(extracted_apk_dir, "base", "dex")
        else:
            return extracted_apk_dir


class LibraryManager:
    """
    __enter__: Unpacks additional libraries in extracted_apk_dirs so library class files can be found
    __exit__: Cleanup temp directories used by the class
    """

    temporary_libs_dir = None

    def __init__(self, extracted_apk_dir):
        self.extracted_apk_dir = extracted_apk_dir

    def __enter__(self):
        # Some of the native libraries can be concatenated together into one
        # xz-compressed file. We need to decompress that file so that we can scan
        # through it looking for classnames.
        libs_to_extract = []
        xz_lib_name = "libs.xzs"
        zstd_lib_name = "libs.zstd"
        for root, _, filenames in os.walk(self.extracted_apk_dir):
            for filename in fnmatch.filter(filenames, xz_lib_name):
                libs_to_extract.append(join(root, filename))
            for filename in fnmatch.filter(filenames, zstd_lib_name):
                fullpath = join(root, filename)
                # For voltron modules BUCK creates empty zstd files for each module
                if os.path.getsize(fullpath) > 0:
                    libs_to_extract.append(fullpath)
        if len(libs_to_extract) > 0:
            libs_dir = join(self.extracted_apk_dir, "lib")
            extracted_dir = join(libs_dir, "__extracted_libs__")
            # Ensure both directories exist.
            self.temporary_libs_dir = ensure_libs_dir(libs_dir, extracted_dir)
            lib_count = 0
            for lib_to_extract in libs_to_extract:
                extract_path = join(extracted_dir, "lib_{}.so".format(lib_count))
                if lib_to_extract.endswith(xz_lib_name):
                    cmd = "xz -d --stdout {} > {}".format(lib_to_extract, extract_path)
                else:
                    cmd = "zstd -d {} -o {}".format(lib_to_extract, extract_path)
                subprocess.check_call(cmd, shell=True)
                lib_count += 1

    def __exit__(self, *args):
        # This dir was just here so we could scan it for classnames, but we don't
        # want to pack it back up into the apk
        if self.temporary_libs_dir is not None:
            shutil.rmtree(self.temporary_libs_dir)

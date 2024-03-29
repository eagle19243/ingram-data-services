#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
It is imperative that you process all delta files in sequential order.

You should process delta files first and then deletes.

When processing deletes, you must also process deletes for all of your tables
that are directly related to the item (images, descriptions, etc...these are
files that contain a primary key of EAN). This is required to stay in
compliance with your data license and maintain data integrity.
"""

import argparse
import configparser
import logging.handlers
import multiprocessing
import os
from argparse import HelpFormatter
from operator import attrgetter
from zipfile import ZipFile

from ingram_data_services import logger
from ingram_data_services.__version__ import __version__
from ingram_data_services.ftp import IngramFTP
from ingram_data_services.utils import get_files_matching, get_local_path, set_log_dir, save_run_history

host = None
user = None
passwd = None
pool = None
cover_size = None


class SortingHelpFormatter(HelpFormatter):
    def add_arguments(self, actions):
        actions = sorted(actions, key=attrgetter("option_strings"))
        super(SortingHelpFormatter, self).add_arguments(actions)


def get_args():
    """Returns the Argument Parser."""
    parser = argparse.ArgumentParser(
        description="Login and pull data from Ingram's FTP server",
        usage="ingram-data-services [--config-section CONFIG_SECTION]",
        formatter_class=SortingHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s {version}".format(version=__version__),
    )
    parser.add_argument(
        "--log-file",
        help="location to log the history",
        default="~/finderscope/logs/ingram-data-services.log",
    )
    parser.add_argument(
        "--config-section", help="config section to use", default="default"
    )

    return parser.parse_args()


def get_config():
    """Read the config file."""
    config_file = os.path.expanduser("~/finderscope/config/ingram-data-services.cfg")
    if os.path.exists(config_file):
        config = configparser.ConfigParser()
        config.read(config_file)
        return config
    raise RuntimeError(f'"{config_file}" does not exist')


def setup_logger(log_file):
    """Setup logger"""
    logger.setLevel(logging.DEBUG)

    # Create logging format
    msg_fmt = "[%(levelname)s] [%(asctime)s] [%(name)s] %(message)s"
    date_fmt = "%Y-%m-%d %I:%M:%S %p"
    formatter = logging.Formatter(msg_fmt, date_fmt)

    # Create file handler
    logfile = os.path.expanduser(log_file)
    if not os.path.exists(os.path.dirname(logfile)):
        os.makedirs(os.path.dirname(logfile))
    fh = logging.handlers.RotatingFileHandler(logfile, maxBytes=10485760, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # Create console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)

    # Add logging handlers
    logger.addHandler(fh)
    logger.addHandler(ch)


def download_file(remote_file, download_dir):
    """Helper to allow multiprocessing."""
    # Create the local file path
    local_file = get_local_path(remote_file, download_dir)

    # We can't download multiple files from FTP at once, so we need to
    # create an FTP instance and login for each download
    with IngramFTP(host=host, user=user, passwd=passwd) as ftp:
        try:
            ftp.download_file(remote_file, local_file)
            # print(ftp.get_modified_date(remote_file))
        except KeyboardInterrupt:
            pool.terminate()


def download_data_files(download_dir):
    logger.info("Download Ingram data files ...")
    with IngramFTP(host=host, user=user, passwd=passwd) as ftp:
        logger.info(f"Connected to {host}...")
        logger.info(f"Welcome message: {ftp.getwelcome()}")

        cover_paths = ftp.get_cover_files(folder=cover_size)
        logger.info(f"{len(cover_paths)} cover zips found")
        onix_paths = ftp.get_onix_files()
        logger.info(f"{len(onix_paths)} ONIX zips found")
        onix_bklst_paths = ftp.get_onix_bklst_files()
        logger.info(f"{len(onix_bklst_paths)} ONIX BKLST zips found")
        ref_paths = ftp.get_reference_files()
        logger.info(f"{len(ref_paths)} reference files found")

        # Assemble our tuple of args for the download method
        cover_paths = [(p, download_dir) for p in cover_paths]
        onix_paths = [(p, download_dir) for p in onix_paths]
        onix_bklst_paths = [(p, download_dir) for p in onix_bklst_paths]
        ref_paths = [(p, download_dir) for p in ref_paths]

    # Process the downloads
    with multiprocessing.Pool() as pool:
        pool.starmap(download_file, cover_paths)
        pool.starmap(download_file, onix_paths)
        pool.starmap(download_file, onix_bklst_paths)
        pool.starmap(download_file, ref_paths)


def extract_cover_zip(file, target_dir):
    """Extract files one by one so we can organize into folders
    based on last 4 of ISBN for faster access."""
    with ZipFile(file, "r") as zf:
        filenames = zf.namelist()
        for f in filenames:
            subdir, _ = os.path.splitext(f)
            filepath = os.path.join(target_dir, subdir[-4:], f)
            # We only extract if the file doesn't exist
            if not os.path.exists(filepath):
                zf.extract(f, os.path.join(target_dir, subdir[-4:]))


def extract_zip(file, target_dir):
    """Extract files from zip"""
    with ZipFile(file, "r") as zf:
        zf.extractall(target_dir)


def unzip_onix_threaded(data_zips, download_dir, working_dir):
    """Unzip ONIX zips using multiprocessing."""
    zip_args = []
    for z in data_zips:
        extract_dir = os.path.dirname(z).replace(download_dir, "").lstrip("/")
        extract_dir = os.path.join(working_dir, extract_dir)
        zip_args.append((z, extract_dir))

    # Use multiprocessing to unzip multiple files at once
    with multiprocessing.Pool() as pool:
        pool.starmap(extract_zip, zip_args)


def unzip_covers_threaded(cover_zips, working_dir):
    """Unzip cover zips using multiprocessing."""
    # This is here to prevent an issue where multiple threads were creating
    # a "FileExistsError" when trying to create a folder at the same time?
    # To avoid it we create all the folders ahead of time.
    for i in range(0, 10000):
        dirname = os.path.join(working_dir, "Imageswk", cover_size, f'{i:04}')
        os.makedirs(dirname, exist_ok=True)

    cover_args = []
    for z in cover_zips:
        cover_args.append((z, os.path.join(working_dir, "Imageswk", cover_size)))

    # Use multiprocessing to unzip multiple files at once
    with multiprocessing.Pool() as pool:
        pool.starmap(extract_cover_zip, cover_args)


def main():
    global host, user, passwd, pool, cover_size

    # Ensure proper command line usage
    args = get_args()

    # Read our config file
    config = get_config()

    section = args.config_section
    log_file = args.log_file

    host = config.get(section, "host")
    user = config.get(section, "user")
    passwd = config.get(section, "passwd")

    setup_logger(log_file)
    set_log_dir(os.path.dirname(log_file))

    download_dir = os.path.expanduser(config.get(section, "download_dir"))
    working_dir = os.path.expanduser(config.get(section, "working_dir"))
    cover_size = config.get(section, "cover_size")

    # Download data files
    download_data_files(download_dir)

    # Unzip cover files
    logger.info("Unzip cover zips ...")
    cover_zips = get_files_matching(os.path.join(download_dir, "Imageswk"), "*.zip")
    unzip_covers_threaded(cover_zips, working_dir)

    # Unzip onix files
    logger.info("Unzip ONIX zips ...")
    data_zips = get_files_matching(os.path.join(download_dir, "ONIX"), "*.zip")
    unzip_onix_threaded(data_zips, download_dir, working_dir)

    # Unzip onix backlist files
    logger.info("Unzip ONIX_BKLST zips ...")
    data_zips = get_files_matching(os.path.join(download_dir, "ONIX_BKLST"), "*.zip")
    unzip_onix_threaded(data_zips, download_dir, working_dir)

    # Save run history, on next run we will only be interested in files
    # newer than the most recent run date
    save_run_history()


if __name__ == "__main__":
    main()

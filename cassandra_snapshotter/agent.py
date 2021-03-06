from __future__ import (absolute_import, print_function)

import boto
from boto.s3.connection import S3Connection
from yaml import load

try:
    # LibYAML based parser and emitter
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader
import os
import time
import glob
import logging
import multiprocessing
from multiprocessing.dummy import Pool

from cassandra_snapshotter import logging_helper
from cassandra_snapshotter.timeout import timeout
from cassandra_snapshotter.utils import (add_s3_arguments, base_parser,
                                         map_wrap, get_s3_connection_host,
                                         check_lzop, compressed_pipe)

DEFAULT_CONCURRENCY = max(multiprocessing.cpu_count() - 1, 1)
BUFFER_SIZE = 64  # Default bufsize is 64M
MBFACTOR = float(1 << 20)
MAX_RETRY_COUNT = 3
SLEEP_TIME = 2
UPLOAD_TIMEOUT = 600

logging_helper.configure(
    format='%(name)-12s %(levelname)-8s %(message)s')

logger = logging_helper.CassandraSnapshotterLogger('cassandra_snapshotter.agent')
boto.set_stream_logger('boto', logging.WARNING)


def get_bucket(
        s3_bucket, aws_access_key_id,
        aws_secret_access_key, s3_connection_host):
    connection = S3Connection(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        host=s3_connection_host
    )
    return connection.get_bucket(s3_bucket, validate=False)


def destination_path(s3_base_path, file_path, compressed=True):
    suffix = compressed and '.lzo' or ''
    return '/'.join([s3_base_path, file_path + suffix])


def s3_progress_update_callback(*args):
    # TODO: use this to display some nice progress bar
    pass


@map_wrap
def upload_file(bucket, source, destination, s3_ssenc, bufsize):
    retry_count = 0
    while True:
        try:
            mp = bucket.initiate_multipart_upload(destination, encrypt_key=s3_ssenc)
            logger.info("Initialized multipart upload for file {!s} to {!s}".format(source, destination))
        except Exception as exc:
            logger.error("Error while initializing multipart upload for file {!s} to {!s}".format(source, destination))
            logger.error(exc.message)
            return False
        try:
            for i, chunk in enumerate(compressed_pipe(source, bufsize)):
                mp.upload_part_from_file(chunk, i + 1, cb=s3_progress_update_callback)
        except Exception as exc:
            logger.error("Error uploading file {!s} to {!s}.\
                Retry count: {}".format(source, destination, retry_count))
            logger.error(exc.message)
            if retry_count >= MAX_RETRY_COUNT:
                logger.error("Retried too many times uploading file {!s}".format(source))
                cancel_upload(bucket, mp, destination)
                return False
            else:
                time.sleep(SLEEP_TIME)
                retry_count = retry_count + 1
        else:
            try:
                mp.complete_upload()
            except Exception as exc:
                logger.error("Error completing multipart upload for file {!s} to {!s}".format(source, destination))
                logger.error(exc.message)
                logger.error(mp.to_xml())
                cancel_upload(bucket, mp, destination)
                return False
            else:
                return True


@timeout(UPLOAD_TIMEOUT)
def upload_chunk(mp, chunk, index):
    mp.upload_part_from_file(chunk, index)


def cancel_upload(bucket, mp, remote_path):
    """
    Safe way to cancel a multipart upload
    sleeps SLEEP_TIME seconds and then makes sure that there are not parts left
    in storage
    """
    attempts = 0
    while attempts < 5:
        try:
            time.sleep(SLEEP_TIME)
            mp.cancel_upload()
            time.sleep(SLEEP_TIME)
            for mp in bucket.list_multipart_uploads():
                if mp.key_name == remote_path:
                    mp.cancel_upload()
            return
        except Exception:
            logger.error("Error while cancelling multipart upload")
            attempts += 1


def put_from_manifest(
        s3_bucket, s3_connection_host, s3_ssenc, s3_base_path,
        aws_access_key_id, aws_secret_access_key, manifest,
        bufsize, concurrency=None, incremental_backups=False):
    """
    Uploads files listed in a manifest to amazon S3
    to support larger than 5GB files multipart upload is used (chunks of 60MB)
    files are uploaded compressed with lzop, the .lzo suffix is appended
    """
    exit_code = 0
    bucket = get_bucket(
        s3_bucket, aws_access_key_id,
        aws_secret_access_key, s3_connection_host)
    manifest_fp = open(manifest, 'r')
    buffer_size = int(bufsize * MBFACTOR)
    files = manifest_fp.read().splitlines()
    pool = Pool(concurrency)
    for ret in pool.imap(upload_file,
                         ((bucket, f, destination_path(s3_base_path, f), s3_ssenc, buffer_size) for f in files)):
        if not ret:
            exit_code = 1
            break
    pool.terminate()
    if incremental_backups:
        for f in files:
            os.remove(f)
    exit(exit_code)


def get_data_path(conf_path):
    """Retrieve cassandra data_file_directories from cassandra.yaml"""
    config_file_path = os.path.join(conf_path, 'cassandra.yaml')
    cassandra_configs = {}
    with open(config_file_path, 'r') as f:
        cassandra_configs = load(f, Loader=Loader)
    data_paths = cassandra_configs['data_file_directories']
    return data_paths


def create_upload_manifest(
        snapshot_name, snapshot_keyspaces, snapshot_table,
        conf_path, manifest_path, incremental_backups=False):
    if snapshot_keyspaces:
        keyspace_globs = snapshot_keyspaces.split()
    else:
        keyspace_globs = ['*']

    if snapshot_table:
        table_glob = snapshot_table
    else:
        table_glob = '*'

    data_paths = get_data_path(conf_path)
    files = []
    for data_path in data_paths:
        for keyspace_glob in keyspace_globs:
            path = [
                data_path,
                keyspace_glob,
                table_glob
            ]
            if incremental_backups:
                path += ['backups']
            else:
                path += ['snapshots', snapshot_name]
            path += ['*']

            path = os.path.join(*path)
            glob_results = '\n'.join(glob.glob(os.path.join(path)))
            files.extend([f.strip() for f in glob_results.split("\n")])

    with open(manifest_path, 'w') as manifest:
        manifest.write('\n'.join("%s" % f for f in files))


def main():
    subparsers = base_parser.add_subparsers(
        title='subcommands', dest='subcommand')
    base_parser.add_argument(
        '--incremental_backups', action='store_true', default=False)

    put_parser = subparsers.add_parser(
        'put', help="put files on s3 from a manifest")
    manifest_parser = subparsers.add_parser(
        'create-upload-manifest', help="put files on s3 from a manifest")

    # put arguments
    put_parser = add_s3_arguments(put_parser)
    put_parser.add_argument(
        '--bufsize',
        required=False,
        default=BUFFER_SIZE,
        type=int,
        help="Compress and upload buffer size")

    put_parser.add_argument(
        '--manifest',
        required=True,
        help="The manifest containing the files to put on s3")

    put_parser.add_argument(
        '--concurrency',
        required=False,
        default=DEFAULT_CONCURRENCY,
        type=int,
        help="Compress and upload concurrent processes")

    # create-upload-manifest arguments
    manifest_parser.add_argument('--snapshot_name', required=True, type=str)
    manifest_parser.add_argument('--conf_path', required=True, type=str)
    manifest_parser.add_argument('--manifest_path', required=True, type=str)
    manifest_parser.add_argument(
        '--snapshot_keyspaces', default='', required=False, type=str)
    manifest_parser.add_argument(
        '--snapshot_table', required=False, default='', type=str)

    args = base_parser.parse_args()
    subcommand = args.subcommand

    if subcommand == 'create-upload-manifest':
        create_upload_manifest(
            args.snapshot_name,
            args.snapshot_keyspaces,
            args.snapshot_table,
            args.conf_path,
            args.manifest_path,
            args.incremental_backups
        )

    if subcommand == 'put':
        check_lzop()
        put_from_manifest(
            args.s3_bucket_name,
            get_s3_connection_host(args.s3_bucket_region),
            args.s3_ssenc,
            args.s3_base_path,
            args.aws_access_key_id,
            args.aws_secret_access_key,
            args.manifest,
            args.bufsize,
            args.concurrency,
            args.incremental_backups
        )


if __name__ == '__main__':
    # TODO: if lzop is not available we should fail or run without it
    check_lzop()

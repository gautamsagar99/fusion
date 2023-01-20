"""Fusion fsync."""

import time
import sys
import os
from os.path import relpath
import json
from pathlib import Path
import hashlib
import base64
import logging
from tqdm.auto import tqdm
from joblib import Parallel, delayed
import fsspec
import pandas as pd
from .utils import distribution_to_filename, path_to_url, validate_file_names, upload_files, \
    cpu_count

logger = logging.getLogger(__name__)
VERBOSE_LVL = 25
DEFAULT_CHUNK_SIZE = 2 ** 16


def _get_loop(df, show_progress):
    if show_progress:
        loop = tqdm(df.iterrows(), total=len(df))
    else:
        loop = df.iterrows()
    return loop


def _url_to_path(x):
    file_name = distribution_to_filename("", x.split('/')[2], x.split('/')[4], x.split('/')[6], x.split("/")[0])
    return f"{x.split('/')[0]}/{x.split('/')[2]}/{x.split('/')[4]}/{file_name}"


def _download(fs_fusion, fs_local, df, n_par, show_progress=True):
    def _download_files(row):
        p_path = row["path_fusion"]
        if not fs_local.exists(p_path):
            try:
                fs_local.mkdir(Path(p_path).parent, exist_ok=True, create_parents=True)
            except Exception as ex:
                logger.info(f"Path {p_path} exists already", ex)
        try:
            fs_fusion.get(row["url"], p_path, chunk_size=DEFAULT_CHUNK_SIZE)
            return True, p_path, None
        except Exception as ex:
            logger.log(
                VERBOSE_LVL,
                f'Failed to write to {p_path}. ex - {ex}',
            )
            msg = str(ex)

            return False, p_path, msg

    loop = _get_loop(df, show_progress)
    if len(df) > 1:
        res = Parallel(n_jobs=n_par)(delayed(_download_files)(row) for index, row in loop)
    else:
        res = [_download_files(row) for index, row in loop]

    return res


def _upload(fs_fusion, fs_local, df, n_par, show_progress=True):
    df.rename(columns={"path_local": "path"}, inplace=True)
    loop = _get_loop(df, show_progress)
    parallel = True if len(df) > 1 else False
    res = upload_files(fs_fusion, fs_local, loop, parallel=parallel, n_par=n_par, multipart=True)

    return res


def _generate_md5_token(path, fs):
    hash_md5 = hashlib.md5()
    with fs.open(path, "rb") as file:
        for chunk in iter(lambda: file.read(4096), b""):
            hash_md5_chunk = hashlib.md5()
            hash_md5_chunk.update(chunk)
            hash_md5.update(chunk)

    return base64.b64encode(hash_md5.digest()).decode()


def _get_fusion_df(fs_fusion, datasets_lst, catalog, flatten=False, dataset_format=None):
    df_lst = []
    for dataset in datasets_lst:
        changes = fs_fusion.info(f"{catalog}/datasets/{dataset}")["changes"]["datasets"]
        if len(changes) > 0:
            changes = changes[0]["distributions"]
            urls = [catalog + "/datasets/" + "/".join(i["key"].split(".")) for i in changes]
            urls = [i.replace("distribution", "distributions") for i in urls]
            urls = ["/".join(i.split("/")[:3] + ["datasetseries"] + i.split("/")[3:]) if "datasetseries" not in i else i
                    for i in urls]
            # ts = [pd.Timestamp(i["values"][0]).timestamp() for i in changes]
            sz = [int(i["values"][1]) for i in changes]
            md = [i["values"][2].split("md5=")[-1] for i in changes]
            keys = [_url_to_path(i) for i in urls]

            if flatten:
                keys = ["/".join(k.split("/")[:2] + k.split("/")[-1:]) for k in keys]

            df = pd.DataFrame([keys, urls, sz, md]).T
            df.columns = ["path", "url", "size", "md5"]
            if dataset_format and len(df) > 0:
                df = df[df.url.str.split("/").str[-1] == dataset_format]
            df_lst.append(df)
        else:
            df_lst.append(pd.DataFrame(columns=["path", "url", "size", "md5"]))

    return pd.concat(df_lst)


def _get_local_state(fs_local, fs_fusion, datasets, catalog, dataset_format=None):
    local_files = []
    local_files_rel = []
    local_dirs = [f"{catalog}/{i}" for i in datasets] if len(datasets) > 0 else [catalog]

    for local_dir in local_dirs:
        if not fs_local.exists(local_dir):
            fs_local.mkdir(local_dir, exist_ok=True, create_parents=True)

        local_files = fs_local.find(local_dir)
        local_rel_path = [i[i.find(local_dir):] for i in local_files]
        local_file_validation = validate_file_names(local_rel_path, fs_fusion)
        local_files += [f for flag, f in zip(local_file_validation, local_files) if flag]
        local_files_rel += [os.path.join(local_dir, relpath(i, local_dir)).replace("\\", "/") for i in local_files]

    # local_mtime = [fs_local.info(x)["mtime"] for x in local_files]
    local_md5 = [_generate_md5_token(x, fs_local) for x in local_files]
    local_url_eqiv = [path_to_url(i) for i in local_files]
    df_local = pd.DataFrame([local_files_rel, local_url_eqiv, local_md5]).T
    df_local.columns = ["path", "url", "md5"]

    if dataset_format and len(df_local) > 0:
        df_local = df_local[df_local.url.str.split("/").str[-1] == dataset_format]

    df_local = df_local.sort_values("path").drop_duplicates()
    return df_local


def _synchronize(fs_fusion: fsspec.filesystem, fs_local: fsspec.filesystem, df_local: pd.DataFrame,
                 df_fusion: pd.DataFrame, direction: str = "upload", n_par: int = None, show_progress: bool = True):
    """Synchronize two filesystems."""

    n_par = cpu_count(n_par)
    if direction == "upload":
        join_df = df_local.merge(df_fusion, on="url", suffixes=("_local", "_fusion"), how="left")
        join_df = join_df[join_df["md5_local"] != join_df["md5_fusion"]]
        res = _upload(fs_fusion, fs_local, join_df, n_par, show_progress=show_progress)
    elif direction == "download":
        join_df = df_local.merge(df_fusion, on="url", suffixes=("_local", "_fusion"), how="right")
        join_df = join_df[join_df["md5_local"] != join_df["md5_fusion"]]
        res = _download(fs_fusion, fs_local, join_df, n_par, show_progress=show_progress)
    else:
        raise ValueError("Unknown direction of operation.")
    return res


def fsync(fs_fusion: fsspec.filesystem,
          fs_local: fsspec.filesystem,
          products: list = None,
          datasets: list = None,
          catalog: str= None,
          direction: str = "upload",
          flatten=False,
          dataset_format=None,
          n_par=None,
          show_progress=True,
          log_level=logging.ERROR,
          log_path: str = "."
          ):
    """Synchronisation between the local filesystem and Fusion.

    Args:
        fs_fusion (fsspec.filesystem): Fusion filesystem.
        fs_local (fsspec.filesystem): Local filesystem.
        datasets (list): List of products.
        datasets (list): List of datasets.
        catalog (str): Fusion catalog.
        direction (str): Direction of synchronisation: upload/download.
        flatten (bool): Flatten the folder structure.
        dataset_format (str): Dataset format for upload/download.
        n_par (int, optional): Specify how many distributions to download in parallel. Defaults to all.
        show_progress (bool, optional): Display a progress bar during data download Defaults to True.
        log_level: Logging level. Error level by default.
        log_path (str, optional): The folder path where the log is stored.

    Returns:

    """

    if logger.hasHandlers():
        logger.handlers.clear()
    file_handler = logging.FileHandler(filename="{0}/{1}".format(log_path, "fusion_fsync.log"))
    logging.addLevelName(VERBOSE_LVL, "VERBOSE")
    stdout_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(name)s:%(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)
    logger.setLevel(log_level)

    catalog = "common" if not catalog else catalog
    datasets = [] if not datasets else datasets
    products = [] if not products else products

    assert len(products) > 0 or len(datasets) > 0, "At least one list products or datasets should be non-empty."
    assert direction in ["upload", "download"], "The direction must be either upload or download."

    for product in products:
        res = json.loads(fs_fusion.cat(f"common/products/{product}").decode())
        datasets += [r["identifier"] for r in res["resources"]]

    assert len(datasets) > 0, "The supplied products did not contain any datasets."

    local_state = pd.DataFrame()
    while True:
        try:
            local_state_temp = _get_local_state(fs_local, fs_fusion, datasets, catalog, dataset_format)
            if not local_state_temp.equals(local_state):
                fusion_state = _get_fusion_df(fs_fusion, datasets, catalog, flatten, dataset_format)
                res = _synchronize(fs_fusion, fs_local, local_state_temp, fusion_state, direction, n_par, show_progress)
                if len(res) == 0 or all((i[0] for i in res)):
                    local_state = local_state_temp
            else:
                logger.info("All synced, sleeping")
                time.sleep(10)

        except KeyboardInterrupt:
            if input("Type exit to exit: ") != "exit":
                continue
            break

        except Exception as ex:
            logger.error('%s Issue occurred: %s', type(ex), ex)
            continue
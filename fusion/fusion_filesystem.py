"""Fusion FileSystem."""

from fsspec.implementations.http import HTTPFileSystem, sync
from fsspec.callbacks import _DEFAULT_CALLBACK
import logging
from urllib.parse import urljoin
import hashlib
import base64
import asyncio
from copy import deepcopy
import pandas as pd
import nest_asyncio
import io
from fsspec.utils import DEFAULT_BLOCK_SIZE, isfilelike, nullcontext
from fusion.utils import get_client
from .authentication import FusionCredentials

nest_asyncio.apply()
logger = logging.getLogger(__name__)
VERBOSE_LVL = 25


class FusionHTTPFileSystem(HTTPFileSystem):
    """Fusion HTTP filesystem.
    """

    def __init__(self, credentials='config/client_credentials.json', *args, **kwargs):
        """Same signature as the fsspec HTTPFileSystem.

        Args:
            credentials: Credentials.
            *args: Args.
            **kwargs: Kwargs.
        """

        self.credentials = credentials
        if "get_client" not in kwargs:
            kwargs["get_client"] = get_client
        if "client_kwargs" not in kwargs:
            if isinstance(credentials, FusionCredentials):
                self.credentials = credentials
            else:
                self.credentials = FusionCredentials.from_object(credentials)
            kwargs["client_kwargs"] = {"credentials": self.credentials,
                                       "root_url": "https://fusion-api.jpmorgan.com/fusion/v1/"}
            if self.credentials.proxies:
                if "http" in self.credentials.proxies.keys():
                    kwargs["proxy"] = self.credentials.proxies["http"]
                elif "https" in self.credentials.proxies.keys():
                    kwargs["proxy"] = self.credentials.proxies["https"]

        if "headers" not in kwargs:
            kwargs["headers"] = {"Accept-Encoding": "identity"}

        super().__init__(*args, **kwargs)

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value):
        self._loop = value

    async def _decorate_url_a(self, url):
        url = urljoin(f'{self.client_kwargs["root_url"]}catalogs/', url) if "http" not in url else url
        return url

    def _decorate_url(self, url):
        url = urljoin(f'{self.client_kwargs["root_url"]}catalogs/', url) if "http" not in url else url
        return url

    async def _isdir(self, path):
        path = self._decorate_url(path)
        try:
            ret = await self._info(path)
            return ret["type"] == "directory"
        except Exception as ex:
            logger.log(VERBOSE_LVL, f"Artificial error, {ex}")
            return False

    async def _ls_real(self, url, detail=True, **kwargs):
        # ignoring URL-encoded arguments
        clean_url = url
        if "http" not in url:
            url = f'{self.client_kwargs["root_url"]}catalogs/' + url
        kw = self.kwargs.copy()
        kw.update(kwargs)
        # logger.debug(url)
        session = await self.set_session()
        is_file = False
        size = None
        async with session.get(url, **self.kwargs) as r:
            self._raise_not_found_for_status(r, url)
            try:
                out = await r.json()
            except Exception as ex:
                logger.log(VERBOSE_LVL, f"{url} cannot be parsed to json, {ex}")
                # out = await r.content.read(10)
                out = [r.headers["Content-Disposition"].split("=")[-1]]
                size = int(r.headers["Content-Length"])
                is_file = True

        if not is_file:
            out = [urljoin(clean_url + "/", x["identifier"]) for x in out["resources"]]

        if detail:
            if not is_file:
                return [
                    {
                        "name": u,
                        "size": None,
                        "type": "directory" if not (u.endswith("csv") or u.endswith("parquet")) else "file",
                    }
                    for u in out
                ]
            else:
                return [
                    {
                        "name": out[0],
                        "size": size,
                        "type": "file",
                    }
                ]
        else:
            return out

    def info(self, path, **kwargs):
        """Return info.

        Args:
            path: Path.
            **kwargs: Kwargs.

        Returns:

        """
        path = self._decorate_url(path)
        kwargs["keep_protocol"] = True
        res = super().ls(path, detail=True, **kwargs)[0]
        if res["type"] == "file":
            return res
        else:
            return super().info(path, **kwargs)

    async def _ls(self, url, detail=True, **kwargs):
        url = await self._decorate_url_a(url)
        return await super()._ls(url, detail, **kwargs)

    def ls(self, url, detail=False, **kwargs):
        """List resources.

        Args:
            url: Url.
            detail: Detail.
            **kwargs: Kwargs.

        Returns:

        """
        url = self._decorate_url(url)
        ret = super().ls(url, detail=detail, **kwargs)
        keep_protocol = kwargs.pop("keep_protocol", False)
        if detail:
            if not keep_protocol:
                for k in ret:
                    k["name"] = k["name"].split(f'{self.client_kwargs["root_url"]}catalogs/')[-1]

        else:
            if not keep_protocol:
                return [x.split(f'{self.client_kwargs["root_url"]}catalogs/')[-1] for x in ret]

        return ret

    def exists(self, url, detail=True, **kwargs):
        """Check existence.

        Args:
            url: Url.
            detail: Detail.
            **kwargs: Kwargs.

        Returns:

        """
        url = self._decorate_url(url)
        return super().exists(url, **kwargs)

    def isfile(self, path):
        """Is path a file.

        Args:
            path: Path.

        Returns:

        """
        path = self._decorate_url(path)
        return super().isfile(path)

    def cat(self, url, start=None, end=None, **kwargs):
        """Fetch paths' contents.

        Args:
            url: Url.
            start: Start.
            end: End.
            **kwargs: Kwargs.

        Returns:

        """
        url = self._decorate_url(url)
        return super().cat(url, start=start, end=end, **kwargs)

    def get(self, rpath, lpath, chunk_size=5 * 2 ** 20, callback=_DEFAULT_CALLBACK, **kwargs):
        """Copy file(s) to local.

        Args:
            rpath: Rpath.
            lpath: Lpath.
            chunk_size: Chunk size.
            callback: Callback function.
            **kwargs: Kwargs.

        Returns:

        """
        rpath = self._decorate_url(rpath)
        return super().get(rpath, lpath, chunk_size=5 * 2 ** 20, callback=_DEFAULT_CALLBACK, **kwargs)

    async def _put_file(
        self,
        lpath,
        rpath,
        chunk_size=5 * 2**20,
        callback=_DEFAULT_CALLBACK,
        method="post",
        multipart=False,
        **kwargs,
    ):
        async def put_data():
            # Support passing arbitrary file-like objects
            # and use them instead of streams.
            if isinstance(lpath, io.IOBase):
                context = nullcontext(lpath)
                use_seek = False  # might not support seeking
            else:
                context = open(lpath, "rb")
                use_seek = True

            with context as f:
                if use_seek:
                    callback.set_size(f.seek(0, 2))
                    f.seek(0)
                else:
                    callback.set_size(getattr(f, "size", None))

                if not chunk_size:
                    pass
                    #yield f.read()
                else:
                    chunk = f.read(chunk_size)
                    i = 0
                    lst = []
                    while chunk:
                        kw = self.kwargs.copy()
                        url = rpath + f"/operations/upload?operationId={operation_id}&partNumber={i+1}"
                        kw.update({"headers": kwargs["chunk_headers_lst"][i]})
                        lst.append([meth(url, data=chunk, **kw)])
                        yield meth(url=url, data=chunk, **kw)
                        callback.relative_update(len(chunk))
                        chunk = f.read(chunk_size)
                    #ret = await asyncio.gather(*lst)
                    #return ret

        session = await self.set_session()

        method = method.lower()
        if method not in ("post", "put"):
            raise ValueError(
                f"method has to be either 'post' or 'put', not: {method!r}"
            )

        headers = kwargs["headers"]

        meth = getattr(session, method)
        if not multipart:
            kw = self.kwargs.copy()
            kw.update({"headers": headers})
            async with meth(rpath, data=lpath.read(), **kw) as resp:
                self._raise_not_found_for_status(resp, rpath)
        else:
            async with session.post(rpath + f"/operations?operationType=upload") as resp:
            #resp = await session.post(rpath + f"/operations?operationType=upload")
                self._raise_not_found_for_status(resp, rpath)
            # yield resp
            operation_id = resp.json()["operationId"]
            resp = await asyncio.gather(put_data())
            # async for resp in put_data():
            #     self._raise_not_found_for_status(resp, rpath)
            kw = self.kwargs.copy()
            kw.update({"headers": headers})
            async with session.post(url=rpath + f"/operations?operationType=upload&operationId={operation_id}", data=resp, **kw) as resp:
                self._raise_not_found_for_status(resp, rpath)

    @staticmethod
    def _construct_headers(file_local, dt_iso, url, operation_id=None, chunk_size=5 * 2 ** 20):

        headers = {
            "Content-Type": "application/octet-stream",
            "x-jpmc-distribution-created-date": dt_iso,
            "x-jpmc-distribution-from-date": dt_iso,
            "x-jpmc-distribution-to-date": dt_iso,
            "Digest": ""
        }

        headers_chunks = {
            "Content-Type": "application/octet-stream",
            "Digest": ""
        }

        headers_chunk_lst = []
        hash_md5 = hashlib.md5()
        for i, chunk in enumerate(iter(lambda: file_local.read(chunk_size), b"")):
            hash_md5_chunk = hashlib.md5()
            hash_md5_chunk.update(chunk)
            hash_md5.update(chunk)
            headers_chunks = deepcopy(headers_chunks)
            headers_chunks["Digest"] = "md5=" + base64.b64encode(hash_md5.digest()).decode()
            headers_chunk_lst.append(headers_chunks)

        file_local.seek(0)
        headers["Digest"] = "md5=" + base64.b64encode(hash_md5.digest()).decode()
        return headers, headers_chunk_lst

    def put(self,
            lpath,
            rpath,
            chunk_size=5 * 2 ** 20,
            callback=_DEFAULT_CALLBACK,
            method="put",
            multipart=False,
            **kwargs):
        """Copy file(s) from local.

        Args:
            lpath: Lpath.
            rpath: Rpath.
            chunk_size: Chunk size.
            callback: Callback function.
            method: Method: put/post.
            multipart: Flag which indicated whether it's a multipart uplaod.
            **kwargs: Kwargs.

        Returns:

        """

        dt_iso = pd.Timestamp(rpath.split("/")[-3]).strftime("%Y-%m-%d")
        headers, chunk_headers_lst = self._construct_headers(lpath, dt_iso, chunk_size)
        rpath = self._decorate_url(rpath)
        kwargs.update({"headers": headers})
        if multipart:
            kwargs.update({"chunk_headers_lst": chunk_headers_lst})
            args = [lpath, rpath, chunk_size, callback, method, multipart]
        else:
            args = [lpath, rpath, None, callback, method, multipart]

        return sync(super().loop, self._put_file, *args, **kwargs)

    def find(self, path, maxdepth=None, withdirs=False, **kwargs):
        """Find all file in a folder.

        Args:
            path: Path.
            maxdepth: Max depth.
            withdirs: With dirs, default to False.
            **kwargs: Kwargs.

        Returns:

        """
        path = self._decorate_url(path)
        return super().find(path, maxdepth=maxdepth, withdirs=withdirs, **kwargs)

    def glob(self, path, **kwargs):
        """Glob.

        Args:
            path: Path.
            **kwargs: Kwargs.

        Returns:

        """

        return super().glob(path, **kwargs)

    def open(self,
             path,
             mode="rb",
             **kwargs,
             ):
        """Open.

        Args:
            path: Path.
            mode: Defaults to rb.
            **kwargs: Kwargs.

        Returns:

        """

        path = self._decorate_url(path)
        return super().open(path, mode, **kwargs)

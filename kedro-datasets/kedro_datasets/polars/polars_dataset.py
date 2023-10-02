"""``PolarsDataset`` loads/saves data from/to a data file using an underlying
filesystem (e.g.: local, S3, GCS). It uses polars to handle the
type of read/write target.
"""
import logging
from copy import deepcopy
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Dict, Optional, Union

import fsspec
import polars as pl
import pyarrow.dataset as ds
from kedro.io.core import (
    AbstractVersionedDataSet,
    DatasetError,
    Version,
    get_filepath_str,
    get_protocol_and_path,
)

ACCEPTED_FILE_FORMATS = ["csv", "parquet"]

PolarsFrame = Union[pl.LazyFrame, pl.DataFrame]

logger = logging.getLogger(__name__)


class PolarsDataset(AbstractVersionedDataSet[pl.LazyFrame, PolarsFrame]):
    """``PolarsDataset`` loads/saves data from/to a data file using an
    underlying filesystem (e.g.: local, S3, GCS). It uses polars to handle
    the type of read/write target.

    Example adding a catalog entry with
    `YAML API
    <https://kedro.readthedocs.io/en/stable/data/\
        data_catalog.html#use-the-data-catalog-with-the-yaml-api>`_:

    .. code-block:: yaml

        >>> cars:
        >>>   type: polars.PolarsDataset
        >>>   filepath: data/01_raw/company/cars.csv
        >>>   load_args:
        >>>     sep: ","
        >>>     parse_dates: False
        >>>   save_args:
        >>>     has_header: False
                null_value: "somenullstring"
        >>>
        >>> motorbikes:
        >>>   type: polars.PolarsDataset
        >>>   filepath: s3://your_bucket/data/02_intermediate/company/motorbikes.csv
        >>>   credentials: dev_s3

    Example using Python API:
    ::

        >>> from kedro_datasets.polars import PolarsDataset
        >>> import polars as pl
        >>>
        >>> data = pl.DataFrame({'col1': [1, 2], 'col2': [4, 5],
        >>>                      'col3': [5, 6]})
        >>>
        >>> data_set = PolarsDataset(filepath="test.csv")
        >>> data_set.save(data)
        >>> reloaded = data_set.load()
        >>> assert data.frame_equal(reloaded)

    """

    DEFAULT_LOAD_ARGS: ClassVar[Dict[str, Any]] = {}
    DEFAULT_SAVE_ARGS: ClassVar[Dict[str, Any]] = {}

    # pylint: disable=too-many-arguments
    def __init__(
        self,
        filepath: str,
        file_format: str,
        load_args: Optional[Dict[str, Any]] = None,
        save_args: Optional[Dict[str, Any]] = None,
        version: Version = None,
        credentials: Optional[Dict[str, Any]] = None,
        fs_args: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Creates a new instance of ``PolarsDataset`` pointing to a concrete
        data file on a specific filesystem.

        Args:
            filepath: Filepath in POSIX format to a file prefixed with a protocol like
                `s3://`.
                If prefix is not provided, `file` protocol (local filesystem)
                will be used.
                The prefix should be any protocol supported by ``fsspec``.
                Key assumption: The first argument of either load/save method points to
                a filepath/buffer/io type location. There are some read/write targets
                such as 'clipboard' or 'records' that will fail since they do not take a
                filepath like argument.
            file_format: String which is used to match the appropriate load/save method
                on a best effort basis. For example if 'csv' is passed the
                `polars.read_csv` and
                `polars.DataFrame.write_csv` methods will be identified. An error will
                be raised unless
                at least one matching `read_{file_format}` or `write_{file_format}`.
            load_args: polars options for loading files.
                Here you can find all available arguments:
                https://pola-rs.github.io/polars/py-polars/html/reference/io.html
                All defaults are preserved.
            save_args: Polars options for saving files.
                Here you can find all available arguments:
                https://pola-rs.github.io/polars/py-polars/html/reference/io.html
                All defaults are preserved.
            version: If specified, should be an instance of
                ``kedro.io.core.Version``. If its ``load`` attribute is
                None, the latest version will be loaded. If its ``save``
                attribute is None, save version will be autogenerated.
            credentials: Credentials required to get access to the underlying filesystem.
                E.g. for ``GCSFileSystem`` it should look like `{"token": None}`.
            fs_args: Extra arguments to pass into underlying filesystem class constructor
                (e.g. `{"project": "my-project"}` for ``GCSFileSystem``), as well as
                to pass to the filesystem's `open` method through nested keys
                `open_args_load` and `open_args_save`.
                Here you can find all available arguments for `open`:
                https://filesystem-spec.readthedocs.io/en/latest/api.html#fsspec.spec.AbstractFileSystem.open
                All defaults are preserved, except `mode`, which is set to `r` when loading
                and to `w` when saving.
            metadata: Any arbitrary metadata.
                This is ignored by Kedro, but may be consumed by users or external plugins.
        Raises:
            DatasetError: Will be raised if at least less than one appropriate
                read or write methods are identified.
        """
        self._file_format = file_format.lower()

        if self._file_format not in ACCEPTED_FILE_FORMATS:
            raise DatasetError(
                f"'{self._file_format}' is not an accepted format "
                "({ACCEPTED_FILE_FORMATS}) ensure that your "
                "'file_format' parameter has been defined correctly as per the Polars"
                " Lazy API"
                " https://pola-rs.github.io/polars/py-polars/html/reference/io.html"
            )

        _fs_args = deepcopy(fs_args) or {}
        _credentials = deepcopy(credentials) or {}

        protocol, path = get_protocol_and_path(filepath, version)
        if protocol == "file":
            _fs_args.setdefault("auto_mkdir", True)

        self._protocol = protocol
        self._storage_options = {**_credentials, **_fs_args}
        self._fs = fsspec.filesystem(self._protocol, **self._storage_options)

        self.metadata = metadata

        super().__init__(
            filepath=PurePosixPath(path),
            version=version,
            exists_function=self._fs.exists,
            glob_function=self._fs.glob,
        )

        # Handle default load and save arguments
        self._load_args = deepcopy(self.DEFAULT_LOAD_ARGS)
        if load_args is not None:
            self._load_args.update(load_args)
        self._save_args = deepcopy(self.DEFAULT_SAVE_ARGS)
        if save_args is not None:
            self._save_args.update(save_args)

        if "storage_options" in self._save_args or "storage_options" in self._load_args:
            logger.warning(
                "Dropping 'storage_options' for %s, "
                "please specify them under 'fs_args' or 'credentials'.",
                self._filepath,
            )
            self._save_args.pop("storage_options", None)
            self._load_args.pop("storage_options", None)

    def _describe(self) -> Dict[str, Any]:
        return {
            "filepath": self._filepath,
            "protocol": self._protocol,
            "load_args": self._load_args,
            "save_args": self._save_args,
            "version": self._version,
        }

    def _load(self) -> pl.LazyFrame:
        load_path = str(self._get_load_path())

        if self._protocol == "file":
            # With local filesystems, we can use Polar's build-in I/O method:
            load_method = getattr(pl, f"scan_{self._file_format}", None)
            return load_method(load_path, **self._load_args)

        # For object storage, we use pyarrow for I/O:
        fsspec.filesystem("s3")
        dataset = ds.dataset(load_path, filesystem=self._fs, format=self._file_format)
        return pl.scan_pyarrow_dataset(dataset)

    def _save(self, data: Union[pl.DataFrame, pl.LazyFrame]) -> None:
        save_path = get_filepath_str(self._get_save_path(), self._protocol)
        if Path(save_path).is_dir():
            raise DatasetError(
                f"Saving {self.__class__.__name__} to a directory is not supported."
            )

        if "partition_cols" in self._save_args:
            raise DatasetError(
                f"{self.__class__.__name__} does not support save argument "
                f"'partition_cols'. Please use 'kedro.io.PartitionedDataset' instead."
            )

        BytesIO()
        collected_data = None
        if isinstance(data, pl.LazyFrame):
            collected_data = data.collect()
        else:
            collected_data = data

        save_method = getattr(collected_data, f"write_{self._file_format}", None)
        if save_method:
            buf = BytesIO()
            save_method(file=buf, **self._save_args)
            with self._fs.open(save_path, mode="wb") as fs_file:
                fs_file.write(buf.getvalue())
                self._invalidate_cache()
        else:
            raise DatasetError(
                f"Unable to retrieve 'polars.DataFrame.write_{self._file_format}' "
                "method, please ensure that your 'file_format' parameter has been "
                "defined correctly as per the Polars API"
                "https://pola-rs.github.io/polars/py-polars/html/reference/dataframe/index.html"
            )

    def _exists(self) -> bool:
        try:
            load_path = get_filepath_str(self._get_load_path(), self._protocol)
        except DatasetError:
            return False

        return self._fs.exists(load_path)

    def _release(self) -> None:
        super()._release()
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        """Invalidate underlying filesystem caches."""
        filepath = get_filepath_str(self._filepath, self._protocol)
        self._fs.invalidate_cache(filepath)

_DEPRECATED_CLASSES = {
    "PolarsDataSet": PolarsDataset,
}


def __getattr__(name):
    if name in _DEPRECATED_CLASSES:
        alias = _DEPRECATED_CLASSES[name]
        warnings.warn(
            f"{repr(name)} has been renamed to {repr(alias.__name__)}, "
            f"and the alias will be removed in Kedro-Datasets 2.0.0",
            DeprecationWarning,
            stacklevel=2,
        )
        return alias
    raise AttributeError(f"module {repr(__name__)} has no attribute {repr(name)}")

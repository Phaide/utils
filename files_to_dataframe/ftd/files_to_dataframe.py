"""
Python3 utility used to parse a directory recursively,
and store all files info in a pandas DataFrame,
which is then stored as Apache Parquet.
This file can later be used for disk usage analytics.
"""

import os
import re
import fastparquet  # Only here to raise error if not installed.
import tracemalloc
import pandas as pd

from time import time
from pathlib import Path

from .utils import write_dataframe, read_dataframe, log_duration


class FilesToDataFrame:

    # Files under this size (in bytes) will not be registered.
    file_size_threshold = 1024

    def __init__(self, directory: Path, mem_limit: int):
        self.directory = directory
        self.mem_limit = mem_limit

        # Temporary directory in which we will store the
        # temporary dataframes to limit memory usage.
        self.temp_dir = Path.cwd() / 'ftd_temp'
        self.temp_dir.mkdir()  # If this raises FileExistsError, delete temp dir

        self.run()

    @staticmethod
    def clean_path(p: Path) -> str:
        # Divide parts
        p_parts = str(p).split(os.sep)
        # Clean parts
        p_parts = [re.sub('[^A-Za-z0-9]+', '', part) for part in p_parts]
        # Remove empties
        p_parts = [part for part in p_parts if part != '']
        # Construct name
        p = '_'.join(p_parts)
        return p

    def _write_temp_df(self, data: dict) -> None:
        """
        Write a new temporary dataframe.
        """
        index = len(list(self.temp_dir.iterdir()))
        path = Path(self.temp_dir, f'{index}.tmpdf')
        df = pd.DataFrame.from_dict(data)
        write_dataframe(path, df)

    @log_duration('Walking from the root directory')
    def _walk(self) -> None:
        """
        Walk `directory` recursively,
        and store the results in temporary files on the disk.
        """

        def get_default_dict() -> dict:
            return {
                'path': [],
                'size': [],
                'uid': [],
                'atime': [],
                'mtime': [],
            }

        info = get_default_dict()
        for subdir, dirs, files in os.walk(self.directory):
            current_usage, _ = tracemalloc.get_traced_memory()
            if current_usage > self.mem_limit:
                # If we reached the memory limit,
                # cast the dictionary to a DataFrame and reset the former,
                # freeing some space.
                self._write_temp_df(info)
                info = get_default_dict()
            for file in files:
                file_path = Path(subdir, file)
                try:
                    file_stat = file_path.stat()
                except (PermissionError, FileNotFoundError, OSError):
                    continue

                file_owner = file_stat.st_uid
                file_last_access = file_stat.st_atime
                file_last_mod = file_stat.st_mtime
                file_size = file_stat.st_size
                if file_size > self.file_size_threshold:
                    info['path'].append(str(file_path))
                    info['size'].append(file_size)
                    info['uid'].append(file_owner)
                    info['atime'].append(file_last_access)
                    info['mtime'].append(file_last_mod)

        self._write_temp_df(info)

    def _df_from_temp(self) -> pd.DataFrame:
        """
        Reads the temporary directory,
        and returns a DataFrame constructed by the files' content.
        """
        dataframes = []
        # Read the temporary dataframes from the disk,
        # and remove them along the way.
        for stored_df_path in self.temp_dir.iterdir():
            new_df = read_dataframe(stored_df_path)
            dataframes.append(new_df)
            stored_df_path.unlink()
        # Finally, remove the temp directory
        self.temp_dir.rmdir()
        # And concatenate all the dataframes
        df = pd.concat(objs=dataframes)
        return df

    def get_final_df_path(self) -> Path:
        name = self.clean_path(self.directory) + '_persistent.df'
        return Path('./', name).resolve()

    def store_final_df(self, df: pd.DataFrame) -> None:
        path = self.get_final_df_path()

        # Cast to more efficient types
        if df['uid'].max() > 65535:
            uid_type = 'uint32'
        else:
            uid_type = 'uint16'
        df = df.astype({
            'path': 'string',
            'size': 'uint64',
            'uid': uid_type,
            'atime': 'uint32',  # Will work until February 2106
            'mtime': 'uint32',  # Will work until February 2106
        })

        write_dataframe(path, df)

    def run(self):
        tracemalloc.start()
        t0 = time()

        self._walk()
        t1 = time()
        _, walk_peak = tracemalloc.get_traced_memory()
        walk_peak /= (1024 ** 2)
        parsing_time = t1 - t0
        print(f'Parsing stats: '
              f'duration={parsing_time:.3f}s, '
              f'mem_peak={walk_peak:.3f}MB')

        df = self._df_from_temp()
        t2 = time()
        _, pp_peak = tracemalloc.get_traced_memory()
        df_mem_usage = sum(df.memory_usage(index=True))
        pp_peak /= (1024 ** 2)
        df_mem_usage /= (1024 ** 2)
        post_process_time = t2 - t1
        print(f'Post-processing stats: '
              f'duration={post_process_time:.3f}s, '
              f'mem_peak={pp_peak:.3f}MB, '
              f'rows={df.shape[0]}, '
              f'df_mem={df_mem_usage:.3f}MB')

        self.store_final_df(df)
        t3 = time()
        _, store_peak = tracemalloc.get_traced_memory()
        store_peak /= (1024 ** 2)
        store_time = t3 - t2
        print(f'Parsing stats: '
              f'duration={store_time:.3f}s, '
              f'mem_peak={store_peak:.3f}MB')

        _, total_peak = tracemalloc.get_traced_memory()
        total_peak /= (1024 ** 2)
        print(f'Total stats: '
              f'duration={time() - t0:.3f}s, '
              f'peak={total_peak:.3f}MB')

        tracemalloc.stop()

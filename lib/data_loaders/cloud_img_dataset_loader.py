import shutil
import pandas as pd
import os
from tqdm import tqdm
import numpy as np
import asyncio
import random
from loguru import logger
import concurrent.futures
from aiohttp_retry import RetryClient, FibonacciRetry
from sklearn.model_selection import train_test_split
from copy import deepcopy
from pandarallel import pandarallel
from urllib.parse import urlparse
from typing import Optional, Tuple, Iterable, List

from .abstract_loader import AbstractLoader


pandarallel.initialize(progress_bar=True)
tqdm.pandas()

np.random.seed(42)

logger.add('logs/logs.log', level='DEBUG')


def gen_proxy(proxy_id: int) -> str:
    super_proxy_url = ('http://%s-session-%s:%s@zproxy.lum-superproxy.io:%d' %
                       (os.getenv('PROXY_USER'),
                        proxy_id,
                        os.getenv('PROXY_PASSWORD'),
                        int(os.getenv("PROXY_PORT"))))
    return super_proxy_url


ALL_PROXY = [gen_proxy(i) for i in range(120)]
ALL_PROXY.append('self_url')


class CloudImgDatasetLoader(AbstractLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_ipfs_batch(self, batch_df: pd.DataFrame):
        urls, ids = batch_df['imageUrl'].to_list(), batch_df['id'].to_list()
        image_paths, src_paths = self._gen_all_paths(ids)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            tqdm(executor.map(self.load_ipfs_file, urls, src_paths),
                 leave=False,
                 total=len(image_paths))
        return image_paths, src_paths

    def _load_full_data(self) -> None:
        logger.info('Start downloading loop')
        for idx, batch_df in enumerate(self.df_iterator):
            batch_df = self._dataframe_preprocessing(batch_df)
            logger.info(f'Checkpoint {idx} on downloading')
            loop = asyncio.get_event_loop()
            final_paths, statuses = loop.run_until_complete(self._load_batch(batch_df))
            self.save_checkpoint(batch_df, final_paths, statuses)
            logger.info(f'Checkpoint {idx} was successfully downloaded')

    async def _load_batch(self, batch_df: pd.DataFrame) -> None:
        final_paths, final_status_codes = [], []
        for stratified_batch in self._get_stratified_batches(batch_df, self.batch_size):
            urls, ids = stratified_batch['imageUrl'].to_list(), stratified_batch['id'].to_list()
            src_paths = self._gen_all_paths(ids)
            final_paths += [*src_paths]
            statuses = await asyncio.wait(map(self._file_download_task, urls, src_paths))
            statuses = [status.result() for status in statuses[0]]
            final_status_codes += [*statuses]
        return final_paths, final_status_codes

    @staticmethod
    def _dataframe_preprocessing(df: pd.DataFrame) -> pd.DataFrame:
        logger.info('Start dataframe preprocessing!')
        df = df.dropna(subset=['id', 'imageUrl'])
        df = df.drop_duplicates(subset=['id', 'imageUrl'], keep='first', ignore_index=True)
        df = df[df['imageUrl'] != ""]
        df['domen'] = list(df.imageUrl.parallel_apply(lambda url: urlparse(url).netloc))
        return df

    @staticmethod
    def _get_stratified_batches(df: pd.DataFrame, batch_size: int):
        def get_batch(state_df: pd.DataFrame, micro_batch_size: int) -> pd.DataFrame:
            x_train, x_test, y_train, y_test = train_test_split(state_df,
                                                                state_df['domen'].to_list(),
                                                                test_size=micro_batch_size,
                                                                random_state=42)
            return x_test

        batched_df = deepcopy(df)
        batches = []
        iter_num = int(len(batched_df) / batch_size)
        for i in range(iter_num):
            if len(batched_df) > batch_size:
                batch = get_batch(batched_df, batch_size)
            else:
                batch = deepcopy(batched_df)
            batches += [batch]
            batched_df = batched_df[-batched_df['id'].isin(batch['id'].to_list())]
            yield batch

    @staticmethod
    async def _load_sample(url: str) -> Optional[bytes]:
        content = None
        headers = None
        proxy_url = random.choice(ALL_PROXY)
        old_url = deepcopy(url)
        if proxy_url == 'self_url':
            proxy_url = None
        if url.startswith('ipfs://'):
            url = url.replace('ipfs://', 'https://ipfs.io/ipfs/')

        if 'pinata.cloud' in url:
            headers = {
                'pinata_api_key': os.getenv('PINATA_API_KEY'),
                'pinata_secret_api_key': os.getenv('PINATA_SECRET')
            }
            url = url.replace('pinata.cloud', 'pixelplex.mypinata.cloud')
            proxy_url = None
        try:
            async with RetryClient(retry_options=FibonacciRetry(attempts=1, max_timeout=3)) as session:
                async with session.get(url, proxy=proxy_url, ssl=False, headers=headers) as response:
                    content = await response.read()
        except Exception as e:
            logger.debug(e)
            logger.debug(url)
            logger.debug(old_url)
            return content, e.status_code
        finally:
            return content, 200

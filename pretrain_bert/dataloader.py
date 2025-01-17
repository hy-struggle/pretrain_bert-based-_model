# /usr/bin/env python
# coding=utf-8
import torch
from torch.utils.data import Dataset

import json
from pathlib import Path
import numpy as np
import logging
from utils import set_logger
from tqdm import tqdm

set_logger()


class InputFeatures(object):
    """a single set of features of data_src
    """

    def __init__(self, input_ids, input_mask, segment_ids, lm_label_ids, is_next):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.lm_label_ids = lm_label_ids
        self.is_next = is_next


def convert_example_to_features(example, tokenizer, max_seq_length):
    tokens = example["tokens"]
    segment_ids = example["segment_ids"]
    is_random_next = example["is_random_next"]
    masked_lm_positions = example["masked_lm_positions"]
    masked_lm_labels = example["masked_lm_labels"]
    assert len(tokens) == len(segment_ids) <= max_seq_length

    input_ids = tokenizer.convert_tokens_to_ids(tokens)
    masked_label_ids = tokenizer.convert_tokens_to_ids(masked_lm_labels)

    # pad
    input_array = np.zeros(max_seq_length, dtype=np.int)
    input_array[:len(input_ids)] = input_ids
    mask_array = np.zeros(max_seq_length, dtype=np.bool)
    mask_array[:len(input_ids)] = 1
    segment_array = np.zeros(max_seq_length, dtype=np.bool)
    segment_array[:len(segment_ids)] = segment_ids
    lm_label_array = np.full(max_seq_length, dtype=np.int, fill_value=-1)
    lm_label_array[masked_lm_positions] = masked_label_ids

    features = InputFeatures(input_ids=input_array,
                             input_mask=mask_array,
                             segment_ids=segment_array,
                             lm_label_ids=lm_label_array,
                             is_next=is_random_next)

    return features


class PregeneratedDataset(Dataset):
    """create a torch dataset for pretrain data
    """

    def __init__(self, training_path, epoch, tokenizer, num_data_epochs, fp16=False):
        """
        :param training_path (Path): 数据路径
        :param epoch: 当前epoch
        :param num_data_epochs: 共有多少epoch的差异数据
        """
        self.vocab = tokenizer.vocab
        self.tokenizer = tokenizer
        self.epoch = epoch

        # 如果数据epoch小于训练epoch，则循环使用数据
        self.data_epoch = epoch % num_data_epochs
        data_file = training_path / f"epoch_{self.data_epoch}.json"
        metrics_file = training_path / f"epoch_{self.data_epoch}_metrics.json"
        # sanity check
        assert data_file.is_file() and metrics_file.is_file()

        # get metrics
        metrics = json.loads(metrics_file.read_text())
        num_samples = metrics['num_training_examples']
        seq_len = metrics['max_seq_len']

        self.fp16 = fp16

        # 保存到np.memmap
        self.temp_dir = "./tmp"
        self.working_dir = Path(self.temp_dir)
        if not self.working_dir.exists():
            self.working_dir.mkdir(exist_ok=True)
        input_ids = np.memmap(filename=self.working_dir / 'input_ids.memmap',
                              mode='w+', dtype=np.int32, shape=(num_samples, seq_len))
        input_masks = np.memmap(filename=self.working_dir / 'input_masks.memmap',
                                shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
        segment_ids = np.memmap(filename=self.working_dir / 'segment_ids.memmap',
                                shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
        lm_label_ids = np.memmap(filename=self.working_dir / 'lm_label_ids.memmap',
                                 shape=(num_samples, seq_len), mode='w+', dtype=np.int32)
        lm_label_ids[:] = -1
        is_nexts = np.memmap(filename=self.working_dir / 'is_nexts.memmap',
                             shape=(num_samples,), mode='w+', dtype=np.bool)

        logging.info(f"Loading training examples for epoch {epoch}")
        with data_file.open() as f:
            for i, line in enumerate(tqdm(f, total=num_samples, desc="Training examples")):
                line = line.strip()
                example = json.loads(line)
                features = convert_example_to_features(example, tokenizer, seq_len)
                input_ids[i] = features.input_ids
                segment_ids[i] = features.segment_ids
                input_masks[i] = features.input_mask
                lm_label_ids[i] = features.lm_label_ids
                is_nexts[i] = features.is_next

        assert i == num_samples - 1  # Assert that the sample count metric was true
        logging.info("Loading complete!")
        self.num_samples = num_samples
        self.seq_len = seq_len
        self.input_ids = input_ids
        self.input_masks = input_masks
        self.segment_ids = segment_ids
        self.lm_label_ids = lm_label_ids
        self.is_nexts = is_nexts

    def __len__(self):
        return self.num_samples

    def __getitem__(self, item):

        return (torch.tensor(self.input_ids[item].astype(np.int64)),
                torch.tensor(self.input_masks[item].astype(np.int64)),
                torch.tensor(self.segment_ids[item].astype(np.int64)),
                torch.tensor(self.lm_label_ids[item].astype(np.int64)),
                torch.tensor(self.is_nexts[item].astype(np.int64)),
                )

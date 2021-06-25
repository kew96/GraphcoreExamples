# Copyright (c) 2019 Graphcore Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import random
import glob
import os
from tqdm import tqdm
from logging import getLogger
from functools import reduce

from .dataset import DataSet
from .data_sampler import DistributedDataSampler, SampleGenerator

logger = getLogger(__name__)


def data_file_format(sequence_length, mask_tokens):
    return [sequence_length,
            sequence_length,
            sequence_length,
            1,
            1,
            mask_tokens,
            1]


def data_ranges(sequence_length, mask_tokens, vocab_length):
    return [vocab_length,
            sequence_length,
            2,
            mask_tokens+1,
            sequence_length+1,
            vocab_length,
            3]

# The contents of a packed pretraining dataset:
# Each example contains the following arrays (in this order and shape)
#   SEQ:
#   packed_input_ids, [sequence_length]
#   packed_input_mask, [sequence_length]
#   packed_segment_ids, [sequence_length]
#   packed_positions, [sequence_length]
#   MLM:
#   packed_masked_lm_positions, [mask_tokens + max_sequences_per_pack]
#   packed_masked_lm_ids, [mask_tokens + max_sequences_per_pack]
#   packed_masked_lm_weights, [mask_tokens + max_sequences_per_pack]
#   NSP:
#   packed_next_sentence_positions, [max_sequences_per_pack]
#   packed_next_sentence_labels, [max_sequences_per_pack]
#   packed_next_sentence_weights, [max_sequences_per_pack]


def packed_data_file_format(sequence_length, mask_tokens, max_sequences_per_pack):
    return [sequence_length,
            sequence_length,
            sequence_length,
            sequence_length,
            mask_tokens + max_sequences_per_pack,
            mask_tokens + max_sequences_per_pack,
            mask_tokens + max_sequences_per_pack,
            max_sequences_per_pack,
            max_sequences_per_pack,
            max_sequences_per_pack]


def packed_data_ranges(sequence_length, mask_tokens, vocab_length, max_sequences_per_pack):
    return [vocab_length,
            max_sequences_per_pack,
            2,
            sequence_length,
            sequence_length,
            vocab_length,
            max_sequences_per_pack,
            sequence_length,
            1,
            1]


# This could be replaced by a pytorch dataloader
class BinaryDataLoader(object):
    '''
    Iterates binary input files into list of N np.ndarrays with shapes (batch_size, samples_sizes[i]) for i in N

    :param input_files: Iterable of paths to binary files generated by create_pretraining_data.py
    :param sample_size: Iterable of the sizes of each element in the binary file. See data_file_format for the default
    :param batch_size: Number of samples to return each iteration. For packed pretraining data each sample is a pack.
    :param dtype: Numpy type of binary files
    :param shuffle: If True, shuffle the input_files and the data contained.
    :param duplication_factor:
        The number of times each file contains the same sample. This will then only take 1/duplication_factor
        from each file before moving to the next.
    :param synthetic: If True, generate random data instead of reading from input_files
    '''
    def __init__(self,
                 input_files,
                 sample_sizes,
                 batch_size=1,
                 dtype=np.int32,
                 shuffle=True,
                 seed=1984,
                 duplication_factor=1,
                 start_data_at_epoch=0):
        self.files = []
        for pattern in input_files:
            self.files.extend(glob.glob(pattern))
        print(f"Loading {len(self.files)} files: {self.files}")
        self.sample_size = reduce(lambda a, s: a + s, sample_sizes, 0)
        self.sample_sizes = sample_sizes
        self.batch_size = batch_size
        self.dtype = dtype
        self.file_index = 0
        self.data_index = 0
        self.file_duplication_index = [start_data_at_epoch % duplication_factor] * len(self.files)
        self.duplication_factor = duplication_factor
        self.shuffle = shuffle
        self._rng = np.random.default_rng(seed)
        self.len = None

    def samples_in_file(self, filename):
        bytes_per_sample = self.sample_size * self.dtype().itemsize
        num_bytes = os.path.getsize(filename)
        if (num_bytes % bytes_per_sample) != 0:
            raise RuntimeError(f"Input file: {filename} does not align to the size of a sample. Check the dataset was generated correctly")
        duplicated_samples = num_bytes // bytes_per_sample
        return duplicated_samples // self.duplication_factor

    def __len__(self):
        if self.len is None:
            total_bytes = reduce(lambda a, f: a + self.samples_in_file(f), self.files, 0)
            self.len = total_bytes // (self.batch_size)
        return self.len

    def __iter__(self):
        self.file_index = 0
        self.data_index = 0
        if self.shuffle:
            self._rng.shuffle(self.files)
        self.load_data()
        return self

    def __next__(self):
        data = self.get_data(self.batch_size)
        # Split the batch into separate np.ndarrays
        items = []
        total = 0
        for size in self.sample_sizes:
            items.append(np.array(data[:, total:total + size]))
            total += size
        return items

    def get_data(self, batch_size):
        """
        Slice batch_size samples from self.data or from the next file if there is not enough left
        """
        if self.data_index + batch_size > self.data.shape[0]:
            prev_data = self.data[self.data_index:, :]
            still_required = batch_size - prev_data.shape[0]
            self.load_data()
            next_data = self.get_data(still_required)
            data = np.concatenate((prev_data, next_data), axis=0)
        else:
            data = self.data[self.data_index:self.data_index + batch_size, :]
            self.data_index += batch_size
        return data

    def load_data(self):
        # This drops the remainder
        if self.file_index >= len(self.files):
            raise StopIteration
        self.data = self.load_file()
        if self.shuffle:
            self._rng.shuffle(self.data)

    def load_file(self):
        filename = self.files[self.file_index]
        # Input files are assumed to be duplicated by create_pretraining_data only within a single file.
        # So for preprocessed files: A, B, C. The output files are created: AAA.., BBB.., CCC..
        # This makes sure in a single epoch A, B & C are all used once.
        count = self.samples_in_file(filename) * self.sample_size
        offset_bytes = count * self.file_duplication_index[self.file_index] * self.dtype().itemsize

        new_data = np.fromfile(filename, self.dtype, count=count, offset=offset_bytes)
        new_data = new_data.reshape(new_data.size // self.sample_size,
                                    self.sample_size)

        self.file_duplication_index[self.file_index] = \
            (self.file_duplication_index[self.file_index] + 1) % self.duplication_factor

        self.file_index += 1
        self.data_index = 0

        return new_data


class CachedDataLoader(BinaryDataLoader):
    """
    Same as the BinaryDataLoader but preloads the specified number of epochs into memory ahead of time.
    :param epochs_to_cache:
        Specify the number of epochs to keep loaded in memory. This can reduce the number of times the inputs
        are read. It is recommended to make this as large as possible as the dataset files can be very large due to duplication factor.
        Must be greater than 0.
    """
    def __init__(self,
                 *args,
                 epochs_to_cache=1,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.epochs_to_cache = epochs_to_cache
        self.data_cache = []
        self.cache_index = 0

        if self.epochs_to_cache < 1:
            raise RuntimeError("epochs_to_cache must be greater than 0")

        self.load_cache()
        self.len = self.data_cache[0].shape[0] // self.batch_size

    def get_data(self, batch_size):
        if self.data_index + batch_size > self.data.shape[0]:
            raise StopIteration

        data = self.data[self.data_index:self.data_index + batch_size, :]
        self.data_index += batch_size
        return data

    def load_data(self):
        if self.cache_index >= len(self.data_cache):
            if self.shuffle or self.duplication_factor > 1:
                self.load_cache()
            else:
                self.cache_index = 0
        self.data = self.data_cache[self.cache_index]
        self.cache_index += 1

    def load_cache(self):
        self.cache_index = 0
        self.data_cache = []
        logger.info("Filling Dataset Cache")
        for __ in range(self.epochs_to_cache):
            data = []
            for __ in tqdm(self.files):
                data.append(self.load_file())
            data = np.concatenate(data, axis=0)
            if self.shuffle:
                self._rng.shuffle(data)
            self.data_cache.append(data)
            self.file_index = 0


class GeneratedDataLoader(BinaryDataLoader):
    """
    Same as the BinaryDataLoader but generates random data instead of reading from input_files
    :param generated_ranges: Iterable of the max value each element of a sample can be. See data_ranges for the default
    """
    def __init__(self,
                 *args,
                 length=1,
                 generated_ranges=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.generated_ranges = generated_ranges
        self.len = length

        if self.generated_ranges is None:
            raise RuntimeError("keyword argument 'generated_ranges' must not be None")

    def __iter__(self):
        self.data_index = 0
        return self

    def __next__(self):
        if self.data_index >= self.len:
            raise StopIteration
        items = []
        for size, max_value in zip(self.sample_sizes, self.generated_ranges):
            items.append(np.random.randint(0, max_value, [self.batch_size, size]))
        self.data_index += 1
        return items


class BertDataTransform(object):
    '''
    Masks the indices that are larger than the vocab_length
    '''
    def __init__(self, dataloader, vocab_length, mask_tokens):
        self.dataloader = dataloader
        self.vocab_length = vocab_length
        self.mask_tokens = mask_tokens

    def __len__(self):
        return len(self.dataloader)

    def __iter__(self):
        self.dataloader_iterator = iter(self.dataloader)
        return self

    def __next__(self):
        items = next(self.dataloader_iterator)
        # Specific BERT Post Processing. TODO: Find a better place for this processing
        # The vocab_length may be smaller than the original vocab so
        # Mask values that are not within the vocab_length
        # 100 is unknown token [UNK]
        # 0 in the label is padding
        OOB = items[0] >= self.vocab_length
        items[0][OOB] = 100

        # TODO: If Ind == [MASK] and label > vocab_length, should [MASK] be changed to [UNK]
        OOB = items[5] >= self.vocab_length
        items[5][OOB] = 0

        # Force use of uint32 for all inputs.
        for i in range(len(items)):
            items[i] = items[i].astype(np.uint32)
        return items


def get_bert_dataset(args, tensor_shapes):
    generated_data = args.generated_data or args.synthetic_data
    samples_per_step = args.micro_batch_size * args.batches_per_step * args.replication_factor * args.gradient_accumulation_factor

    if not generated_data and len(args.input_files) == 0:
        raise ValueError("No input files were provided for the BERT dataset.")

    # Support for packed pretraining data i.e. multiple samples per sequence
    if args.use_packed_sequence_format:
        sample_sizes = packed_data_file_format(args.sequence_length, args.mask_tokens, args.max_sequences_per_pack)
        synthetic_data_ranges = packed_data_ranges(args.sequence_length, args.mask_tokens, args.vocab_length, args.max_sequences_per_pack)
    else:
        sample_sizes = data_file_format(args.sequence_length, args.mask_tokens)
        synthetic_data_ranges = data_ranges(args.sequence_length, args.mask_tokens, args.vocab_length)


    data_loader_args = dict(
        input_files=args.input_files,
        sample_sizes=sample_sizes,
        batch_size=samples_per_step,
        duplication_factor=args.duplication_factor,
        start_data_at_epoch=args.continue_training_from_epoch,
        shuffle=args.shuffle,
        seed=args.seed)
    tfrecord_input = args.input_files[0].lower().endswith('tfrecord') if len(args.input_files) > 0 else False
    if generated_data:
        length = 1
        if args.use_popdist:
            length = args.popdist_size
        dl = GeneratedDataLoader(**data_loader_args,
                                 length=length,
                                 generated_ranges=synthetic_data_ranges)
    elif tfrecord_input:
        if args.use_packed_sequence_format:
            raise RuntimeError("tfrecord dataset not supported for packed sequence data format")

        from .tfrecord_dataset import PretrainingTfRecordDataLoader
        dl = PretrainingTfRecordDataLoader(args.input_files,
                                           args.sequence_length,
                                           args.mask_tokens,
                                           samples_per_step)
    elif args.epochs_to_cache > 0:
        dl = CachedDataLoader(**data_loader_args,
                              epochs_to_cache=args.epochs_to_cache)
    else:
        dl = BinaryDataLoader(**data_loader_args)

    if args.use_popdist:
        sampler = DistributedDataSampler(
            dl,
            popdist_size=args.popdist_size,
            popdist_rank=args.popdist_rank)
        dl = SampleGenerator(dl, sampler)

    if len(dl) == 0:
        raise ValueError("Insufficient data for training parameters.")

    bert_ds = BertDataTransform(dl, args.vocab_length, args.mask_tokens)
    ds = DataSet(bert_ds,
                 tensor_shapes,
                 batches_per_step=args.batches_per_step,
                 replication_factor=args.replication_factor,
                 accumulation_factor=args.gradient_accumulation_factor)

    return ds

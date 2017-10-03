import concurrent.futures as fs
import io
import itertools
import os
import time

import boto3
import cloudpickle
import numpy as np
import hashlib

def hash_string(s):
    return hashlib.sha1(s.encode('utf-8')).hexdigest()

def chunk(l, n):
    """Yield successive n-sized chunks from l."""
    if n == 0: return []
    for i in range(0, len(l), n):
        yield l[i:i + n]

def generate_key_name(X, Y, op):
    assert op == "gemm"
    key = "gemm({0}, {1})".format(str(X), str(Y))
    return key

def load_mmap(mmap_loc, mmap_shape, mmap_dtype):
    return np.memmap(mmap_loc, dtype=mmap_dtype, mode='r+', shape=mmap_shape)

def list_all_keys(bucket, prefix):
    client = boto3.client('s3')
    objects = client.list_objects(Bucket=bucket, Prefix=prefix, Delimiter=prefix)
    keys = list(map(lambda x: x['Key'], objects['Contents']))
    truncated = objects['IsTruncated']
    next_marker = objects.get('NextMarker')
    while truncated:
        objects = client.list_objects(Bucket=bucket, Prefix=prefix,
                                      Delimiter=prefix, Marker=next_marker)
        truncated = objects['IsTruncated']
        next_marker = objects.get('NextMarker')
        keys += list(map(lambda x: x['Key'], objects['Contents']))
    return list(filter(lambda x: len(x) > 0, keys))

def block_key_to_block(key):
    try:
        block_key = key.strip().split("/")[-1]
        if (block_key) == "header": return None
        blocks_split = block_key.strip('_').split("_")

        print(len(blocks_split))
        print(blocks_split)
        assert(len(blocks_split)%3 == 0)
        block = []
        for i in range(0,len(blocks_split),3):
            start = int(blocks_split[i])
            end = int(blocks_split[i+1])
            block.append((start,end))

        print("RETURNING", block)
        return tuple(block)
    except Exception as e:
        raise
        return None

def get_blocks_mmap(bigm, block_idxs, local_idxs, mmap_loc, mmap_shape, dtype='float32'):
    '''Map block_idxs to local_idxs in np.memmamp object found at mmap_loc'''
    print("MMAP_SHAPE", mmap_shape)
    X_full = np.memmap(mmap_loc, dtype=dtype, mode='r+', shape=mmap_shape)
    for block_idx, local_idx  in zip(block_idxs, local_idxs):
        local_idx_slices = [slice(s,e) for s,e in local_idx]
        block_data = bigm.get_block(*block_idx)
        X_full[local_idx_slices] = block_data
    X_full.flush()
    return (mmap_loc, mmap_shape, dtype)


def get_local_matrix(bigm, workers=22, mmap_loc=None, big_axis=0):
    hash_key = hash_string(bigm.key)
    if (mmap_loc == None):
        mmap_loc = "/tmp/{0}".format(hash_key)
    executor = fs.ProcessPoolExecutor(max_workers=workers)
    blocks_to_get = [bigm._block_idxs(i) for i in range(len(bigm.shape))]
    futures = get_matrix_blocks_full_async(bigm, mmap_loc, *blocks_to_get)
    fs.wait(futures)
    [f.result() for f in futures]
    return load_mmap(*futures[0].result())


def get_matrix_blocks_full_async(bigm, mmap_loc, *blocks_to_get, big_axis=0, executor=None, workers=20):
    '''
        Download blocks from bigm using multiprocess and memmap to maximize S3 bandwidth
        * blocks_to_get is a list equal in length to the number of dimensions of bigm
        * each element of that list is a block to get from that axis
    '''
    mmap_shape = []
    local_idxs = []
    matrix_locations = [{} for _ in range(len(bigm.shape))]
    matrix_maxes = [0 for _ in range(len(bigm.shape))]
    current_local_idx = np.zeros(len(bigm.shape), np.int)
    print(blocks_to_get)
    # statically assign parts of our mmap matrix to parts of our sharded matrix
    for axis, axis_blocks in enumerate(blocks_to_get):
        axis_size = 0
        for block in axis_blocks:
            size = int(min(bigm.shard_sizes[axis], bigm.shape[axis] - block*bigm.shard_sizes[axis]))
            axis_size += size
            start = bigm.shard_sizes[axis]*block
            end = start + size
            if (matrix_locations[axis].get((start,end)) == None):
                matrix_locations[axis][(start,end)] = (matrix_maxes[axis], matrix_maxes[axis]+size)
                matrix_maxes[axis] += size
        mmap_shape.append(axis_size)

    mmap_shape = tuple(mmap_shape)
    if (executor == None):
        executor = fs.ProcessPoolExecutor(max_workers=workers)
    np.memmap(mmap_loc, dtype=bigm.dtype, mode='w+', shape=mmap_shape)
    futures = []

    # chunk across whatever we decided is our "big axis"
    chunk_size = int(np.ceil(len(blocks_to_get[big_axis])/workers))
    chunks = list(chunk(blocks_to_get[big_axis], chunk_size))
    blocks_to_get = list(blocks_to_get)
    for c in chunks:
        c = sorted(c)
        small_axis_blocks = blocks_to_get.copy()
        del small_axis_blocks[big_axis]
        small_axis_blocks.insert(big_axis, c)
        block_idxs = list(itertools.product(*small_axis_blocks))
        local_idxs = []
        for block_idx in block_idxs:
            real_idx = bigm.__block_idx_to_real_idx__(block_idx)
            local_idx = tuple((matrix_locations[i][(s,e)] for i,(s,e) in enumerate(real_idx)))
            local_idxs.append(local_idx)
            print("REAL_IDX", real_idx, "LOCAL_IDX", local_idx)
        print("LOCAL_IDXS",  local_idxs)
        futures.append(executor.submit(get_blocks_mmap, bigm, block_idxs, local_idxs, mmap_loc, mmap_shape, bigm.dtype))
    return futures











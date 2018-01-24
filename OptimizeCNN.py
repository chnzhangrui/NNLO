#!/usr/bin/env python3

import sys,os
import numpy as np
import argparse
import json
import time
import glob
from mpi4py import MPI

sys.path.append(os.path.dirname(os.path.realpath(__file__))+'/mpi_learn_src')
from mpi_learn.train.algo import Algo
from mpi_learn.train.data import H5Data
from mpi_learn.train.model import ModelFromJsonTF
from mpi_learn.utils import import_keras
import mpi_learn.mpi.manager as mm

import coordinator
import proc_block
import mpiLAPI as mpi

def get_block_num(comm, block_size):
    """
    Gets the correct block number for this process.
    The coordinator (process 0) is in block 999.
    The other processes are divided according to the block size.
    """
    rank = comm.Get_rank()
    if rank == 0:
        return 999
    block_num = int((rank-1) / block_size)
    return block_num

def make_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument('--batch', help='batch size', default=100, type=int)
    parser.add_argument('--epochs', help='number of training epochs', default=1, type=int)
    parser.add_argument('--optimizer',help='optimizer for master to use',default='adam')
    parser.add_argument('--loss',help='loss function',default='binary_crossentropy')
    parser.add_argument('--early-stopping', type=int, 
            dest='early_stopping', help='patience for early stopping')
    parser.add_argument('--sync-every', help='how often to sync weights with master', 
            default=1, type=int, dest='sync_every')

    parser.add_argument('--block-size', type=int, default=1,
            help='number of GPUs per block')
    return parser


if __name__ == '__main__':
    parser = make_parser()
    args = parser.parse_args()

    train_list = glob.glob('/bigdata/shared/LCDJets_Remake/train/04*.h5')
    val_list = glob.glob('/bigdata/shared/LCDJets_Remake/val/020*.h5')

    comm_world = MPI.COMM_WORLD.Dup()
    num_blocks = int(comm_world.Get_size()/args.block_size)
    block_num = get_block_num(comm_world, args.block_size)
    device = mm.get_device(comm_world, num_blocks)
    backend = 'tensorflow'
    comm_block = comm_world.Split(block_num)

    param_ranges = [
            (0.0, 1.0), # dropout
            (1, 6), # kernel_size
            (1., 10.), # lr exponent
            ]

    # MPI process 0 coordinates the Bayesian optimization procedure
    if block_num == -1:
        model_fn = lambda x, y, z: mpiLAPI.test_cnn(x, y, np.exp(-z))
        opt_coordinator = coordinator.Coordinator(comm_world, num_blocks,
                param_ranges, model_fn)
        opt_coordinator.run(num_iterations=5)
    else:
        data = H5Data( batch_size=args.batch, 
                features_name=args.features_name, labels_name=args.labels_name )
        data.set_file_names( train_list )
        validate_every = data.count_data()/args.batch 
        algo = Algo(args.optimizer, loss=args.loss, validate_every=validate_every,
                sync_every=args.sync_every) 
        os.environ['KERAS_BACKEND'] = backend
        import_keras()
        import keras.callbacks as cbks
        callbacks = []
        if args.early_stopping is not None:
            callbacks.append( cbks.EarlyStopping( patience=args.early_stopping,
                verbose=1 ) )
        block = proc_block.ProcBlock(comm_world, comm_block, algo, data,
                args.epochs, train_list, val_list, callbacks)
        block.run()
